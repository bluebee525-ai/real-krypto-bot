"""
LTC Discord Bot - Auto-detects transactions + /checktx + DM notifications
Hosted on Railway | Uses Sochain API (100% free, no signup or API key needed)
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import os
import io
import qrcode
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────────────────────────────────────
# CONFIG  (set these in Railway environment variables)
# ─────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["DISCORD_BOT_TOKEN"]          # required
NOTIFY_USER_ID   = int(os.environ.get("NOTIFY_USER_ID", 0)) # your Discord user ID
WATCH_ADDRESS    = os.environ.get("LTC_WATCH_ADDRESS", "")  # LTC address to auto-watch
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", 30)) # seconds between polls
REQUIRED_CONFS   = int(os.environ.get("REQUIRED_CONFS", 6)) # confirmations for "confirmed"

# Sochain V3 API — completely free, no API key, no signup
SOCHAIN_BASE = "https://sochain.com/api/v3"

# Colors
C_LTC     = 0x345D9D
C_GREEN   = 0x2ECC71
C_RED     = 0xE74C3C
C_ORANGE  = 0xF39C12
C_SILVER  = 0xA8A8A8
C_GOLD    = 0xFFD700

# ─────────────────────────────────────────────────────────
# BOT SETUP
# ─────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# State
watched_addresses: dict[str, dict] = {}   # addr -> {last_tx_hash, channel_id}
watched_txids: dict[str, dict] = {}       # txid -> {channel_id, user_id, last_confs, notified_confirmed}
invoice_store: dict[str, dict] = {}
invoice_counter = 0


# ─────────────────────────────────────────────────────────
# API HELPERS  (Sochain V3 — free, no key needed)
# ─────────────────────────────────────────────────────────
async def _get(url: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 200:
                data = await r.json()
                # Sochain wraps responses in {"data": {...}}
                return data.get("data", data)
    return None

async def get_tx(txid: str) -> dict | None:
    """Fetch a transaction and normalise to a common schema."""
    raw = await _get(f"{SOCHAIN_BASE}/transaction/LTC/{txid}")
    if not raw:
        return None
    # Normalise Sochain fields → our internal schema
    confs = int(raw.get("confirmations", 0))
    # total output value
    total_sats = sum(
        int(float(o.get("value", 0)) * 1e8)
        for o in raw.get("outputs", [])
    )
    fee_sats = int(float(raw.get("fee", 0)) * 1e8)
    return {
        "hash":          raw.get("hash") or raw.get("txid", txid),
        "confirmations": confs,
        "total":         total_sats,
        "fees":          fee_sats,
        "size":          raw.get("size", 0),
        "confirmed":     raw.get("time"),
        "received":      raw.get("time"),
        "inputs":        [
            {"addresses": [i.get("address")] if i.get("address") else []}
            for i in raw.get("inputs", [])
        ],
        "outputs":       [
            {
                "addresses": [o.get("address")] if o.get("address") else [],
                "value":     int(float(o.get("value", 0)) * 1e8),
            }
            for o in raw.get("outputs", [])
        ],
    }

async def get_address_txs(address: str) -> dict | None:
    """Return latest txs for an address. Returns normalised dict with 'txrefs' list."""
    raw = await _get(f"{SOCHAIN_BASE}/transactions/LTC/{address}/1")
    if not raw:
        return None
    txs = raw.get("transactions", [])
    return {
        "txrefs": [{"tx_hash": t.get("hash") or t.get("txid")} for t in txs if t.get("hash") or t.get("txid")]
    }

async def get_network_stats() -> dict | None:
    """Fetch LTC network info from Sochain."""
    return await _get(f"{SOCHAIN_BASE}/info/LTC")

def satoshi_to_ltc(sats: int) -> float:
    return sats / 1e8

def confirmation_bar(confs: int, required: int = 6) -> str:
    filled = min(confs, required)
    bar = "█" * filled + "░" * (required - filled)
    return f"`[{bar}]` {confs}/{required}"

def conf_color(confs: int) -> int:
    if confs == 0:   return C_ORANGE
    if confs < 3:    return C_SILVER
    if confs < 6:    return C_LTC
    return C_GREEN

def tx_status_label(confs: int) -> str:
    if confs == 0:  return "⏳ Unconfirmed (mempool)"
    if confs < 3:   return "🔄 Confirming..."
    if confs < 6:   return "✅ Partially Confirmed"
    return "🔒 Fully Confirmed"


# ─────────────────────────────────────────────────────────
# EMBED BUILDERS
# ─────────────────────────────────────────────────────────
def build_tx_embed(tx: dict, title: str = "🔍 Transaction Details") -> discord.Embed:
    txid    = tx.get("hash", "N/A")
    confs   = tx.get("confirmations", 0)
    total   = tx.get("total", 0)
    fees    = tx.get("fees", 0)
    size    = tx.get("size", 0)
    t       = tx.get("confirmed") or tx.get("received", "")

    inputs  = tx.get("inputs", [])
    outputs = tx.get("outputs", [])

    # Sender addresses
    senders = []
    for inp in inputs[:3]:
        addrs = inp.get("addresses", [])
        senders += addrs
    sender_str = "\n".join(f"`{a}`" for a in senders[:3]) or "Coinbase / Unknown"
    if len(inputs) > 3:
        sender_str += f"\n*+{len(inputs)-3} more*"

    # Receiver addresses
    receivers = []
    for out in outputs[:3]:
        addrs = out.get("addresses", [])
        val   = satoshi_to_ltc(out.get("value", 0))
        for a in addrs:
            receivers.append(f"`{a}` — **{val:.6f} LTC**")
    receiver_str = "\n".join(receivers[:3]) or "Unknown"
    if len(outputs) > 3:
        receiver_str += f"\n*+{len(outputs)-3} more*"

    # Time
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        time_str = str(t) or "Pending"

    embed = discord.Embed(
        title=title,
        color=conf_color(confs),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name="Litecoin Network",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.add_field(
        name="🆔 TXID",
        value=f"```{txid}```",
        inline=False
    )
    embed.add_field(
        name="💰 Amount",
        value=f"**{satoshi_to_ltc(total):.8f} LTC**",
        inline=True
    )
    embed.add_field(
        name="⛽ Fee",
        value=f"{satoshi_to_ltc(fees):.8f} LTC",
        inline=True
    )
    embed.add_field(
        name="📦 Size",
        value=f"{size} bytes",
        inline=True
    )
    embed.add_field(
        name="📊 Status",
        value=f"{tx_status_label(confs)}\n{confirmation_bar(confs, REQUIRED_CONFS)}",
        inline=False
    )
    embed.add_field(name="📤 From",    value=sender_str,   inline=True)
    embed.add_field(name="📥 To",      value=receiver_str, inline=True)
    embed.add_field(name="🕐 Time",    value=time_str,      inline=False)
    embed.set_footer(
        text=f"View on Explorer",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.url = f"https://sochain.com/tx/LTC/{txid}"
    return embed


def build_new_tx_embed(tx: dict, address: str) -> discord.Embed:
    embed = build_tx_embed(tx, title="🚨 New Incoming Transaction Detected!")
    embed.color = C_ORANGE
    embed.insert_field_at(0,
        name="📍 Watched Address",
        value=f"`{address}`",
        inline=False
    )
    return embed


def build_confirmed_embed(txid: str, confs: int) -> discord.Embed:
    embed = discord.Embed(
        title="🔒 Transaction Fully Confirmed!",
        description=f"Your transaction has reached **{confs} confirmations** and is now fully settled on the Litecoin blockchain.",
        color=C_GREEN,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name="Litecoin Network",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.add_field(name="🆔 TXID", value=f"```{txid}```", inline=False)
    embed.add_field(
        name="📊 Confirmations",
        value=confirmation_bar(confs, REQUIRED_CONFS),
        inline=False
    )
    embed.url = f"https://sochain.com/tx/LTC/{txid}"
    embed.set_footer(text="Litecoin Bot • Powered by Sochain")
    return embed


def build_invoice_embed(inv: dict) -> discord.Embed:
    status_map = {
        "pending":   ("⏳ Awaiting Payment", C_ORANGE),
        "paid":      ("✅ Paid & Confirmed",  C_GREEN),
        "expired":   ("❌ Expired",           C_RED),
    }
    label, color = status_map.get(inv["status"], ("❓ Unknown", C_SILVER))

    embed = discord.Embed(
        title=f"🧾 Invoice #{inv['id']}",
        description=f"**{inv.get('description', 'Litecoin Payment')}**",
        color=color,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name="Litecoin Invoice",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.add_field(name="💎 Amount",  value=f"**{inv['amount']} LTC**",  inline=True)
    embed.add_field(name="📌 Status",  value=label,                        inline=True)
    embed.add_field(name="🔢 Invoice", value=f"`#{inv['id']}`",            inline=True)
    embed.add_field(
        name="📬 Pay To",
        value=f"```{inv['address']}```",
        inline=False
    )
    if inv.get("txid"):
        embed.add_field(name="🔗 TXID", value=f"```{inv['txid']}```", inline=False)
    embed.add_field(
        name="📋 How to Pay",
        value=(
            f"Send exactly **{inv['amount']} LTC** to the address above.\n"
            f"The bot will automatically detect and confirm your payment."
        ),
        inline=False
    )
    embed.set_footer(text=f"Created by {inv['creator']} • Invoice Bot")
    return embed


# ─────────────────────────────────────────────────────────
# QR CODE GENERATOR
# ─────────────────────────────────────────────────────────
def make_qr_image(address: str, amount: float) -> io.BytesIO:
    data = f"litecoin:{address}?amount={amount}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#345D9D", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ─────────────────────────────────────────────────────────
# BACKGROUND TASKS
# ─────────────────────────────────────────────────────────
@tasks.loop(seconds=POLL_INTERVAL)
async def poll_addresses():
    """Auto-detect new transactions on watched addresses."""
    if not watched_addresses:
        return
    for address, state in list(watched_addresses.items()):
        try:
            data = await get_address_txs(address)
            if not data:
                continue
            txrefs = data.get("txrefs", []) + data.get("unconfirmed_txrefs", [])
            if not txrefs:
                continue
            latest = txrefs[0]
            latest_hash = latest.get("tx_hash")
            if not latest_hash:
                continue

            if state.get("last_tx_hash") != latest_hash:
                # New transaction detected!
                watched_addresses[address]["last_tx_hash"] = latest_hash
                tx = await get_tx(latest_hash)
                if not tx:
                    continue

                # Notify user via DM
                if NOTIFY_USER_ID:
                    try:
                        user = await bot.fetch_user(NOTIFY_USER_ID)
                        embed = build_new_tx_embed(tx, address)
                        await user.send(embed=embed)
                    except Exception as e:
                        print(f"DM error: {e}")

                # Also post in channel if set
                channel_id = state.get("channel_id")
                if channel_id:
                    try:
                        ch = bot.get_channel(channel_id)
                        if ch:
                            embed = build_new_tx_embed(tx, address)
                            await ch.send(embed=embed)
                    except Exception as e:
                        print(f"Channel notify error: {e}")

                # Auto-watch this txid for confirmations
                watched_txids[latest_hash] = {
                    "channel_id":        state.get("channel_id"),
                    "user_id":           NOTIFY_USER_ID,
                    "last_confs":        tx.get("confirmations", 0),
                    "notified_confirmed": False,
                }
        except Exception as e:
            print(f"poll_addresses error for {address}: {e}")


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_txids():
    """Poll watched TXIDs for confirmation updates."""
    for txid, state in list(watched_txids.items()):
        try:
            tx = await get_tx(txid)
            if not tx:
                continue
            confs = tx.get("confirmations", 0)
            last  = state.get("last_confs", 0)

            # Update stored confs
            watched_txids[txid]["last_confs"] = confs

            # Notify at milestone confirmations: 1, 3, 6
            milestones = [1, 3, REQUIRED_CONFS]
            for m in milestones:
                if last < m <= confs:
                    await send_conf_update(txid, tx, state, confs)
                    break

            # Final confirmation notification
            if confs >= REQUIRED_CONFS and not state.get("notified_confirmed"):
                watched_txids[txid]["notified_confirmed"] = True
                await send_final_confirmed(txid, confs, state)

        except Exception as e:
            print(f"poll_txids error for {txid}: {e}")


async def send_conf_update(txid: str, tx: dict, state: dict, confs: int):
    embed = build_tx_embed(tx, title=f"🔄 Confirmation Update — {confs} conf{'s' if confs != 1 else ''}")
    user_id = state.get("user_id")
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception as e:
            print(f"send_conf_update DM error: {e}")
    channel_id = state.get("channel_id")
    if channel_id:
        try:
            ch = bot.get_channel(channel_id)
            if ch:
                await ch.send(embed=embed)
        except Exception:
            pass


async def send_final_confirmed(txid: str, confs: int, state: dict):
    embed = build_confirmed_embed(txid, confs)
    user_id = state.get("user_id")
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
            await user.send(embed=embed)
        except Exception as e:
            print(f"final confirmed DM error: {e}")
    channel_id = state.get("channel_id")
    if channel_id:
        try:
            ch = bot.get_channel(channel_id)
            if ch:
                await ch.send(embed=embed)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────
# SLASH COMMANDS
# ─────────────────────────────────────────────────────────

@bot.tree.command(name="checktx", description="Look up a Litecoin transaction by TXID")
@app_commands.describe(txid="The Litecoin transaction ID (64 hex characters)")
async def checktx(interaction: discord.Interaction, txid: str):
    await interaction.response.defer(ephemeral=False)
    txid = txid.strip()
    if len(txid) != 64 or not all(c in "0123456789abcdefABCDEF" for c in txid):
        embed = discord.Embed(
            title="❌ Invalid TXID",
            description="Please provide a valid 64-character hex transaction ID.",
            color=C_RED
        )
        await interaction.followup.send(embed=embed)
        return

    tx = await get_tx(txid)
    if not tx:
        embed = discord.Embed(
            title="❌ Transaction Not Found",
            description=f"Could not find transaction:\n```{txid}```\nMake sure this is a valid LTC txid.",
            color=C_RED
        )
        await interaction.followup.send(embed=embed)
        return

    embed = build_tx_embed(tx)
    confs = tx.get("confirmations", 0)

    # Auto-watch if unconfirmed
    if confs < REQUIRED_CONFS:
        watched_txids[txid] = {
            "channel_id":        interaction.channel_id,
            "user_id":           interaction.user.id,
            "last_confs":        confs,
            "notified_confirmed": confs >= REQUIRED_CONFS,
        }
        embed.set_footer(text=f"👁️ Now watching for confirmations — you'll be DM'd at 1, 3 & {REQUIRED_CONFS} confs")

    await interaction.followup.send(embed=embed)

    # Also DM the user
    if interaction.user.id != NOTIFY_USER_ID:
        try:
            await interaction.user.send(embed=embed)
        except Exception:
            pass


@bot.tree.command(name="watch", description="Watch a Litecoin address for incoming transactions")
@app_commands.describe(address="The LTC address to monitor")
async def watch(interaction: discord.Interaction, address: str):
    await interaction.response.defer(ephemeral=True)
    address = address.strip()

    # Basic LTC address validation
    if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
        embed = discord.Embed(
            title="❌ Invalid LTC Address",
            description="Please provide a valid Litecoin address (starts with L, M, or ltc1).",
            color=C_RED
        )
        await interaction.followup.send(embed=embed)
        return

    # Fetch current state so we don't alert on old txs
    data = await get_address_txs(address)
    last_hash = None
    if data:
        txrefs = data.get("txrefs", [])
        if txrefs:
            last_hash = txrefs[0].get("tx_hash")

    watched_addresses[address] = {
        "channel_id": interaction.channel_id,
        "last_tx_hash": last_hash,
    }

    embed = discord.Embed(
        title="👁️ Address Now Being Watched",
        description=f"I'll notify you in this channel **and DM you** when new transactions arrive.",
        color=C_LTC
    )
    embed.add_field(name="📬 Address", value=f"```{address}```", inline=False)
    embed.add_field(
        name="🔔 Notifications",
        value=f"• New transaction detected\n• Confirmations: 1 → 3 → {REQUIRED_CONFS}\n• Final confirmation",
        inline=False
    )
    embed.set_footer(text=f"Polling every {POLL_INTERVAL}s • Powered by Sochain")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="unwatch", description="Stop watching a Litecoin address")
@app_commands.describe(address="The LTC address to stop monitoring")
async def unwatch(interaction: discord.Interaction, address: str):
    address = address.strip()
    if address in watched_addresses:
        del watched_addresses[address]
        embed = discord.Embed(
            title="🛑 Stopped Watching",
            description=f"No longer monitoring `{address}`",
            color=C_SILVER
        )
    else:
        embed = discord.Embed(
            title="❓ Not Found",
            description=f"`{address}` is not currently being watched.",
            color=C_RED
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="watchlist", description="See all currently watched LTC addresses")
async def watchlist(interaction: discord.Interaction):
    if not watched_addresses and not watched_txids:
        embed = discord.Embed(
            title="📭 Nothing Watched",
            description="No addresses or transactions are currently being monitored.\nUse `/watch <address>` to start.",
            color=C_SILVER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(title="👁️ Watch List", color=C_LTC, timestamp=datetime.now(timezone.utc))

    if watched_addresses:
        addr_lines = "\n".join(f"• `{a}`" for a in watched_addresses)
        embed.add_field(name=f"📬 Addresses ({len(watched_addresses)})", value=addr_lines, inline=False)

    if watched_txids:
        tx_lines = []
        for txid, s in list(watched_txids.items())[:10]:
            confs = s.get("last_confs", 0)
            tx_lines.append(f"• `{txid[:16]}...` — {confs} conf(s)")
        embed.add_field(name=f"🔗 Transactions ({len(watched_txids)})", value="\n".join(tx_lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="invoice", description="Create a Litecoin payment invoice")
@app_commands.describe(
    address="Your LTC receiving address",
    amount="Amount in LTC",
    description="What the payment is for (optional)"
)
async def invoice(
    interaction: discord.Interaction,
    address: str,
    amount: float,
    description: str = "Litecoin Payment"
):
    await interaction.response.defer()
    global invoice_counter
    invoice_counter += 1
    inv_id = f"{invoice_counter:04d}"

    if amount <= 0:
        await interaction.followup.send("❌ Amount must be greater than 0.", ephemeral=True)
        return

    inv = {
        "id":          inv_id,
        "address":     address.strip(),
        "amount":      amount,
        "description": description,
        "status":      "pending",
        "txid":        None,
        "creator":     str(interaction.user),
        "channel_id":  interaction.channel_id,
        "user_id":     interaction.user.id,
    }
    invoice_store[inv_id] = inv

    # Auto-watch the address for payment detection
    if address not in watched_addresses:
        data = await get_address_txs(address)
        last_hash = None
        if data:
            txrefs = data.get("txrefs", [])
            if txrefs:
                last_hash = txrefs[0].get("tx_hash")
        watched_addresses[address] = {
            "channel_id": interaction.channel_id,
            "last_tx_hash": last_hash,
        }

    embed = build_invoice_embed(inv)

    # Generate QR code
    qr_buf = make_qr_image(address, amount)
    file = discord.File(qr_buf, filename="invoice_qr.png")
    embed.set_image(url="attachment://invoice_qr.png")

    await interaction.followup.send(embed=embed, file=file)

    # DM the creator too
    try:
        qr_buf2 = make_qr_image(address, amount)
        file2 = discord.File(qr_buf2, filename="invoice_qr.png")
        embed2 = build_invoice_embed(inv)
        embed2.set_image(url="attachment://invoice_qr.png")
        embed2.set_footer(text="Invoice sent to your DMs")
        await interaction.user.send(embed=embed2, file=file2)
    except Exception:
        pass


@bot.tree.command(name="invoicestatus", description="Check status of an invoice")
@app_commands.describe(invoice_id="Invoice ID (e.g. 0001)")
async def invoicestatus(interaction: discord.Interaction, invoice_id: str):
    inv = invoice_store.get(invoice_id.zfill(4))
    if not inv:
        embed = discord.Embed(
            title="❌ Invoice Not Found",
            description=f"No invoice with ID `{invoice_id}` found.",
            color=C_RED
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = build_invoice_embed(inv)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ltcstats", description="Show Litecoin network stats")
async def ltcstats(interaction: discord.Interaction):
    await interaction.response.defer()
    data = await get_network_stats()
    if not data:
        await interaction.followup.send("❌ Could not fetch network stats.", ephemeral=True)
        return

    embed = discord.Embed(
        title="⛏️ Litecoin Network Stats",
        color=C_LTC,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name="Litecoin Network",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.add_field(name="📦 Block Height",     value=f"`{data.get('blocks', 'N/A'):,}`",              inline=True)
    embed.add_field(name="🔗 Network",          value=f"`{data.get('network', 'LTC')}`",               inline=True)
    embed.add_field(name="⛏️ Difficulty",       value=f"`{float(data.get('difficulty', 0)):,.2f}`",    inline=True)
    embed.add_field(name="💸 TX Fee (avg)",     value=f"`{data.get('price', 'N/A')} USD/LTC`",         inline=True)
    embed.add_field(name="🏷️ Hashrate",         value=f"`{data.get('hashrate', 'N/A')}`",              inline=True)
    embed.add_field(name="⏱️ Unconfirmed TXs", value=f"`{data.get('unconfirmed_txs', 'N/A')}`",       inline=True)
    embed.set_footer(text="Data from Sochain API • No API key required")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="help", description="Show all available bot commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🪙 LTC Bot — Command Help",
        description="A Litecoin transaction monitor & invoice bot.",
        color=C_LTC,
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(
        name="LTC Bot",
        icon_url="https://cryptologos.cc/logos/litecoin-ltc-logo.png"
    )
    embed.add_field(
        name="🔍 Transaction Commands",
        value=(
            "`/checktx <txid>` — Look up any LTC transaction\n"
            "`/watch <address>` — Auto-watch an address for new TXs\n"
            "`/unwatch <address>` — Stop watching an address\n"
            "`/watchlist` — See all watched addresses & TXs"
        ),
        inline=False
    )
    embed.add_field(
        name="🧾 Invoice Commands",
        value=(
            "`/invoice <address> <amount> [desc]` — Create a payment invoice with QR\n"
            "`/invoicestatus <id>` — Check invoice payment status"
        ),
        inline=False
    )
    embed.add_field(
        name="📡 Network Commands",
        value="`/ltcstats` — Show live Litecoin network stats",
        inline=False
    )
    embed.add_field(
        name="🔔 Auto-Notifications",
        value=(
            f"You'll receive DMs at **1, 3, and {REQUIRED_CONFS} confirmations**\n"
            f"All watched addresses are polled every **{POLL_INTERVAL} seconds**"
        ),
        inline=False
    )
    embed.set_footer(text="Powered by Sochain API • No API key required")
    await interaction.response.send_message(embed=embed)


# ─────────────────────────────────────────────────────────
# BOT EVENTS
# ─────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    poll_addresses.start()
    poll_txids.start()

    # Start watching default address if configured
    if WATCH_ADDRESS:
        data = await get_address_txs(WATCH_ADDRESS)
        last_hash = None
        if data:
            txrefs = data.get("txrefs", [])
            if txrefs:
                last_hash = txrefs[0].get("tx_hash")
        watched_addresses[WATCH_ADDRESS] = {
            "channel_id": None,
            "last_tx_hash": last_hash,
        }
        print(f"👁️ Auto-watching address: {WATCH_ADDRESS}")

    print(f"🔄 Polling every {POLL_INTERVAL}s | Required confirmations: {REQUIRED_CONFS}")


bot.run(BOT_TOKEN)
