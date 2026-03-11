"""
╔══════════════════════════════════════════════════╗
║        LTC Discord Bot  •  Python 3.13           ║
║   Auto-detect transactions • DM notifications    ║
║   /checktx • /watch • /invoice • /ltcstats       ║
║   Powered by Sochain API (no key needed)         ║
╚══════════════════════════════════════════════════╝
"""

import os
import io
import asyncio
from datetime import datetime, timezone

import aiohttp
import nextcord
from nextcord.ext import commands, tasks
import qrcode

# ──────────────────────────────────────────────────────────────
# CONFIG  ·  All via Railway environment variables
# ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
NOTIFY_USER_ID = int(os.environ.get("NOTIFY_USER_ID", "0"))
WATCH_ADDRESS  = os.environ.get("LTC_WATCH_ADDRESS", "")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "30"))
REQUIRED_CONFS = int(os.environ.get("REQUIRED_CONFS", "6"))

SOCHAIN = "https://sochain.com/api/v3"

C_LTC    = 0x345D9D
C_GREEN  = 0x2ECC71
C_RED    = 0xE74C3C
C_ORANGE = 0xF39C12
C_GREY   = 0x95A5A6
LTC_ICON = "https://cryptologos.cc/logos/litecoin-ltc-logo.png"

# ──────────────────────────────────────────────────────────────
# BOT
# ──────────────────────────────────────────────────────────────
intents = nextcord.Intents.default()
bot = commands.Bot(intents=intents)

watched_addresses: dict = {}
watched_txids:     dict = {}
invoices:          dict = {}
invoice_seq              = 0


