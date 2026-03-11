"""
LTC Discord Bot  •  Python 3.13  •  hikari
- Auto-detects transactions on watched addresses
- DM notifications with LTC + USD amounts
- Balance high watermark alerts
- /checktx /watch /invoice /convert /ltcstats
- Works as User App (anywhere in Discord)
"""

import os, io, asyncio, traceback
from datetime import datetime, timezone

import aiohttp
import hikari
import qrcode

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
NOTIFY_USER_ID = int(os.environ.get("NOTIFY_USER_ID", "0"))
WATCH_ADDRESS  = os.environ.get("LTC_WATCH_ADDRESS", "")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "30"))
REQUIRED_CONFS = int(os.environ.get("REQUIRED_CONFS", "6"))
GUILD_ID       = int(os.environ.get("GUILD_ID", "0"))

BLOCKCHAIR = "https://api.blockchair.com/litecoin"
LTC_ICON   = "https://cryptologos.cc/logos/litecoin-ltc-logo.png"
C_LTC    = 0x345D9D
C_GREEN  = 0x2ECC71
C_RED    = 0xE74C3C
C_ORANGE = 0xF39C12
C_GREY   = 0x95A5A6
C_GOLD   = 0xF1C40F

# ──────────────────────────────────────────────────────────────
# BOT  — USER INSTALL CONTEXT FLAGS
# ──────────────────────────────────────────────────────────────
bot = hikari.GatewayBot(
    token=BOT_TOKEN,
    intents=(
        hikari.Intents.GUILDS
        | hikari.Intents.GUILD_MESSAGES
        | hikari.Intents.DM_MESSAGES
        | hikari.Intents.MESSAGE_CONTENT
        | hikari.Intents.GUILD_MEMBERS
    ),
    logs="INFO",
)

watched_addresses: dict = {}   # addr → {last_tx_hash, channel_id, high_watermark_ltc}
watched_txids:     dict = {}
invoices:          dict = {}
invoice_seq              = 0
ltc_price_usd: float     = 0.0  # cached price

