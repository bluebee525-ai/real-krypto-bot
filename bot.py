"""
LTC Discord Bot  •  Python 3.13  •  hikari
Commands: checktx, watch, unwatch, watchlist, balance, txhistory,
          invoice, invoicestatus, invoicelist, expireinvoice,
          convert, portfolio, addwallet, removewallet,
          pricealert, pricealerts, removealert,
          qr, fees, setnotify, ltcstats, help
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
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "30"))
REQUIRED_CONFS = int(os.environ.get("REQUIRED_CONFS", "6"))
GUILD_ID       = int(os.environ.get("GUILD_ID", "0"))
WATCH_ADDRESS  = os.environ.get("LTC_WATCH_ADDRESS", "")

_raw_ids = os.environ.get("NOTIFY_USER_IDS", os.environ.get("NOTIFY_USER_ID", ""))
NOTIFY_USER_IDS: list[int] = [int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()]

BLOCKCHAIR = "https://api.blockchair.com/litecoin"
LTC_ICON   = "https://cryptologos.cc/logos/litecoin-ltc-logo.png"
C_LTC    = 0x345D9D
C_GREEN  = 0x2ECC71
C_RED    = 0xE74C3C
C_ORANGE = 0xF39C12
C_GREY   = 0x95A5A6
C_GOLD   = 0xF1C40F
C_PURPLE = 0x9B59B6
C_BLUE   = 0x3498DB

# ──────────────────────────────────────────────────────────────
# BOT
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

watched_addresses: dict  = {}   # addr → {last_tx_hash, channel_id, high_watermark_ltc, label}
watched_txids:     dict  = {}
invoices:          dict  = {}
invoice_seq:       int   = 0
ltc_price_usd:     float = 0.0
portfolio:         dict  = {}   # label → address
price_alerts:      list  = []   # [{target, direction, triggered}]
notify_user_ids:   list  = list(NOTIFY_USER_IDS)  # mutable at runtime via /setnotify

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
    if raw and "litecoin" in raw:
        return float(raw["litecoin"].get("usd", 0))
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
    data = raw.get("data", {})
    if address in data:
        return data[address]
    if data:
        return next(iter(data.values()))
    return None

async def fetch_address_stats(address: str) -> dict:
    data = await fetch_address(address)
    if not data:
        return {"balance": 0.0, "received": 0.0, "spent": 0.0, "tx_count": 0, "txs": []}
    addr = data.get("address", {})
    return {
        "balance":  addr.get("balance", 0) / 1e8,
        "received": addr.get("received", 0) / 1e8,
        "spent":    addr.get("spent", 0) / 1e8,
        "tx_count": addr.get("transaction_count", 0),
        "txs":      data.get("transactions", []),
    }

async def fetch_address_balance(address: str) -> float:
    stats = await fetch_address_stats(address)
    return stats["balance"]

async def fetch_latest_tx_hash(address: str) -> str | None:
    data = await fetch_address(address)
    if data:
        txs = data.get("transactions", [])
        return txs[0] if txs else None
    return None

async def fetch_network() -> dict | None:
    raw = await api_get(f"{BLOCKCHAIR}/stats")
    return raw.get("data", {}) if raw else None

# ──────────────────────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────────────────────
def fmt_dual(ltc: float) -> str:
    if ltc_price_usd:
        return f"**{ltc:.8f} LTC** (${ltc * ltc_price_usd:,.2f} USD)"
    return f"**{ltc:.8f} LTC**"

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

def make_qr(address: str, amount_ltc: float = 0) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    data = f"litecoin:{address}" + (f"?amount={amount_ltc}" if amount_ltc else "")
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#345D9D", back_color="white").save(buf, format="PNG")
    return buf.getvalue()

def price_footer() -> str:
    return f"1 LTC = ${ltc_price_usd:,.2f} USD | Blockchair" if ltc_price_usd else "Blockchair API"

def notify_list_str() -> str:
    return "\n".join(f"• <@{uid}>" for uid in notify_user_ids) or "⚠️ Nobody — use `/setnotify add <user_id>`"

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
            recv_lines.append(f"`{addr}`\n{fmt_dual(o['value_ltc'])}")
    receiver_str = "\n".join(recv_lines[:3]) or "Unknown"
    if len(tx["outputs"]) > 3:
        receiver_str += f"\n*+{len(tx['outputs'])-3} more*"
    return (
        hikari.Embed(title=title,
                     url=f"https://blockchair.com/litecoin/transaction/{txid}",
                     color=conf_color(confs), timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Network", icon=LTC_ICON)
        .add_field("🆔 TXID",   f"```{txid}```",                             inline=False)
        .add_field("💰 Amount", fmt_dual(tx["total_ltc"]),                   inline=True)
        .add_field("⛽ Fee",    fmt_dual(tx["fee_ltc"]),                     inline=True)
        .add_field("📦 Size",   f"{tx['size']} bytes",                       inline=True)
        .add_field("📊 Status", f"{status_label(confs)}\n{conf_bar(confs)}", inline=False)
        .add_field("📤 From",   sender_str,                                  inline=True)
        .add_field("📥 To",     receiver_str,                                inline=True)
        .add_field("🕐 Time",   fmt_time(tx["time"]),                        inline=False)
        .set_footer(text=price_footer(), icon=LTC_ICON)
    )

def invoice_embed(inv: dict) -> hikari.Embed:
    labels = {"pending": ("⏳ Awaiting Payment", C_ORANGE), "paid": ("✅ Paid", C_GREEN), "expired": ("❌ Expired", C_RED)}
    label, color = labels.get(inv["status"], ("❓ Unknown", C_GREY))
    amt_ltc = inv["amount"]
    amt_usd = amt_ltc * ltc_price_usd if ltc_price_usd else 0
    e = (
        hikari.Embed(title=f"🧾 Invoice #{inv['id']}",
                     description=f"**{inv.get('description','Litecoin Payment')}**",
                     color=color, timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Invoice", icon=LTC_ICON)
        .add_field("💎 LTC Amount", f"**{amt_ltc:.8f} LTC**",                        inline=True)
        .add_field("💵 USD Value",  f"**${amt_usd:,.2f} USD**" if amt_usd else "N/A", inline=True)
        .add_field("📌 Status",     label,                                            inline=True)
        .add_field("🔢 Invoice ID", f"`#{inv['id']}`",                                inline=True)
        .add_field("📬 Pay To",     f"```{inv['address']}```",                        inline=False)
        .add_field("📋 Instructions",
                   f"Send exactly **{amt_ltc:.8f} LTC**" +
                   (f" (≈ ${amt_usd:,.2f} USD)" if amt_usd else "") +
                   " to the address above.\nThe bot will auto-detect your payment.", inline=False)
        .set_footer(text=price_footer())
    )
    if inv.get("txid"):
        e.add_field("🔗 TXID", f"```{inv['txid']}```", inline=False)
    return e

# ──────────────────────────────────────────────────────────────
# DM / NOTIFY
# ──────────────────────────────────────────────────────────────
async def dm_user(user_id: int, embed: hikari.Embed, attachment: bytes | None = None):
    try:
        dm = await bot.rest.create_dm_channel(user_id)
        kwargs: dict = {"embed": embed}
        if attachment:
            kwargs["attachment"] = hikari.Bytes(attachment, "qr.png")
        await bot.rest.create_message(dm.id, **kwargs)
        print(f"[DM] ✅ {user_id}")
    except Exception as e:
        print(f"[DM] ❌ {user_id}: {e}")

async def dm_all(embed: hikari.Embed, attachment: bytes | None = None):
    for uid in notify_user_ids:
        await dm_user(uid, embed, attachment)

async def notify(embed: hikari.Embed, channel_id: int | None, dm: bool = True):
    if channel_id:
        try:
            await bot.rest.create_message(channel_id, embed=embed)
        except Exception as e:
            print(f"[Notify] {e}")
    if dm:
        await dm_all(embed)

# ──────────────────────────────────────────────────────────────
# POLLING
# ──────────────────────────────────────────────────────────────
async def poll_loop():
    global ltc_price_usd
    while True:
        await asyncio.sleep(POLL_INTERVAL)

        price = await get_ltc_price()
        if price:
            ltc_price_usd = price

        # ── Price alerts ──────────────────────────────────────
        for alert in price_alerts:
            if alert.get("triggered"):
                continue
            target = alert["target"]
            try:
                if alert["direction"] == "above" and ltc_price_usd >= target:
                    alert["triggered"] = True
                    embed = (
                        hikari.Embed(title="🚨 Price Alert Triggered!",
                                     description=f"LTC has reached **${ltc_price_usd:,.2f} USD**!",
                                     color=C_GREEN, timestamp=datetime.now(timezone.utc))
                        .set_author(name="LTC Price Alert", icon=LTC_ICON)
                        .add_field("🎯 Target",   f"${target:,.2f} USD", inline=True)
                        .add_field("💵 Current",  f"${ltc_price_usd:,.2f} USD", inline=True)
                        .set_footer(text="CoinGecko")
                    )
                    await dm_all(embed)
                elif alert["direction"] == "below" and ltc_price_usd <= target:
                    alert["triggered"] = True
                    embed = (
                        hikari.Embed(title="🚨 Price Alert Triggered!",
                                     description=f"LTC has dropped to **${ltc_price_usd:,.2f} USD**!",
                                     color=C_RED, timestamp=datetime.now(timezone.utc))
                        .set_author(name="LTC Price Alert", icon=LTC_ICON)
                        .add_field("🎯 Target",  f"${target:,.2f} USD",     inline=True)
                        .add_field("💵 Current", f"${ltc_price_usd:,.2f} USD", inline=True)
                        .set_footer(text="CoinGecko")
                    )
                    await dm_all(embed)
            except Exception as e:
                print(f"[Price Alert] {e}")

        # ── Address polling ───────────────────────────────────
        for address, state in list(watched_addresses.items()):
            try:
                latest = await fetch_latest_tx_hash(address)
                if not latest or state.get("last_tx_hash") == latest:
                    balance   = await fetch_address_balance(address)
                    prev_high = state.get("high_watermark_ltc", 0.0)
                    if balance > prev_high + 0.00000001:
                        watched_addresses[address]["high_watermark_ltc"] = balance
                        label = state.get("label", address[:16] + "…")
                        embed = (
                            hikari.Embed(title="🏆 New Balance High!",
                                         description=f"Wallet **{label}** hit a new all-time high!",
                                         color=C_GOLD, timestamp=datetime.now(timezone.utc))
                            .set_author(name="LTC Balance Alert", icon=LTC_ICON)
                            .add_field("💰 New High", f"**{balance:.8f} LTC**",                          inline=True)
                            .add_field("💵 In USD",   f"**${balance*ltc_price_usd:,.2f}**" if ltc_price_usd else "N/A", inline=True)
                            .add_field("📬 Address",  f"`{address}`",                                    inline=False)
                            .set_footer(text=price_footer())
                        )
                        await dm_all(embed)
                    continue

                watched_addresses[address]["last_tx_hash"] = latest
                tx = await fetch_tx(latest)
                if not tx:
                    continue
                balance = await fetch_address_balance(address)
                watched_addresses[address]["high_watermark_ltc"] = max(
                    balance, state.get("high_watermark_ltc", 0.0))
                label = state.get("label", address[:20] + "…")
                embed = tx_embed(tx, "🚨 New Incoming Transaction!")
                embed.color = C_ORANGE
                embed.add_field("📍 Wallet",      f"**{label}**\n`{address}`", inline=False)
                embed.add_field("💼 New Balance", fmt_dual(balance),           inline=False)
                await notify(embed, state.get("channel_id"))
                if latest not in watched_txids:
                    watched_txids[latest] = {"channel_id": state.get("channel_id"),
                                             "last_confs": tx["confirmations"],
                                             "done": tx["confirmations"] >= REQUIRED_CONFS}
            except Exception as e:
                print(f"[Poll Addr] {address}: {e}")

        # ── TX confirmation polling ───────────────────────────
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
                                hikari.Embed(title="🔒 Transaction Fully Confirmed!",
                                             description=f"Reached **{confs} confirmations** — fully settled.",
                                             url=f"https://blockchair.com/litecoin/transaction/{txid}",
                                             color=C_GREEN, timestamp=datetime.now(timezone.utc))
                                .set_author(name="Litecoin Network", icon=LTC_ICON)
                                .add_field("🆔 TXID",          f"```{txid}```",           inline=False)
                                .add_field("💰 Amount",        fmt_dual(tx["total_ltc"]), inline=True)
                                .add_field("📊 Confirmations", conf_bar(confs),           inline=False)
                                .set_footer(text=price_footer())
                            )
                            watched_txids[txid]["done"] = True
                        else:
                            embed = tx_embed(tx, f"🔄 {confs} Confirmation{'s' if confs!=1 else ''}")
                        await notify(embed, state.get("channel_id"))
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
        # ── Transactions ──────────────────────────────────────
        bot.rest.slash_command_builder("checktx", "Look up a Litecoin transaction by TXID")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="txid",
                description="64-character hex transaction ID", is_required=True)),

        bot.rest.slash_command_builder("txhistory", "Show last transactions for an address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.INTEGER, name="limit",
                description="Number of TXs to show (1-10, default 5)", is_required=False)),

        # ── Watching ──────────────────────────────────────────
        bot.rest.slash_command_builder("watch", "Watch a Litecoin address for incoming transactions")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address to monitor", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="label",
                description="Friendly name e.g. 'hot wallet'", is_required=False)),

        bot.rest.slash_command_builder("unwatch", "Stop watching a Litecoin address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address to stop monitoring", is_required=True)),

        bot.rest.slash_command_builder("watchlist", "Show all watched addresses and active transactions"),

        # ── Balance ───────────────────────────────────────────
        bot.rest.slash_command_builder("balance", "Check the balance of a Litecoin address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address to check", is_required=True)),

        bot.rest.slash_command_builder("qr", "Generate a QR code for any LTC address")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="amount",
                description="Optional amount to encode in the QR", is_required=False)),

        # ── Portfolio ─────────────────────────────────────────
        bot.rest.slash_command_builder("portfolio", "View total balance across all watched wallets"),

        bot.rest.slash_command_builder("addwallet", "Add a wallet to portfolio (no TX alerts)")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="label",
                description="Friendly name e.g. 'cold storage'", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="LTC address", is_required=True)),

        bot.rest.slash_command_builder("removewallet", "Remove a wallet from portfolio")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="label",
                description="Label of wallet to remove", is_required=True)),

        # ── Invoices ──────────────────────────────────────────
        bot.rest.slash_command_builder("invoice", "Create a Litecoin payment invoice with QR code")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="address",
                description="Your LTC receiving address (auto-fills from watched wallets if empty)", is_required=False))
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="amount",
                description="Amount to request", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="currency",
                description="Currency of the amount", is_required=True,
                choices=[hikari.CommandChoice(name="LTC", value="ltc"),
                         hikari.CommandChoice(name="USD", value="usd")]))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="description",
                description="What the payment is for", is_required=False)),

        bot.rest.slash_command_builder("invoicestatus", "Check the status of an invoice")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="invoice_id",
                description="Invoice ID e.g. 0001", is_required=True)),

        bot.rest.slash_command_builder("invoicelist", "View all open invoices"),

        bot.rest.slash_command_builder("expireinvoice", "Manually mark an invoice as expired")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="invoice_id",
                description="Invoice ID to expire", is_required=True)),

        # ── Price & Convert ───────────────────────────────────
        bot.rest.slash_command_builder("convert", "Convert between LTC and USD")
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="amount",
                description="Amount to convert", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="from_currency",
                description="Convert from", is_required=True,
                choices=[hikari.CommandChoice(name="LTC → USD", value="ltc"),
                         hikari.CommandChoice(name="USD → LTC", value="usd")])),

        bot.rest.slash_command_builder("pricealert", "Get DM'd when LTC hits a price target")
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="target",
                description="Target price in USD e.g. 120", is_required=True))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="direction",
                description="Alert when price goes above or below target", is_required=True,
                choices=[hikari.CommandChoice(name="Above target", value="above"),
                         hikari.CommandChoice(name="Below target", value="below")])),

        bot.rest.slash_command_builder("pricealerts", "List all active price alerts"),

        bot.rest.slash_command_builder("removealert", "Remove a price alert")
            .add_option(hikari.CommandOption(type=hikari.OptionType.FLOAT, name="target",
                description="Target price of the alert to remove", is_required=True)),

        # ── Settings ──────────────────────────────────────────
        bot.rest.slash_command_builder("setnotify", "Add or remove a user from DM notifications")
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="action",
                description="Add or remove", is_required=True,
                choices=[hikari.CommandChoice(name="Add user", value="add"),
                         hikari.CommandChoice(name="Remove user", value="remove"),
                         hikari.CommandChoice(name="List users", value="list")]))
            .add_option(hikari.CommandOption(type=hikari.OptionType.STRING, name="user_id",
                description="Discord user ID (not needed for list)", is_required=False)),

        # ── Info ──────────────────────────────────────────────
        bot.rest.slash_command_builder("fees", "Show current recommended Litecoin network fees"),
        bot.rest.slash_command_builder("ltcstats", "Show live Litecoin network stats"),
        bot.rest.slash_command_builder("help", "Show all bot commands"),
    ]

    if GUILD_ID:
        await bot.rest.set_application_commands(application=app.id, guild=GUILD_ID, commands=[])
        await bot.rest.set_application_commands(application=app.id, guild=GUILD_ID, commands=commands)
        print(f"✅ {len(commands)} commands registered to guild {GUILD_ID} (instant)")
    else:
        await bot.rest.set_application_commands(application=app.id, commands=[])
        await bot.rest.set_application_commands(application=app.id, commands=commands)
        print(f"✅ {len(commands)} commands registered globally")

# ──────────────────────────────────────────────────────────────
# INTERACTION HANDLER
# ──────────────────────────────────────────────────────────────
def opt(ix: hikari.CommandInteraction, name: str):
    for o in (ix.options or []):
        if o.name == name:
            return o.value
    return None

@bot.listen(hikari.InteractionCreateEvent)
async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    if not isinstance(event.interaction, hikari.CommandInteraction):
        return
    ix  = event.interaction
    cmd = ix.command_name
    global invoice_seq, ltc_price_usd

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
            watched_txids[txid] = {"channel_id": ix.channel_id,
                                   "last_confs": tx["confirmations"], "done": False}
            embed.set_footer(text=f"👁️ Watching — DM'd at 1, 3 & {REQUIRED_CONFS} confs | {price_footer()}")
        await ix.edit_initial_response(embed=embed)
        await dm_all(embed)

    # ── /txhistory ────────────────────────────────────────────
    elif cmd == "txhistory":
        address = str(opt(ix, "address") or "").strip()
        limit   = min(int(opt(ix, "limit") or 5), 10)
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        stats = await fetch_address_stats(address)
        txids = stats["txs"][:limit]
        if not txids:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="📭 No Transactions", description=f"No transactions found for `{address}`", color=C_GREY))
            return
        embed = (
            hikari.Embed(title=f"📜 Last {len(txids)} Transactions",
                         color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin TX History", icon=LTC_ICON)
            .add_field("📬 Address", f"`{address}`", inline=False)
        )
        for i, txid in enumerate(txids):
            tx = await fetch_tx(txid)
            if tx:
                usd = f" (${tx['total_ltc']*ltc_price_usd:,.2f})" if ltc_price_usd else ""
                embed.add_field(
                    f"TX {i+1} — {status_label(tx['confirmations'])}",
                    f"`{txid[:32]}…`\n**{tx['total_ltc']:.6f} LTC{usd}**\n{fmt_time(tx['time'])}",
                    inline=False)
        embed.set_footer(text=price_footer())
        await ix.edit_initial_response(embed=embed)

    # ── /watch ────────────────────────────────────────────────
    elif cmd == "watch":
        address = str(opt(ix, "address") or "").strip()
        label   = str(opt(ix, "label") or address[:20] + "…")
        if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Invalid Address",
                    description="Please provide a valid LTC address.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        last    = await fetch_latest_tx_hash(address)
        balance = await fetch_address_balance(address)
        watched_addresses[address] = {"channel_id": ix.channel_id, "last_tx_hash": last,
                                      "high_watermark_ltc": balance, "label": label}
        portfolio[label] = address
        embed = (
            hikari.Embed(title="👁️ Now Watching", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Monitor", icon=LTC_ICON)
            .add_field("🏷️ Label",          f"**{label}**",                                          inline=True)
            .add_field("📬 Address",         f"```{address}```",                                      inline=False)
            .add_field("💰 Current Balance", fmt_dual(balance),                                       inline=True)
            .add_field("🔔 Notifying",       notify_list_str(),                                       inline=False)
            .add_field("📡 Alerts",          f"New TX • Confs 1→3→{REQUIRED_CONFS} • 🏆 Balance highs", inline=False)
            .set_footer(text=f"Polling every {POLL_INTERVAL}s • {price_footer()}")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /unwatch ──────────────────────────────────────────────
    elif cmd == "unwatch":
        address = str(opt(ix, "address") or "").strip()
        if address in watched_addresses:
            label = watched_addresses[address].get("label", address)
            del watched_addresses[address]
            portfolio.pop(label, None)
            embed = hikari.Embed(title="🛑 Stopped Watching",
                description=f"**{label}**\n`{address}`", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Found",
                description=f"`{address}` is not being watched.", color=C_RED)
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
                hw    = s.get("high_watermark_ltc", 0)
                label = s.get("label", a[:20] + "…")
                usd   = f" (${hw*ltc_price_usd:,.2f})" if ltc_price_usd else ""
                lines.append(f"• **{label}** — {hw:.4f} LTC{usd}")
            embed.add_field(f"📬 Addresses ({len(watched_addresses)})", "\n".join(lines), inline=False)
        active = {k: v for k, v in watched_txids.items() if not v.get("done")}
        if active:
            embed.add_field(f"🔗 Active TXs ({len(active)})",
                "\n".join(f"• `{t[:20]}…` — {s['last_confs']} conf(s)"
                          for t, s in list(active.items())[:10]), inline=False)
        embed.set_footer(text=price_footer())
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /balance ──────────────────────────────────────────────
    elif cmd == "balance":
        address = str(opt(ix, "address") or "").strip()
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        if not address:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ No Address", description="Please provide an LTC address.", color=C_RED))
            return
        print(f"[Balance] Fetching {address}...")
        raw = await api_get(f"{BLOCKCHAIR}/dashboards/address/{address}")
        print(f"[Balance] Raw response keys: {list(raw.keys()) if raw else 'None'}")
        if not raw or not raw.get("data"):
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Not Found",
                description=f"Could not fetch data for:
`{address}`

Check it's a valid LTC address.",
                color=C_RED))
            return
        data    = raw["data"]
        entry   = data.get(address) or next(iter(data.values()), None)
        if not entry:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ No Data", description=f"Blockchair returned no data for `{address}`", color=C_RED))
            return
        addr    = entry.get("address", {})
        balance  = addr.get("balance", 0) / 1e8
        received = addr.get("received", 0) / 1e8
        spent    = addr.get("spent", 0) / 1e8
        tx_count = addr.get("transaction_count", 0)
        print(f"[Balance] {address}: {balance} LTC, {tx_count} TXs")
        embed = (
            hikari.Embed(title="💼 Address Balance", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Balance", icon=LTC_ICON)
            .add_field("📬 Address",        f"```{address}```",   inline=False)
            .add_field("💰 Balance",        fmt_dual(balance),    inline=True)
            .add_field("📥 Total Received", fmt_dual(received),   inline=True)
            .add_field("📤 Total Sent",     fmt_dual(spent),      inline=True)
            .add_field("🔢 Transactions",   f"`{tx_count}`",      inline=True)
            .set_footer(text=price_footer())
        )
        await ix.edit_initial_response(embed=embed)

    # ── /qr ───────────────────────────────────────────────────
    elif cmd == "qr":
        address    = str(opt(ix, "address") or "").strip()
        amount_ltc = float(opt(ix, "amount") or 0)
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        qr_bytes = make_qr(address, amount_ltc)
        embed = (
            hikari.Embed(title="📱 QR Code", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin QR", icon=LTC_ICON)
            .add_field("📬 Address", f"```{address}```", inline=False)
        )
        if amount_ltc:
            embed.add_field("💰 Amount", fmt_dual(amount_ltc), inline=True)
        embed.set_footer(text=price_footer())
        await ix.edit_initial_response(embed=embed, attachment=hikari.Bytes(qr_bytes, "qr.png"))

    # ── /portfolio ────────────────────────────────────────────
    elif cmd == "portfolio":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        all_wallets = {**{s.get("label", a): a for a, s in watched_addresses.items()}, **portfolio}
        if not all_wallets:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="📭 Portfolio Empty",
                description="Add wallets with `/watch <address> <label>` or `/addwallet <label> <address>`",
                color=C_GREY))
            return
        total_ltc = 0.0
        lines     = []
        for label, address in list(all_wallets.items())[:10]:
            balance    = await fetch_address_balance(address)
            total_ltc += balance
            usd        = f" (${balance*ltc_price_usd:,.2f})" if ltc_price_usd else ""
            lines.append((label, address, balance, usd))
        total_usd = total_ltc * ltc_price_usd if ltc_price_usd else 0
        embed = (
            hikari.Embed(title="💼 Portfolio Overview", color=C_PURPLE,
                         timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Portfolio", icon=LTC_ICON)
            .add_field("💰 Total Balance",
                       f"**{total_ltc:.8f} LTC**" + (f"\n**${total_usd:,.2f} USD**" if total_usd else ""),
                       inline=False)
        )
        for label, address, balance, usd in lines:
            embed.add_field(f"🏷️ {label}", f"`{address[:24]}…`\n{balance:.8f} LTC{usd}", inline=True)
        embed.set_footer(text=price_footer())
        await ix.edit_initial_response(embed=embed)

    # ── /addwallet ────────────────────────────────────────────
    elif cmd == "addwallet":
        label   = str(opt(ix, "label") or "").strip()
        address = str(opt(ix, "address") or "").strip()
        portfolio[label] = address
        balance = await fetch_address_balance(address)
        embed = (
            hikari.Embed(title="✅ Wallet Added to Portfolio", color=C_GREEN,
                         timestamp=datetime.now(timezone.utc))
            .add_field("🏷️ Label",   f"**{label}**",        inline=True)
            .add_field("📬 Address", f"`{address}`",         inline=False)
            .add_field("💰 Balance", fmt_dual(balance),      inline=True)
            .set_footer(text="No TX alerts — use /watch for alerts")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /removewallet ─────────────────────────────────────────
    elif cmd == "removewallet":
        label = str(opt(ix, "label") or "").strip()
        if label in portfolio:
            del portfolio[label]
            embed = hikari.Embed(title="🗑️ Wallet Removed",
                description=f"**{label}** removed from portfolio.", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Found",
                description=f"No wallet labelled **{label}** in portfolio.", color=C_RED)
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /invoice ──────────────────────────────────────────────
    elif cmd == "invoice":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        address     = str(opt(ix, "address") or "").strip()
        amount      = float(opt(ix, "amount") or 0)
        currency    = str(opt(ix, "currency") or "ltc").lower()
        description = str(opt(ix, "description") or "Litecoin Payment")

        # Auto-fill address from watched wallets / portfolio if empty
        if not address:
            if WATCH_ADDRESS:
                address = WATCH_ADDRESS
                print(f"[Invoice] Auto-filled address from WATCH_ADDRESS: {address}")
            elif watched_addresses:
                address = next(iter(watched_addresses))
                print(f"[Invoice] Auto-filled address from watched_addresses: {address}")
            elif portfolio:
                address = next(iter(portfolio.values()))
                print(f"[Invoice] Auto-filled address from portfolio: {address}")
            else:
                await ix.edit_initial_response(embed=hikari.Embed(
                    title="❌ No Address",
                    description="No address provided and no watched wallets to auto-fill from.
Provide an address or use `/watch` first.",
                    color=C_RED))
                return

        if amount <= 0:
            await ix.edit_initial_response("❌ Amount must be greater than 0.")
            return
        if currency == "usd":
            if not ltc_price_usd:
                await ix.edit_initial_response(embed=hikari.Embed(
                    title="❌ Price Unavailable",
                    description="Could not fetch LTC price to convert USD. Try again shortly.",
                    color=C_RED))
                return
            amount_ltc = amount / ltc_price_usd
        else:
            amount_ltc = amount
        invoice_seq += 1
        inv = {"id": f"{invoice_seq:04d}", "address": address, "amount": round(amount_ltc, 8),
               "description": description, "status": "pending", "txid": None,
               "creator": str(ix.user), "channel_id": ix.channel_id}
        invoices[inv["id"]] = inv
        if address not in watched_addresses:
            last    = await fetch_latest_tx_hash(address)
            balance = await fetch_address_balance(address)
            watched_addresses[address] = {"channel_id": ix.channel_id, "last_tx_hash": last,
                                          "high_watermark_ltc": balance, "label": f"Invoice #{inv['id']}"}
        qr_bytes = make_qr(address, amount_ltc)
        await ix.edit_initial_response(embed=invoice_embed(inv),
                                       attachment=hikari.Bytes(qr_bytes, "qr.png"))
        await dm_all(invoice_embed(inv), qr_bytes)

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

    # ── /invoicelist ──────────────────────────────────────────
    elif cmd == "invoicelist":
        open_inv = {k: v for k, v in invoices.items() if v["status"] == "pending"}
        if not open_inv:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="📭 No Open Invoices",
                    description="All invoices are paid or expired.", color=C_GREY),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        embed = hikari.Embed(title=f"🧾 Open Invoices ({len(open_inv)})",
                             color=C_ORANGE, timestamp=datetime.now(timezone.utc))
        for inv_id, inv in list(open_inv.items())[:10]:
            usd = f" (${inv['amount']*ltc_price_usd:,.2f})" if ltc_price_usd else ""
            embed.add_field(f"#{inv_id} — {inv.get('description','Payment')}",
                            f"{inv['amount']:.8f} LTC{usd}\n`{inv['address'][:24]}…`", inline=True)
        embed.set_footer(text=price_footer())
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /expireinvoice ────────────────────────────────────────
    elif cmd == "expireinvoice":
        inv_id = str(opt(ix, "invoice_id") or "").zfill(4)
        inv    = invoices.get(inv_id)
        if not inv:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Not Found",
                    description=f"No invoice `{inv_id}`.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        invoices[inv_id]["status"] = "expired"
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=hikari.Embed(title="❌ Invoice Expired",
                description=f"Invoice **#{inv_id}** has been marked as expired.", color=C_RED),
            flags=hikari.MessageFlag.EPHEMERAL)

    # ── /convert ──────────────────────────────────────────────
    elif cmd == "convert":
        amount        = float(opt(ix, "amount") or 0)
        from_currency = str(opt(ix, "from_currency") or "ltc")
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        if not ltc_price_usd:
            await ix.edit_initial_response(embed=hikari.Embed(
                title="❌ Price Unavailable", description="Could not fetch LTC price.", color=C_RED))
            return
        if from_currency == "ltc":
            result   = amount * ltc_price_usd
            equation = f"{amount} LTC × ${ltc_price_usd:,.2f} = **${result:,.2f} USD**"
            title    = "🔄 LTC → USD"
            res_str  = f"**${result:,.4f} USD**"
        else:
            result   = amount / ltc_price_usd
            equation = f"${amount} ÷ ${ltc_price_usd:,.2f} = **{result:.8f} LTC**"
            title    = "🔄 USD → LTC"
            res_str  = f"**{result:.8f} LTC**"
        embed = (
            hikari.Embed(title=title, color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Converter", icon=LTC_ICON)
            .add_field("📥 Input",    f"`{amount}` {'LTC' if from_currency=='ltc' else 'USD'}", inline=True)
            .add_field("📤 Result",   res_str,                                                  inline=True)
            .add_field("🧮 Equation", equation,                                                 inline=False)
            .add_field("💵 Rate",     f"`1 LTC = ${ltc_price_usd:,.2f} USD`",                  inline=True)
            .set_footer(text="CoinGecko")
        )
        await ix.edit_initial_response(embed=embed)

    # ── /pricealert ───────────────────────────────────────────
    elif cmd == "pricealert":
        target    = float(opt(ix, "target") or 0)
        direction = str(opt(ix, "direction") or "above")
        if target <= 0:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Invalid Target", description="Target must be > 0", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        price_alerts.append({"target": target, "direction": direction, "triggered": False})
        direction_str = "rises above" if direction == "above" else "drops below"
        embed = (
            hikari.Embed(title="🔔 Price Alert Set!", color=C_GREEN,
                         timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Price Alert", icon=LTC_ICON)
            .add_field("🎯 Alert",    f"DM when LTC **{direction_str}** ${target:,.2f} USD", inline=False)
            .add_field("💵 Current",  f"${ltc_price_usd:,.2f} USD",                          inline=True)
            .add_field("🔔 Notifying", notify_list_str(),                                    inline=False)
            .set_footer(text=f"Checks every {POLL_INTERVAL}s")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /pricealerts ──────────────────────────────────────────
    elif cmd == "pricealerts":
        active = [a for a in price_alerts if not a.get("triggered")]
        if not active:
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="📭 No Active Alerts",
                    description="Set one with `/pricealert <target> <above|below>`", color=C_GREY),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        lines = []
        for a in active:
            arrow = "📈 Above" if a["direction"] == "above" else "📉 Below"
            lines.append(f"• {arrow} **${a['target']:,.2f} USD**")
        embed = (
            hikari.Embed(title=f"🔔 Active Price Alerts ({len(active)})",
                         color=C_LTC, timestamp=datetime.now(timezone.utc))
            .add_field("Alerts", "\n".join(lines), inline=False)
            .add_field("💵 Current Price", f"${ltc_price_usd:,.2f} USD", inline=True)
            .set_footer(text=f"Checks every {POLL_INTERVAL}s")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /removealert ──────────────────────────────────────────
    elif cmd == "removealert":
        target = float(opt(ix, "target") or 0)
        before = len(price_alerts)
        price_alerts[:] = [a for a in price_alerts if a["target"] != target]
        removed = before - len(price_alerts)
        if removed:
            embed = hikari.Embed(title="🗑️ Alert Removed",
                description=f"Removed alert for **${target:,.2f} USD**", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Found",
                description=f"No alert found for **${target:,.2f} USD**", color=C_RED)
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /setnotify ────────────────────────────────────────────
    elif cmd == "setnotify":
        action  = str(opt(ix, "action") or "list")
        user_id_raw = opt(ix, "user_id")

        if action == "list":
            embed = (
                hikari.Embed(title="🔔 DM Notification List", color=C_LTC,
                             timestamp=datetime.now(timezone.utc))
                .add_field("Users being notified", notify_list_str(), inline=False)
                .set_footer(text=f"{len(notify_user_ids)} user(s) total")
            )
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=embed, flags=hikari.MessageFlag.EPHEMERAL)
            return

        if not user_id_raw or not str(user_id_raw).strip().isdigit():
            await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Invalid User ID",
                    description="Provide a valid numeric Discord user ID.\nRight-click a user → Copy User ID (needs Developer Mode on).",
                    color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return

        uid = int(str(user_id_raw).strip())

        if action == "add":
            if uid in notify_user_ids:
                desc = f"<@{uid}> is already in the notification list."
                color = C_GREY
            else:
                notify_user_ids.append(uid)
                desc = f"<@{uid}> will now receive DM notifications."
                color = C_GREEN
        else:  # remove
            if uid in notify_user_ids:
                notify_user_ids.remove(uid)
                desc = f"<@{uid}> removed from notifications."
                color = C_GREY
            else:
                desc = f"<@{uid}> was not in the notification list."
                color = C_RED

        embed = (
            hikari.Embed(title="🔔 Notification List Updated", description=desc,
                         color=color, timestamp=datetime.now(timezone.utc))
            .add_field("Current List", notify_list_str(), inline=False)
            .set_footer(text=f"{len(notify_user_ids)} user(s) total")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE,
            embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /fees ─────────────────────────────────────────────────
    elif cmd == "fees":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data = await fetch_network()
        if not data:
            await ix.edit_initial_response("❌ Could not reach Blockchair API.")
            return
        # Blockchair provides suggested_transaction_fee_per_byte_sat
        fee_sat = data.get("suggested_transaction_fee_per_byte_sat", None)
        mempool = data.get("mempool_transactions", "N/A")
        mempool_size = data.get("mempool_size", "N/A")
        embed = (
            hikari.Embed(title="⛽ Litecoin Network Fees", color=C_LTC,
                         timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Fee Estimator", icon=LTC_ICON)
        )
        if fee_sat:
            # Typical TX is ~226 bytes
            slow_fee   = fee_sat * 226 / 1e8
            medium_fee = fee_sat * 1.5 * 226 / 1e8
            fast_fee   = fee_sat * 3 * 226 / 1e8
            embed.add_field("🐢 Slow",   f"`{slow_fee:.6f} LTC`" + (f"\n${slow_fee*ltc_price_usd:.4f} USD"   if ltc_price_usd else ""), inline=True)
            embed.add_field("🚶 Medium", f"`{medium_fee:.6f} LTC`" + (f"\n${medium_fee*ltc_price_usd:.4f} USD" if ltc_price_usd else ""), inline=True)
            embed.add_field("🚀 Fast",   f"`{fast_fee:.6f} LTC`" + (f"\n${fast_fee*ltc_price_usd:.4f} USD"   if ltc_price_usd else ""), inline=True)
            embed.add_field("📊 Fee/byte", f"`{fee_sat} sat/byte`", inline=True)
        else:
            embed.add_field("ℹ️ Info", "Fee data unavailable — network may be very low activity.", inline=False)
        embed.add_field("⏱️ Mempool TXs",  f"`{mempool}`",      inline=True)
        embed.add_field("📦 Mempool Size", f"`{mempool_size}`",  inline=True)
        embed.set_footer(text="Blockchair API")
        await ix.edit_initial_response(embed=embed)

    # ── /ltcstats ─────────────────────────────────────────────
    elif cmd == "ltcstats":
        await ix.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data = await fetch_network()
        if not data:
            await ix.edit_initial_response("❌ Could not reach Blockchair API.")
            return
        blocks = data.get("blocks", "N/A")
        embed = (
            hikari.Embed(title="⛏️ Litecoin Network Stats", color=C_LTC,
                         timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Network", icon=LTC_ICON)
            .add_field("📦 Block Height",     f"`{blocks:,}`" if isinstance(blocks, int) else f"`{blocks}`", inline=True)
            .add_field("⏱️ Unconfirmed TXs",  f"`{data.get('mempool_transactions','N/A')}`",                  inline=True)
            .add_field("📊 24h Transactions", f"`{data.get('transactions_24h','N/A')}`",                      inline=True)
            .add_field("⛏️ Difficulty",       f"`{data.get('difficulty','N/A')}`",                            inline=True)
            .add_field("📈 24h Volume",       f"`{data.get('volume_24h','N/A')} LTC`",                        inline=True)
            .add_field("🔗 Best Block",       f"`{str(data.get('best_block_hash','N/A'))[:24]}…`",            inline=True)
            .set_footer(text="Blockchair API")
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
                "`/checktx <txid>` — Look up any LTC TX\n"
                "`/txhistory <addr> [limit]` — Last transactions for address\n"
                "`/watch <addr> [label]` — Watch address for new TXs\n"
                "`/unwatch <addr>` — Stop watching\n"
                "`/watchlist` — All watched addresses & TXs\n"
                "`/balance <addr>` — Address balance (LTC + USD)\n"
                "`/qr <addr> [amount]` — Generate QR code"), inline=False)
            .add_field("💼 Portfolio", (
                "`/portfolio` — Total balance across all wallets\n"
                "`/addwallet <label> <addr>` — Add wallet (no alerts)\n"
                "`/removewallet <label>` — Remove wallet"), inline=False)
            .add_field("🧾 Invoices", (
                "`/invoice <addr> <amount> <LTC|USD> [desc]` — Create invoice + QR\n"
                "`/invoicestatus <id>` — Check invoice status\n"
                "`/invoicelist` — View all open invoices\n"
                "`/expireinvoice <id>` — Mark invoice expired"), inline=False)
            .add_field("💱 Tools", (
                "`/convert <amount> <LTC→USD|USD→LTC>` — Converter\n"
                "`/pricealert <target> <above|below>` — DM when LTC hits price\n"
                "`/pricealerts` — List active alerts\n"
                "`/removealert <target>` — Remove a price alert\n"
                "`/fees` — Current network fee estimates\n"
                "`/ltcstats` — Live network stats"), inline=False)
            .add_field("⚙️ Settings", (
                "`/setnotify add <user_id>` — Add user to DM list\n"
                "`/setnotify remove <user_id>` — Remove user from DM list\n"
                "`/setnotify list` — Show who gets DMs"), inline=False)
            .add_field("🔔 Auto Notifications", (
                f"New TX • Confs 1→3→{REQUIRED_CONFS} • 🏆 Balance highs • 🎯 Price alerts\n"
                f"Currently notifying:\n{notify_list_str()}"), inline=False)
            .set_footer(text="Blockchair + CoinGecko • No API key needed")
        )
        await ix.create_initial_response(hikari.ResponseType.MESSAGE_CREATE, embed=embed)

# ──────────────────────────────────────────────────────────────
# STARTED
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    global ltc_price_usd
    print(f"✅ Bot online | Polling every {POLL_INTERVAL}s | Confs: {REQUIRED_CONFS}")
    print(f"🔔 Notifying: {notify_user_ids}")
    if not notify_user_ids:
        print("⚠️  Set NOTIFY_USER_IDS=id1,id2,id3 in Railway Variables!")

    ltc_price_usd = await get_ltc_price()
    print(f"💵 LTC = ${ltc_price_usd:,.2f} USD")

    if WATCH_ADDRESS:
        last    = await fetch_latest_tx_hash(WATCH_ADDRESS)
        balance = await fetch_address_balance(WATCH_ADDRESS)
        watched_addresses[WATCH_ADDRESS] = {
            "channel_id": None, "last_tx_hash": last,
            "high_watermark_ltc": balance, "label": "Main Wallet"
        }
        portfolio["Main Wallet"] = WATCH_ADDRESS
        print(f"👁️ Auto-watching: {WATCH_ADDRESS} ({balance:.4f} LTC)")

    asyncio.create_task(poll_loop())

bot.run()