# ──────────────────────────────────────────────────────────────
# API HELPERS
# ──────────────────────────────────────────────────────────────
async def api_get(url: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    body = await r.json()
                    return body.get("data", body)
    except Exception as e:
        print(f"[API] {e}")
    return None


async def fetch_tx(txid: str) -> dict | None:
    raw = await api_get(f"{SOCHAIN}/transaction/LTC/{txid}")
    if not raw:
        return None
    outputs = [
        {"addresses": [o["address"]] if o.get("address") else [], "value_ltc": float(o.get("value", 0))}
        for o in raw.get("outputs", [])
    ]
    return {
        "hash":          raw.get("hash", txid),
        "confirmations": int(raw.get("confirmations", 0)),
        "total_ltc":     sum(o["value_ltc"] for o in outputs),
        "fee_ltc":       float(raw.get("fee", 0)),
        "size":          raw.get("size", 0),
        "time":          raw.get("time", ""),
        "inputs":        [{"addresses": [i["address"]] if i.get("address") else []} for i in raw.get("inputs", [])],
        "outputs":       outputs,
    }


async def fetch_latest_tx_hash(address: str) -> str | None:
    raw = await api_get(f"{SOCHAIN}/transactions/LTC/{address}/1")
    if raw:
        txs = raw.get("transactions", [])
        if txs:
            return txs[0].get("hash") or txs[0].get("txid")
    return None


async def fetch_network() -> dict | None:
    return await api_get(f"{SOCHAIN}/info/LTC")


# ──────────────────────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────────────────────
def conf_bar(confs: int) -> str:
    f = min(confs, REQUIRED_CONFS)
    return f"`{'█'*f}{'░'*(REQUIRED_CONFS-f)}` {confs}/{REQUIRED_CONFS}"

def conf_color(c: int) -> int:
    return C_ORANGE if c == 0 else (C_GREEN if c >= REQUIRED_CONFS else C_LTC)

def status_label(c: int) -> str:
    if c == 0:              return "⏳ Unconfirmed (mempool)"
    if c < 3:               return "🔄 Confirming…"
    if c < REQUIRED_CONFS: return "✅ Partially Confirmed"
    return "🔒 Fully Confirmed"

def fmt_time(raw) -> str:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(raw) or "Pending"

def make_qr(address: str, amount: float) -> io.BytesIO:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(f"litecoin:{address}?amount={amount}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#345D9D", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ──────────────────────────────────────────────────────────────
# EMBED BUILDERS
# ──────────────────────────────────────────────────────────────
def tx_embed(tx: dict, title: str = "🔍 Transaction Details") -> nextcord.Embed:
    confs = tx["confirmations"]
    txid  = tx["hash"]

    senders = []
    for i in tx["inputs"][:3]:
        senders += i["addresses"]
    sender_str = "\n".join(f"`{a}`" for a in senders[:3]) or "Coinbase / Unknown"
    if len(tx["inputs"]) > 3:
        sender_str += f"\n*+{len(tx['inputs'])-3} more*"

    recv_lines = []
    for o in tx["outputs"][:3]:
        for addr in o["addresses"]:
            recv_lines.append(f"`{addr}` — **{o['value_ltc']:.6f} LTC**")
    receiver_str = "\n".join(recv_lines[:3]) or "Unknown"
    if len(tx["outputs"]) > 3:
        receiver_str += f"\n*+{len(tx['outputs'])-3} more*"

    embed = nextcord.Embed(title=title, url=f"https://sochain.com/tx/LTC/{txid}",
                           color=conf_color(confs), timestamp=datetime.now(timezone.utc))
    embed.set_author(name="Litecoin Network", icon_url=LTC_ICON)
    embed.add_field(name="🆔 TXID",    value=f"```{txid}```",                             inline=False)
    embed.add_field(name="💰 Amount",  value=f"**{tx['total_ltc']:.8f} LTC**",            inline=True)
    embed.add_field(name="⛽ Fee",     value=f"{tx['fee_ltc']:.8f} LTC",                  inline=True)
    embed.add_field(name="📦 Size",    value=f"{tx['size']} bytes",                       inline=True)
    embed.add_field(name="📊 Status",  value=f"{status_label(confs)}\n{conf_bar(confs)}", inline=False)
    embed.add_field(name="📤 From",    value=sender_str,                                  inline=True)
    embed.add_field(name="📥 To",      value=receiver_str,                                inline=True)
    embed.add_field(name="🕐 Time",    value=fmt_time(tx["time"]),                        inline=False)
    embed.set_footer(text="Sochain Explorer", icon_url=LTC_ICON)
    return embed


def invoice_embed(inv: dict) -> nextcord.Embed:
    labels = {"pending": ("⏳ Awaiting Payment", C_ORANGE), "paid": ("✅ Paid", C_GREEN), "expired": ("❌ Expired", C_RED)}
    label, color = labels.get(inv["status"], ("❓ Unknown", C_GREY))
    embed = nextcord.Embed(title=f"🧾 Invoice #{inv['id']}", description=f"**{inv.get('description','Litecoin Payment')}**",
                           color=color, timestamp=datetime.now(timezone.utc))
    embed.set_author(name="Litecoin Invoice", icon_url=LTC_ICON)
    embed.add_field(name="💎 Amount", value=f"**{inv['amount']} LTC**", inline=True)
    embed.add_field(name="📌 Status", value=label,                      inline=True)
    embed.add_field(name="🔢 ID",     value=f"`#{inv['id']}`",          inline=True)
    embed.add_field(name="📬 Pay To", value=f"```{inv['address']}```",  inline=False)
    if inv.get("txid"):
        embed.add_field(name="🔗 TXID", value=f"```{inv['txid']}```",  inline=False)
    embed.add_field(name="📋 Instructions",
                    value=f"Send exactly **{inv['amount']} LTC** to the address above.\nThe bot will auto-detect your payment.",
                    inline=False)
    embed.set_footer(text=f"Created by {inv['creator']}")
    return embed


# ──────────────────────────────────────────────────────────────
# NOTIFICATION HELPERS
# ──────────────────────────────────────────────────────────────
async def dm_user(user_id: int, embed: nextcord.Embed, file: nextcord.File | None = None):
    if not user_id:
        return
    try:
        user = await bot.fetch_user(user_id)
        kwargs = {"embed": embed}
        if file:
            kwargs["file"] = file
        await user.send(**kwargs)
    except Exception as e:
        print(f"[DM] {user_id}: {e}")


async def notify(embed: nextcord.Embed, channel_id: int | None, user_id: int | None):
    if channel_id:
        try:
            ch = bot.get_channel(channel_id)
            if ch:
                await ch.send(embed=embed)
        except Exception as e:
            print(f"[Notify] {e}")
    if user_id:
        await dm_user(user_id, embed)


# ──────────────────────────────────────────────────────────────
# BACKGROUND TASKS
# ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=POLL_INTERVAL)
async def poll_addresses():
    for address, state in list(watched_addresses.items()):
        try:
            latest = await fetch_latest_tx_hash(address)
            if not latest or state.get("last_tx_hash") == latest:
                continue
            watched_addresses[address]["last_tx_hash"] = latest
            tx = await fetch_tx(latest)
            if not tx:
                continue
            embed = tx_embed(tx, title="🚨 New Incoming Transaction!")
            embed.color = C_ORANGE
            embed.insert_field_at(0, name="📍 Watched Address", value=f"`{address}`", inline=False)
            await notify(embed, state.get("channel_id"), NOTIFY_USER_ID)
            if latest not in watched_txids:
                watched_txids[latest] = {
                    "channel_id": state.get("channel_id"),
                    "user_id":    NOTIFY_USER_ID,
                    "last_confs": tx["confirmations"],
                    "done":       tx["confirmations"] >= REQUIRED_CONFS,
                }
        except Exception as e:
            print(f"[Poll Addr] {address}: {e}")


@tasks.loop(seconds=POLL_INTERVAL)
async def poll_txids():
    for txid, state in list(watched_txids.items()):
        if state.get("done"):
            continue
        try:
            tx = await fetch_tx(txid)
            if not tx:
                continue
            confs = tx["confirmations"]
            prev  = state["last_confs"]
            watched_txids[txid]["last_confs"] = confs
            for milestone in [1, 3, REQUIRED_CONFS]:
                if prev < milestone <= confs:
                    if confs >= REQUIRED_CONFS:
                        embed = nextcord.Embed(
                            title="🔒 Transaction Fully Confirmed!",
                            description=f"Reached **{confs} confirmations** — fully settled.",
                            url=f"https://sochain.com/tx/LTC/{txid}",
                            color=C_GREEN, timestamp=datetime.now(timezone.utc))
                        embed.set_author(name="Litecoin Network", icon_url=LTC_ICON)
                        embed.add_field(name="🆔 TXID",          value=f"```{txid}```", inline=False)
                        embed.add_field(name="📊 Confirmations", value=conf_bar(confs), inline=False)
                        watched_txids[txid]["done"] = True
                    else:
                        embed = tx_embed(tx, title=f"🔄 {confs} Confirmation{'s' if confs!=1 else ''}")
                    await notify(embed, state.get("channel_id"), state.get("user_id"))
                    break
        except Exception as e:
            print(f"[Poll TX] {txid[:16]}: {e}")


# ──────────────────────────────────────────────────────────────
# SLASH COMMANDS
# ──────────────────────────────────────────────────────────────
@bot.slash_command(name="checktx", description="Look up a Litecoin transaction by TXID")
async def checktx(
    interaction: nextcord.Interaction,
    txid: str = nextcord.SlashOption(description="64-character hex transaction ID"),
):
    await interaction.response.defer()
    txid = txid.strip().lower()
    if len(txid) != 64 or not all(c in "0123456789abcdef" for c in txid):
        await interaction.followup.send(embed=nextcord.Embed(title="❌ Invalid TXID",
            description="Must be a 64-character hex string.", color=C_RED))
        return
    tx = await fetch_tx(txid)
    if not tx:
        await interaction.followup.send(embed=nextcord.Embed(title="❌ Not Found",
            description=f"No transaction found for:\n```{txid}```", color=C_RED))
        return
    embed = tx_embed(tx)
    if tx["confirmations"] < REQUIRED_CONFS:
        watched_txids[txid] = {"channel_id": interaction.channel_id, "user_id": interaction.user.id,
                               "last_confs": tx["confirmations"], "done": False}
        embed.set_footer(text=f"👁️ Watching — DM at 1, 3 & {REQUIRED_CONFS} confs | Sochain")
    await interaction.followup.send(embed=embed)
    await dm_user(interaction.user.id, embed)


@bot.slash_command(name="watch", description="Watch a Litecoin address for new incoming transactions")
async def watch(
    interaction: nextcord.Interaction,
    address: str = nextcord.SlashOption(description="LTC address (starts with L, M or ltc1)"),
):
    await interaction.response.defer(ephemeral=True)
    address = address.strip()
    if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
        await interaction.followup.send(embed=nextcord.Embed(title="❌ Invalid Address",
            description="Please provide a valid Litecoin address.", color=C_RED), ephemeral=True)
        return
    last = await fetch_latest_tx_hash(address)
    watched_addresses[address] = {"channel_id": interaction.channel_id, "last_tx_hash": last}
    embed = nextcord.Embed(title="👁️ Now Watching", color=C_LTC, timestamp=datetime.now(timezone.utc))
    embed.set_author(name="Litecoin Monitor", icon_url=LTC_ICON)
    embed.add_field(name="📬 Address",       value=f"```{address}```",                                                         inline=False)
    embed.add_field(name="🔔 Notifications", value=f"DM on new TX\nConf updates at 1 → 3 → {REQUIRED_CONFS}", inline=False)
    embed.set_footer(text=f"Polling every {POLL_INTERVAL}s • Sochain API")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.slash_command(name="unwatch", description="Stop watching a Litecoin address")
async def unwatch(
    interaction: nextcord.Interaction,
    address: str = nextcord.SlashOption(description="LTC address to stop monitoring"),
):
    address = address.strip()
    if address in watched_addresses:
        del watched_addresses[address]
        embed = nextcord.Embed(title="🛑 Stopped Watching", description=f"`{address}`", color=C_GREY)
    else:
        embed = nextcord.Embed(title="❓ Not Watching", description=f"`{address}` was not being watched.", color=C_RED)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.slash_command(name="watchlist", description="Show all watched addresses and transactions")
async def watchlist(interaction: nextcord.Interaction):
    if not watched_addresses and not watched_txids:
        await interaction.response.send_message(embed=nextcord.Embed(title="📭 Watch List Empty",
            description="Use `/watch <address>` to start.", color=C_GREY), ephemeral=True)
        return
    embed = nextcord.Embed(title="👁️ Watch List", color=C_LTC, timestamp=datetime.now(timezone.utc))
    if watched_addresses:
        embed.add_field(name=f"📬 Addresses ({len(watched_addresses)})",
                        value="\n".join(f"• `{a}`" for a in list(watched_addresses)[:15]), inline=False)
    active = {k: v for k, v in watched_txids.items() if not v.get("done")}
    if active:
        embed.add_field(name=f"🔗 Active TXs ({len(active)})",
                        value="\n".join(f"• `{t[:20]}…` — {s['last_confs']} conf(s)" for t, s in list(active.items())[:10]),
                        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.slash_command(name="invoice", description="Create a Litecoin payment invoice with QR code")
async def invoice(
    interaction: nextcord.Interaction,
    address: str   = nextcord.SlashOption(description="Your LTC receiving address"),
    amount:  float = nextcord.SlashOption(description="Amount in LTC"),
    description: str = nextcord.SlashOption(description="What the payment is for", required=False, default="Litecoin Payment"),
):
    await interaction.response.defer()
    global invoice_seq
    if amount <= 0:
        await interaction.followup.send("❌ Amount must be greater than 0.", ephemeral=True)
        return
    invoice_seq += 1
    inv = {"id": f"{invoice_seq:04d}", "address": address.strip(), "amount": round(amount, 8),
           "description": description, "status": "pending", "txid": None,
           "creator": str(interaction.user), "channel_id": interaction.channel_id, "user_id": interaction.user.id}
    invoices[inv["id"]] = inv
    if address not in watched_addresses:
        last = await fetch_latest_tx_hash(address)
        watched_addresses[address] = {"channel_id": interaction.channel_id, "last_tx_hash": last}
    embed = invoice_embed(inv)
    qr_buf = make_qr(address, amount)
    file   = nextcord.File(qr_buf, filename="qr.png")
    embed.set_image(url="attachment://qr.png")
    await interaction.followup.send(embed=embed, file=file)
    try:
        embed2 = invoice_embed(inv)
        qr2    = make_qr(address, amount)
        file2  = nextcord.File(qr2, filename="qr.png")
        embed2.set_image(url="attachment://qr.png")
        await interaction.user.send(embed=embed2, file=file2)
    except Exception:
        pass


@bot.slash_command(name="invoicestatus", description="Check the status of an invoice")
async def invoicestatus(
    interaction: nextcord.Interaction,
    invoice_id: str = nextcord.SlashOption(description="Invoice ID e.g. 0001"),
):
    inv = invoices.get(invoice_id.zfill(4))
    if not inv:
        await interaction.response.send_message(embed=nextcord.Embed(title="❌ Not Found",
            description=f"No invoice `{invoice_id}`.", color=C_RED), ephemeral=True)
        return
    await interaction.response.send_message(embed=invoice_embed(inv))


@bot.slash_command(name="ltcstats", description="Show live Litecoin network stats")
async def ltcstats(interaction: nextcord.Interaction):
    await interaction.response.defer()
    data = await fetch_network()
    if not data:
        await interaction.followup.send("❌ Could not reach Sochain API.", ephemeral=True)
        return
    embed = nextcord.Embed(title="⛏️ Litecoin Network Stats", color=C_LTC, timestamp=datetime.now(timezone.utc))
    embed.set_author(name="Litecoin Network", icon_url=LTC_ICON)
    blocks = data.get("blocks", "N/A")
    embed.add_field(name="📦 Block Height",    value=f"`{blocks:,}`" if isinstance(blocks, int) else f"`{blocks}`", inline=True)
    embed.add_field(name="🌐 Network",         value=f"`{data.get('network','LTC')}`",                              inline=True)
    try:
        embed.add_field(name="⛏️ Difficulty",  value=f"`{float(data.get('difficulty',0)):,.0f}`",                  inline=True)
    except Exception:
        embed.add_field(name="⛏️ Difficulty",  value=f"`{data.get('difficulty','N/A')}`",                          inline=True)
    embed.add_field(name="💵 Price",           value=f"`${data.get('price','N/A')} USD`",                          inline=True)
    embed.add_field(name="⏱️ Unconfirmed TXs", value=f"`{data.get('unconfirmed_txs','N/A')}`",                    inline=True)
    embed.add_field(name="🔗 Hashrate",        value=f"`{data.get('hashrate','N/A')}`",                            inline=True)
    embed.set_footer(text="Sochain API • No key required")
    await interaction.followup.send(embed=embed)


@bot.slash_command(name="help", description="Show all bot commands")
async def help_cmd(interaction: nextcord.Interaction):
    embed = nextcord.Embed(title="🪙 LTC Bot — Help",
        description="Litecoin transaction monitor, watcher & invoice bot.",
        color=C_LTC, timestamp=datetime.now(timezone.utc))
    embed.set_author(name="LTC Bot", icon_url=LTC_ICON)
    embed.add_field(name="🔍 Transactions", value=(
        "`/checktx <txid>` — Look up any LTC transaction\n"
        "`/watch <addr>` — Watch address for new TXs\n"
        "`/unwatch <addr>` — Stop watching\n"
        "`/watchlist` — View all watched addresses & TXs"), inline=False)
    embed.add_field(name="🧾 Invoices", value=(
        "`/invoice <addr> <amount> [desc]` — Create invoice + QR code\n"
        "`/invoicestatus <id>` — Check invoice status"), inline=False)
    embed.add_field(name="📡 Network", value="`/ltcstats` — Live Litecoin network stats", inline=False)
    embed.add_field(name="🔔 Auto Notifications", value=(
        f"DM alerts on new incoming TXs\n"
        f"Confirmation DMs at **1 → 3 → {REQUIRED_CONFS}** confs\n"
        f"Polls every **{POLL_INTERVAL}s**"), inline=False)
    embed.set_footer(text="Powered by Sochain API • No API key needed")
    await interaction.response.send_message(embed=embed)


# ──────────────────────────────────────────────────────────────
# EVENTS
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅  {bot.user}  (ID: {bot.user.id})")
    print(f"🔄  Polling every {POLL_INTERVAL}s | Confs required: {REQUIRED_CONFS}")
    if not poll_addresses.is_running():
        poll_addresses.start()
    if not poll_txids.is_running():
        poll_txids.start()
    if WATCH_ADDRESS:
        last = await fetch_latest_tx_hash(WATCH_ADDRESS)
        watched_addresses[WATCH_ADDRESS] = {"channel_id": None, "last_tx_hash": last}
        print(f"👁️  Auto-watching: {WATCH_ADDRESS}")


bot.run(BOT_TOKEN)