# ──────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────
async def api_get(url: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.json()
                print(f"[API] HTTP {r.status} → {url}")
    except Exception as e:
        print(f"[API] {e}")
    return None

async def get_ltc_price() -> float:
    raw = await api_get("https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd")
    if raw:
        return float(raw.get("litecoin", {}).get("usd", 0))
    return 0.0

async def fetch_tx(txid: str) -> dict | None:
    raw = await api_get(f"{BLOCKCHAIR}/dashboards/transaction/{txid}")
    if not raw:
        return None
    tx_data = raw.get("data", {}).get(txid, {})
    tx      = tx_data.get("transaction", {})
    inputs  = tx_data.get("inputs", [])
    outputs = tx_data.get("outputs", [])
    in_addrs  = [{"addresses": [i["recipient"]] if i.get("recipient") else []} for i in inputs]
    out_addrs = [{"addresses": [o["recipient"]] if o.get("recipient") else [],
                  "value_ltc": o.get("value", 0) / 1e8} for o in outputs]
    confs = tx.get("block_id", -1)
    if confs == -1:
        confs = 0
    else:
        tip   = raw.get("context", {}).get("state", confs)
        confs = max(0, tip - confs + 1)
    return {
        "hash":          txid,
        "confirmations": confs,
        "total_ltc":     tx.get("output_total", 0) / 1e8,
        "fee_ltc":       tx.get("fee", 0) / 1e8,
        "size":          tx.get("size", 0),
        "time":          tx.get("time", ""),
        "inputs":        in_addrs,
        "outputs":       out_addrs,
    }

async def fetch_address(address: str) -> dict | None:
    raw = await api_get(f"{BLOCKCHAIR}/dashboards/address/{address}")
    if not raw:
        return None
    return raw.get("data", {}).get(address, {})

async def fetch_latest_tx_hash(address: str) -> str | None:
    data = await fetch_address(address)
    if data:
        txs = data.get("transactions", [])
        return txs[0] if txs else None
    return None

async def fetch_address_balance(address: str) -> float:
    data = await fetch_address(address)
    if data:
        addr = data.get("address", {})
        return addr.get("balance", 0) / 1e8
    return 0.0

async def fetch_network() -> dict | None:
    raw = await api_get(f"{BLOCKCHAIR}/stats")
    if raw:
        return raw.get("data", {})
    return None

# ──────────────────────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────────────────────
def ltc_usd(ltc: float) -> str:
    if ltc_price_usd:
        return f"{ltc:.8f} LTC (${ltc * ltc_price_usd:,.2f} USD)"
    return f"{ltc:.8f} LTC"

def conf_bar(c: int) -> str:
    f = min(c, REQUIRED_CONFS)
    return f"`{'█'*f}{'░'*(REQUIRED_CONFS-f)}` {c}/{REQUIRED_CONFS}"

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

def make_qr(address: str, amount: float) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(f"litecoin:{address}?amount={amount}")
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#345D9D", back_color="white").save(buf, format="PNG")
    return buf.getvalue()

# ──────────────────────────────────────────────────────────────
# EMBEDS
# ──────────────────────────────────────────────────────────────
def tx_embed(tx: dict, title: str = "🔍 Transaction Details") -> hikari.Embed:
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
            recv_lines.append(f"`{addr}`\n**{ltc_usd(o['value_ltc'])}**")
    receiver_str = "\n".join(recv_lines[:3]) or "Unknown"
    if len(tx["outputs"]) > 3:
        receiver_str += f"\n*+{len(tx['outputs'])-3} more*"
    return (
        hikari.Embed(title=title,
                     url=f"https://blockchair.com/litecoin/transaction/{txid}",
                     color=conf_color(confs), timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Network", icon=LTC_ICON)
        .add_field("🆔 TXID",   f"```{txid}```",                             inline=False)
        .add_field("💰 Amount", f"**{ltc_usd(tx['total_ltc'])}**",           inline=True)
        .add_field("⛽ Fee",    ltc_usd(tx['fee_ltc']),                      inline=True)
        .add_field("📦 Size",   f"{tx['size']} bytes",                       inline=True)
        .add_field("📊 Status", f"{status_label(confs)}\n{conf_bar(confs)}", inline=False)
        .add_field("📤 From",   sender_str,                                  inline=True)
        .add_field("📥 To",     receiver_str,                                inline=True)
        .add_field("🕐 Time",   fmt_time(tx["time"]),                        inline=False)
        .set_footer(text=f"1 LTC = ${ltc_price_usd:,.2f} USD | Blockchair", icon=LTC_ICON)
    )

def invoice_embed(inv: dict) -> hikari.Embed:
    labels = {"pending": ("⏳ Awaiting Payment", C_ORANGE), "paid": ("✅ Paid", C_GREEN), "expired": ("❌ Expired", C_RED)}
    label, color = labels.get(inv["status"], ("❓ Unknown", C_GREY))
    usd_val = f" (${inv['amount'] * ltc_price_usd:,.2f} USD)" if ltc_price_usd else ""
    e = (
        hikari.Embed(title=f"🧾 Invoice #{inv['id']}",
                     description=f"**{inv.get('description','Litecoin Payment')}**",
                     color=color, timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Invoice", icon=LTC_ICON)
        .add_field("💎 Amount", f"**{inv['amount']} LTC{usd_val}**", inline=True)
        .add_field("📌 Status", label,                               inline=True)
        .add_field("🔢 ID",     f"`#{inv['id']}`",                   inline=True)
        .add_field("📬 Pay To", f"```{inv['address']}```",           inline=False)
        .add_field("📋 Instructions",
                   f"Send exactly **{inv['amount']} LTC** to the address above.\nThe bot will auto-detect your payment.",
                   inline=False)
        .set_footer(text=f"1 LTC = ${ltc_price_usd:,.2f} USD")
    )
    if inv.get("txid"):
        e.add_field("🔗 TXID", f"```{inv['txid']}```", inline=False)
    return e

# ──────────────────────────────────────────────────────────────
# DM / NOTIFY
# ──────────────────────────────────────────────────────────────
async def dm_user(user_id: int, embed: hikari.Embed, attachment: bytes | None = None):
    if not user_id:
        print("[DM] NOTIFY_USER_ID not set!")
        return
    try:
        print(f"[DM] Sending to {user_id}...")
        dm = await bot.rest.create_dm_channel(user_id)
        kwargs: dict = {"embed": embed}
        if attachment:
            kwargs["attachment"] = hikari.Bytes(attachment, "qr.png")
        await bot.rest.create_message(dm.id, **kwargs)
        print(f"[DM] ✅ Sent to {user_id}")
    except Exception as e:
        print(f"[DM] ❌ {e}")
        traceback.print_exc()

async def notify(embed: hikari.Embed, channel_id: int | None, user_id: int | None):
    if channel_id:
        try:
            await bot.rest.create_message(channel_id, embed=embed)
        except Exception as e:
            print(f"[Notify channel] {e}")
    if user_id:
        await dm_user(user_id, embed)

# ──────────────────────────────────────────────────────────────
# POLLING
# ──────────────────────────────────────────────────────────────
async def poll_loop():
    global ltc_price_usd
    while True:
        await asyncio.sleep(POLL_INTERVAL)

        # Refresh LTC price every poll
        price = await get_ltc_price()
        if price:
            ltc_price_usd = price

        # Poll addresses
        for address, state in list(watched_addresses.items()):
            try:
                latest = await fetch_latest_tx_hash(address)
                if not latest or state.get("last_tx_hash") == latest:
                    # No new TX — check balance high watermark
                    balance = await fetch_address_balance(address)
                    prev_high = state.get("high_watermark_ltc", 0.0)
                    if balance > prev_high:
                        watched_addresses[address]["high_watermark_ltc"] = balance
                        usd_val = f"${balance * ltc_price_usd:,.2f} USD" if ltc_price_usd else ""
                        embed = (
                            hikari.Embed(
                                title="🏆 New Balance High!",
                                description=f"Your wallet **`{address[:16]}…`** has reached a new all-time high balance!",
                                color=C_GOLD, timestamp=datetime.now(timezone.utc))
                            .set_author(name="Litecoin Balance Alert", icon=LTC_ICON)
                            .add_field("💰 New High",    f"**{balance:.8f} LTC**",  inline=True)
                            .add_field("💵 In USD",      f"**{usd_val}**",          inline=True)
                            .add_field("📬 Address",     f"`{address}`",             inline=False)
                            .set_footer(text=f"1 LTC = ${ltc_price_usd:,.2f} USD")
                        )
                        await dm_user(NOTIFY_USER_ID, embed)
                    continue

                watched_addresses[address]["last_tx_hash"] = latest
                tx = await fetch_tx(latest)
                if not tx:
                    continue

                # Update balance + watermark
                balance = await fetch_address_balance(address)
                watched_addresses[address]["high_watermark_ltc"] = max(
                    balance, state.get("high_watermark_ltc", 0.0))

                embed = tx_embed(tx, "🚨 New Incoming Transaction!")
                embed.color = C_ORANGE
                embed.add_field("📍 Watched Address", f"`{address}`",        inline=False)
                embed.add_field("💼 New Balance",     f"**{ltc_usd(balance)}**", inline=False)
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

        # Poll txids for confirmations
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
                            embed = (
                                hikari.Embed(
                                    title="🔒 Transaction Fully Confirmed!",
                                    description=f"Reached **{confs} confirmations** — fully settled on Litecoin.",
                                    url=f"https://blockchair.com/litecoin/transaction/{txid}",
                                    color=C_GREEN, timestamp=datetime.now(timezone.utc))
                                .set_author(name="Litecoin Network", icon=LTC_ICON)
                                .add_field("🆔 TXID",          f"```{txid}```",           inline=False)
                                .add_field("💰 Amount",        f"**{ltc_usd(tx['total_ltc'])}**", inline=True)
                                .add_field("📊 Confirmations", conf_bar(confs),           inline=False)
                                .set_footer(text=f"1 LTC = ${ltc_price_usd:,.2f} USD")
                            )
                            watched_txids[txid]["done"] = True
                        else:
                            embed = tx_embed(tx, f"🔄 {confs} Confirmation{'s' if confs!=1 else ''}")
                        await notify(embed, state.get("channel_id"), state.get("user_id"))
                        break
            except Exception as e:
                print(f"[Poll TX] {txid[:16]}: {e}")

# ──────────────────────────────────────────────────────────────
# COMMAND REGISTRATION
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartingEvent)
async def on_starting(event: hikari.StartingEvent) -> None:
    app = await bot.rest.fetch_application()
    commands = [
        bot.rest.slash_command_builder("checktx", "Look up a Litecoin transaction by TXID")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="txid",
                description="64-character hex transaction ID", is_required=True)),

        bot.rest.slash_command_builder("watch", "Watch a Litecoin address for incoming transactions")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address (starts with L, M or ltc1)", is_required=True)),

        bot.rest.slash_command_builder("unwatch", "Stop watching a Litecoin address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address to stop monitoring", is_required=True)),

        bot.rest.slash_command_builder("watchlist", "Show all watched addresses and transactions"),

        bot.rest.slash_command_builder("invoice", "Create a Litecoin payment invoice with QR code")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="Your LTC receiving address", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="amount",
                description="Amount in LTC", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="description",
                description="What the payment is for", is_required=False)),

        bot.rest.slash_command_builder("invoicestatus", "Check the status of an invoice")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="invoice_id",
                description="Invoice ID e.g. 0001", is_required=True)),

        bot.rest.slash_command_builder("convert", "Convert between LTC and USD")
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="amount",
                description="Amount to convert", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="from_currency",
                description="Currency to convert FROM", is_required=True,
                choices=[hikari.CommandChoice(name="LTC → USD", value="ltc"),
                         hikari.CommandChoice(name="USD → LTC", value="usd")])),

        bot.rest.slash_command_builder("balance", "Check balance of a Litecoin address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address to check", is_required=True)),

        bot.rest.slash_command_builder("ltcstats", "Show live Litecoin network stats"),
        bot.rest.slash_command_builder("help", "Show all bot commands"),
    ]

    if GUILD_ID:
        await bot.rest.set_application_commands(application=app.id, guild=GUILD_ID, commands=commands)
        print(f"✅ Commands registered to guild {GUILD_ID} (instant)")
    else:
        await bot.rest.set_application_commands(application=app.id, commands=commands)
        print("✅ Commands registered globally")

# ──────────────────────────────────────────────────────────────
# INTERACTION HANDLER
# ──────────────────────────────────────────────────────────────
def opt(interaction: hikari.CommandInteraction, name: str):
    for o in (interaction.options or []):
        if o.name == name:
            return o.value
    return None

async def respond(interaction: hikari.CommandInteraction, embed: hikari.Embed,
                  attachment: bytes | None = None, ephemeral: bool = False):
    """Works for both guild and user-installed app contexts."""
    flags = hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.MessageFlag.NONE
    try:
        await interaction.create_initial_response(
            hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=flags)
        kwargs: dict = {"embed": embed}
        if attachment:
            kwargs["attachment"] = hikari.Bytes(attachment, "qr.png")
        await interaction.edit_initial_response(**kwargs)
    except Exception as e:
        print(f"[Respond] {e}")
        traceback.print_exc()

@bot.listen(hikari.InteractionCreateEvent)
async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    if not isinstance(event.interaction, hikari.CommandInteraction):
        return
    ix  = event.interaction
    cmd = ix.command_name
    global invoice_seq, ltc_price_usd

    # Refresh price on every interaction too
    p = await get_ltc_price()
    if p:
        ltc_price_usd = p

    # ── /checktx ──────────────────────────────────────────────
    if cmd == "checktx":
        txid = str(opt(ix, "txid") or "").strip().lower()
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        if len(txid) != 64 or not all(c in "0123456789abcdef" for c in txid):
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Invalid TXID", description="Must be a 64-character hex string.", color=C_RED))
            return
        tx = await fetch_tx(txid)
        if not tx:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Not Found", description=f"No transaction found for:\n```{txid}```", color=C_RED))
            return
        embed = tx_embed(tx)
        if tx["confirmations"] < REQUIRED_CONFS:
            watched_txids[txid] = {"channel_id": ix.channel_id, "user_id": ix.user.id,
                                   "last_confs": tx["confirmations"], "done": False}
            embed.set_footer(text=f"👁️ Watching — DM at 1, 3 & {REQUIRED_CONFS} confs | 1 LTC = ${ltc_price_usd:,.2f}")
        await ix.edit_initial_response(embed=embed)
        await dm_user(ix.user.id, embed)

    # ── /watch ────────────────────────────────────────────────
    elif cmd == "watch":
        address = str(opt(ix, "address") or "").strip()
        if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Invalid Address",
                    description="Please provide a valid Litecoin address.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        last    = await fetch_latest_tx_hash(address)
        balance = await fetch_address_balance(address)
        watched_addresses[address] = {"channel_id": ix.channel_id, "last_tx_hash": last,
                                      "high_watermark_ltc": balance}
        embed = (
            hikari.Embed(title="👁️ Now Watching", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Monitor", icon=LTC_ICON)
            .add_field("📬 Address",         f"```{address}```",                                      inline=False)
            .add_field("💰 Current Balance", f"**{ltc_usd(balance)}**",                               inline=True)
            .add_field("🔔 Notifications",   f"DM on new TX\nConf updates: 1→3→{REQUIRED_CONFS}\n🏆 New balance highs", inline=False)
            .set_footer(text=f"Polling every {POLL_INTERVAL}s • 1 LTC = ${ltc_price_usd:,.2f} USD")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /unwatch ──────────────────────────────────────────────
    elif cmd == "unwatch":
        address = str(opt(ix, "address") or "").strip()
        if address in watched_addresses:
            del watched_addresses[address]
            embed = hikari.Embed(title="🛑 Stopped Watching", description=f"`{address}`", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Watching", description=f"`{address}` was not being watched.", color=C_RED)
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /watchlist ────────────────────────────────────────────
    elif cmd == "watchlist":
        if not watched_addresses and not watched_txids:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="📭 Watch List Empty",
                    description="Use `/watch <address>` to start.", color=C_GREY),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        embed = hikari.Embed(title="👁️ Watch List", color=C_LTC, timestamp=datetime.now(timezone.utc))
        if watched_addresses:
            lines = []
            for a, s in list(watched_addresses.items())[:15]:
                hw = s.get("high_watermark_ltc", 0)
                lines.append(f"• `{a[:20]}…` — High: **{hw:.4f} LTC**")
            embed.add_field(f"📬 Addresses ({len(watched_addresses)})", "\n".join(lines), inline=False)
        active = {k: v for k, v in watched_txids.items() if not v.get("done")}
        if active:
            embed.add_field(f"🔗 Active TXs ({len(active)})",
                "\n".join(f"• `{t[:20]}…` — {s['last_confs']} conf(s)"
                          for t, s in list(active.items())[:10]), inline=False)
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /balance ──────────────────────────────────────────────
    elif cmd == "balance":
        address = str(opt(ix, "address") or "").strip()
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data = await fetch_address(address)
        if not data:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Not Found", description=f"Could not fetch `{address}`", color=C_RED))
            return
        addr    = data.get("address", {})
        balance = addr.get("balance", 0) / 1e8
        recv    = addr.get("received", 0) / 1e8
        sent    = addr.get("spent", 0) / 1e8
        tx_count = addr.get("transaction_count", 0)
        embed = (
            hikari.Embed(title="💼 Address Balance", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Balance", icon=LTC_ICON)
            .add_field("📬 Address",        f"```{address}```",             inline=False)
            .add_field("💰 Balance",        f"**{ltc_usd(balance)}**",      inline=True)
            .add_field("📥 Total Received", f"{ltc_usd(recv)}",             inline=True)
            .add_field("📤 Total Sent",     f"{ltc_usd(sent)}",             inline=True)
            .add_field("🔢 Transactions",   f"`{tx_count}`",                inline=True)
            .set_footer(text=f"1 LTC = ${ltc_price_usd:,.2f} USD | Blockchair")
        )
        await ix.edit_initial_response(embed=embed)

    # ── /convert ──────────────────────────────────────────────
    elif cmd == "convert":
        amount        = float(opt(ix, "amount") or 0)
        from_currency = str(opt(ix, "from_currency") or "ltc")
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        price = await get_ltc_price()
        if not price:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Price Unavailable", description="Could not fetch LTC price.", color=C_RED))
            return
        if from_currency == "ltc":
            result     = amount * price
            equation   = f"{amount} LTC × ${price:,.2f} = **${result:,.2f} USD**"
            title      = "🔄 LTC → USD"
            result_str = f"**${result:,.4f} USD**"
        else:
            result     = amount / price
            equation   = f"${amount} USD ÷ ${price:,.2f} = **{result:.8f} LTC**"
            title      = "🔄 USD → LTC"
            result_str = f"**{result:.8f} LTC**"
        embed = (
            hikari.Embed(title=title, color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Converter", icon=LTC_ICON)
            .add_field("📥 Input",      f"`{amount}` {'LTC' if from_currency=='ltc' else 'USD'}", inline=True)
            .add_field("📤 Result",     result_str,                                               inline=True)
            .add_field("🧮 Equation",   equation,                                                 inline=False)
            .add_field("💵 LTC Price",  f"`${price:,.2f} USD`",                                   inline=True)
            .set_footer(text="Prices from CoinGecko")
        )
        await ix.edit_initial_response(embed=embed)

    # ── /invoice ──────────────────────────────────────────────
    elif cmd == "invoice":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        address     = str(opt(ix, "address") or "").strip()
        amount      = float(opt(ix, "amount") or 0)
        description = str(opt(ix, "description") or "Litecoin Payment")
        if amount <= 0:
            await ix.edit_initial_response("❌ Amount must be greater than 0.")
            return
        invoice_seq += 1
        inv = {"id": f"{invoice_seq:04d}", "address": address, "amount": round(amount, 8),
               "description": description, "status": "pending", "txid": None,
               "creator": str(ix.user), "channel_id": ix.channel_id, "user_id": ix.user.id}
        invoices[inv["id"]] = inv
        if address not in watched_addresses:
            last    = await fetch_latest_tx_hash(address)
            balance = await fetch_address_balance(address)
            watched_addresses[address] = {"channel_id": ix.channel_id, "last_tx_hash": last,
                                          "high_watermark_ltc": balance}
        qr_bytes = make_qr(address, amount)
        embed    = invoice_embed(inv)
        await ix.edit_initial_response(embed=embed, attachment=hikari.Bytes(qr_bytes, "qr.png"))
        await dm_user(ix.user.id, invoice_embed(inv), qr_bytes)

    # ── /invoicestatus ────────────────────────────────────────
    elif cmd == "invoicestatus":
        inv_id = str(opt(ix, "invoice_id") or "").zfill(4)
        inv    = invoices.get(inv_id)
        if not inv:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Not Found",
                    description=f"No invoice `{inv_id}`.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE, embed=invoice_embed(inv))

    # ── /ltcstats ─────────────────────────────────────────────
    elif cmd == "ltcstats":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data  = await fetch_network()
        price = await get_ltc_price()
        if not data:
            await ix.edit_initial_response("❌ Could not reach Blockchair API.")
            return
        blocks = data.get("blocks", "N/A")
        embed = (
            hikari.Embed(title="⛏️ Litecoin Network Stats", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Network", icon=LTC_ICON)
            .add_field("📦 Block Height",    f"`{blocks:,}`" if isinstance(blocks, int) else f"`{blocks}`", inline=True)
            .add_field("💵 LTC Price",       f"`${price:,.2f} USD`",                                        inline=True)
            .add_field("⏱️ Unconfirmed TXs", f"`{data.get('mempool_transactions','N/A')}`",                  inline=True)
            .add_field("📊 24h TXs",         f"`{data.get('transactions_24h','N/A')}`",                      inline=True)
            .add_field("⛏️ Difficulty",      f"`{data.get('difficulty','N/A')}`",                            inline=True)
            .add_field("📈 24h Volume",      f"`{data.get('volume_24h','N/A')} LTC`",                        inline=True)
            .set_footer(text="Blockchair API + CoinGecko")
        )
        await ix.edit_initial_response(embed=embed)

    # ── /help ─────────────────────────────────────────────────
    elif cmd == "help":
        embed = (
            hikari.Embed(title="🪙 LTC Bot — Help",
                         description="Litecoin transaction monitor, watcher & invoice bot.",
                         color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Bot", icon=LTC_ICON)
            .add_field("🔍 Transactions", (
                "`/checktx <txid>` — Look up any LTC transaction\n"
                "`/watch <addr>` — Watch address for new TXs + balance highs\n"
                "`/unwatch <addr>` — Stop watching\n"
                "`/watchlist` — View all watched addresses & TXs\n"
                "`/balance <addr>` — Check address balance"), inline=False)
            .add_field("🧾 Invoices", (
                "`/invoice <addr> <amount> [desc]` — Create invoice + QR code\n"
                "`/invoicestatus <id>` — Check invoice status"), inline=False)
            .add_field("💱 Convert", "`/convert <amount> <LTC→USD | USD→LTC>` — Price converter", inline=False)
            .add_field("📡 Network", "`/ltcstats` — Live Litecoin network stats", inline=False)
            .add_field("🔔 Auto Notifications", (
                f"• DM on new incoming TX (LTC + USD value)\n"
                f"• Confirmation DMs at 1 → 3 → {REQUIRED_CONFS} confs\n"
                f"• 🏆 DM when wallet hits new balance high\n"
                f"• Polls every **{POLL_INTERVAL}s**"), inline=False)
            .set_footer(text="Blockchair API + CoinGecko • No API key needed")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE, embed=embed)

# ──────────────────────────────────────────────────────────────
# STARTED
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    global ltc_price_usd
    print(f"✅ Bot online | Polling every {POLL_INTERVAL}s | Confs: {REQUIRED_CONFS}")
    print(f"🔔 NOTIFY_USER_ID = {NOTIFY_USER_ID}")
    print(f"📬 WATCH_ADDRESS  = {WATCH_ADDRESS or 'NOT SET'}")
    if not NOTIFY_USER_ID:
        print("⚠️  WARNING: NOTIFY_USER_ID not set — DMs disabled!")

    # Fetch initial price
    ltc_price_usd = await get_ltc_price()
    print(f"💵 LTC Price: ${ltc_price_usd:,.2f} USD")

    if WATCH_ADDRESS:
        last    = await fetch_latest_tx_hash(WATCH_ADDRESS)
        balance = await fetch_address_balance(WATCH_ADDRESS)
        watched_addresses[WATCH_ADDRESS] = {
            "channel_id":        None,
            "last_tx_hash":      last,
            "high_watermark_ltc": balance,
        }
        print(f"👁️ Auto-watching: {WATCH_ADDRESS} (balance: {balance:.4f} LTC)")

    asyncio.create_task(poll_loop())

bot.run()
