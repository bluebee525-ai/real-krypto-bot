"""
LTC Discord Bot  •  Python 3.13  •  hikari 2.5.0
Pure hikari — zero audioop dependency, works on Python 3.13.
Sochain API — no key needed.
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
GUILD_ID       = int(os.environ.get("GUILD_ID", "0"))  # Your server ID for instant command registration

SOCHAIN  = "https://sochain.com/api/v3"
LTC_ICON = "https://cryptologos.cc/logos/litecoin-ltc-logo.png"
C_LTC    = 0x345D9D
C_GREEN  = 0x2ECC71
C_RED    = 0xE74C3C
C_ORANGE = 0xF39C12
C_GREY   = 0x95A5A6

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
    logs="DEBUG",
)

watched_addresses: dict = {}
watched_txids:     dict = {}
invoices:          dict = {}
invoice_seq              = 0

# ──────────────────────────────────────────────────────────────
# SOCHAIN API
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
# EMBED BUILDERS
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
            recv_lines.append(f"`{addr}` — **{o['value_ltc']:.6f} LTC**")
    receiver_str = "\n".join(recv_lines[:3]) or "Unknown"
    if len(tx["outputs"]) > 3:
        receiver_str += f"\n*+{len(tx['outputs'])-3} more*"
    return (
        hikari.Embed(title=title, url=f"https://sochain.com/tx/LTC/{txid}",
                     color=conf_color(confs), timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Network", icon=LTC_ICON)
        .add_field("🆔 TXID",   f"```{txid}```",                             inline=False)
        .add_field("💰 Amount", f"**{tx['total_ltc']:.8f} LTC**",            inline=True)
        .add_field("⛽ Fee",    f"{tx['fee_ltc']:.8f} LTC",                  inline=True)
        .add_field("📦 Size",   f"{tx['size']} bytes",                       inline=True)
        .add_field("📊 Status", f"{status_label(confs)}\n{conf_bar(confs)}", inline=False)
        .add_field("📤 From",   sender_str,                                  inline=True)
        .add_field("📥 To",     receiver_str,                                inline=True)
        .add_field("🕐 Time",   fmt_time(tx["time"]),                        inline=False)
        .set_footer(text="View on Sochain", icon=LTC_ICON)
    )

def invoice_embed(inv: dict) -> hikari.Embed:
    labels = {"pending": ("⏳ Awaiting Payment", C_ORANGE), "paid": ("✅ Paid", C_GREEN), "expired": ("❌ Expired", C_RED)}
    label, color = labels.get(inv["status"], ("❓ Unknown", C_GREY))
    e = (
        hikari.Embed(title=f"🧾 Invoice #{inv['id']}", description=f"**{inv.get('description','Litecoin Payment')}**",
                     color=color, timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Invoice", icon=LTC_ICON)
        .add_field("💎 Amount", f"**{inv['amount']} LTC**", inline=True)
        .add_field("📌 Status", label,                      inline=True)
        .add_field("🔢 ID",     f"`#{inv['id']}`",          inline=True)
        .add_field("📬 Pay To", f"```{inv['address']}```",  inline=False)
        .add_field("📋 Instructions",
                   f"Send exactly **{inv['amount']} LTC** to the address above.\nThe bot will auto-detect your payment.",
                   inline=False)
        .set_footer(text=f"Created by {inv['creator']}")
    )
    if inv.get("txid"):
        e.add_field("🔗 TXID", f"```{inv['txid']}```", inline=False)
    return e

# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────
async def dm_user(user_id: int, embed: hikari.Embed, attachment: bytes | None = None):
    if not user_id:
        print("[DM] NOTIFY_USER_ID is 0 — set it in Railway Variables!")
        return
    try:
        print(f"[DM] Sending DM to user {user_id}...")
        dm = await bot.rest.create_dm_channel(user_id)
        kwargs: dict = {"embed": embed}
        if attachment:
            kwargs["attachment"] = hikari.Bytes(attachment, "qr.png")
        await bot.rest.create_message(dm.id, **kwargs)
        print(f"[DM] ✅ DM sent to {user_id}")
    except Exception as e:
        print(f"[DM] ❌ Failed to DM {user_id}: {e}")
        traceback.print_exc()

async def notify(embed: hikari.Embed, channel_id: int | None, user_id: int | None):
    if channel_id:
        try:
            await bot.rest.create_message(channel_id, embed=embed)
        except Exception as e:
            print(f"[Notify] {e}")
    if user_id:
        await dm_user(user_id, embed)

async def defer_and_respond(interaction: hikari.CommandInteraction, embed: hikari.Embed,
                            attachment: bytes | None = None, ephemeral: bool = False):
    flags = hikari.MessageFlag.EPHEMERAL if ephemeral else hikari.MessageFlag.NONE
    await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=flags)
    kwargs: dict = {"embed": embed}
    if attachment:
        kwargs["attachment"] = hikari.Bytes(attachment, "qr.png")
    await interaction.edit_initial_response(**kwargs)

# ──────────────────────────────────────────────────────────────
# POLLING
# ──────────────────────────────────────────────────────────────
async def poll_loop():
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        # Poll addresses
        for address, state in list(watched_addresses.items()):
            try:
                latest = await fetch_latest_tx_hash(address)
                if not latest or state.get("last_tx_hash") == latest:
                    continue
                watched_addresses[address]["last_tx_hash"] = latest
                tx = await fetch_tx(latest)
                if not tx:
                    continue
                embed = tx_embed(tx, "🚨 New Incoming Transaction!")
                embed.color = C_ORANGE
                embed.add_field("📍 Watched Address", f"`{address}`", inline=False)
                await notify(embed, state.get("channel_id"), NOTIFY_USER_ID)
                if latest not in watched_txids:
                    watched_txids[latest] = {
                        "channel_id": state.get("channel_id"), "user_id": NOTIFY_USER_ID,
                        "last_confs": tx["confirmations"], "done": tx["confirmations"] >= REQUIRED_CONFS,
                    }
            except Exception as e:
                print(f"[Poll Addr] {address}: {e}")

        # Poll txids
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
                                             url=f"https://sochain.com/tx/LTC/{txid}",
                                             color=C_GREEN, timestamp=datetime.now(timezone.utc))
                                .set_author(name="Litecoin Network", icon=LTC_ICON)
                                .add_field("🆔 TXID",          f"```{txid}```", inline=False)
                                .add_field("📊 Confirmations", conf_bar(confs), inline=False)
                            )
                            watched_txids[txid]["done"] = True
                        else:
                            embed = tx_embed(tx, f"🔄 {confs} Confirmation{'s' if confs!=1 else ''}")
                        await notify(embed, state.get("channel_id"), state.get("user_id"))
                        break
            except Exception as e:
                print(f"[Poll TX] {txid[:16]}: {e}")

# ──────────────────────────────────────────────────────────────
# REGISTER SLASH COMMANDS ON STARTUP
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartingEvent)
async def on_starting(event: hikari.StartingEvent) -> None:
    app = await bot.rest.fetch_application()

    commands = [
        bot.rest.slash_command_builder("checktx", "Look up a Litecoin transaction by TXID")
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="txid",
                description="64-character hex transaction ID", is_required=True)),

        bot.rest.slash_command_builder("watch", "Watch a Litecoin address for new incoming transactions")
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="address",
                description="LTC address (starts with L, M or ltc1)", is_required=True)),

        bot.rest.slash_command_builder("unwatch", "Stop watching a Litecoin address")
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="address",
                description="LTC address to stop monitoring", is_required=True)),

        bot.rest.slash_command_builder("watchlist", "Show all watched addresses and transactions"),

        bot.rest.slash_command_builder("invoice", "Create a Litecoin payment invoice with QR code")
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="address",
                description="Your LTC receiving address", is_required=True))
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.NUMBER, name="amount",
                description="Amount in LTC", is_required=True))
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="description",
                description="What the payment is for", is_required=False)),

        bot.rest.slash_command_builder("invoicestatus", "Check the status of an invoice")
            .add_option(hikari.CommandOption(
                type=hikari.OptionType.STRING, name="invoice_id",
                description="Invoice ID e.g. 0001", is_required=True)),

        bot.rest.slash_command_builder("ltcstats", "Show live Litecoin network stats"),
        bot.rest.slash_command_builder("help", "Show all bot commands"),
    ]

    if GUILD_ID:
        # Guild-specific = instant (use this for testing)
        await bot.rest.set_application_commands(
            application=app.id, guild=GUILD_ID, commands=commands)
        print(f"✅ Slash commands registered to guild {GUILD_ID} (instant)")
    else:
        # Global = up to 1 hour propagation
        await bot.rest.set_application_commands(
            application=app.id, commands=commands)
        print("✅ Slash commands registered globally (may take up to 1hr to appear)")

# ──────────────────────────────────────────────────────────────
# INTERACTION HANDLER
# ──────────────────────────────────────────────────────────────
def get_option(interaction: hikari.CommandInteraction, name: str):
    for opt in (interaction.options or []):
        if opt.name == name:
            return opt.value
    return None

@bot.listen(hikari.InteractionCreateEvent)
async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
    if not isinstance(event.interaction, hikari.CommandInteraction):
        return
    interaction = event.interaction
    cmd = interaction.command_name
    global invoice_seq

    # ── /checktx ──────────────────────────────────────────────
    if cmd == "checktx":
        txid = str(get_option(interaction, "txid") or "").strip().lower()
        await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        if len(txid) != 64 or not all(c in "0123456789abcdef" for c in txid):
            await interaction.edit_initial_response(embed=hikari.Embed(
                title="❌ Invalid TXID", description="Must be a 64-character hex string.", color=C_RED))
            return
        tx = await fetch_tx(txid)
        if not tx:
            await interaction.edit_initial_response(embed=hikari.Embed(
                title="❌ Not Found", description=f"No transaction found for:\n```{txid}```", color=C_RED))
            return
        embed = tx_embed(tx)
        if tx["confirmations"] < REQUIRED_CONFS:
            watched_txids[txid] = {"channel_id": interaction.channel_id, "user_id": interaction.user.id,
                                   "last_confs": tx["confirmations"], "done": False}
            embed.set_footer(text=f"👁️ Watching — DM at 1, 3 & {REQUIRED_CONFS} confs | Sochain")
        await interaction.edit_initial_response(embed=embed)
        await dm_user(interaction.user.id, embed)

    # ── /watch ────────────────────────────────────────────────
    elif cmd == "watch":
        address = str(get_option(interaction, "address") or "").strip()
        if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Invalid Address",
                                   description="Please provide a valid Litecoin address.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        last = await fetch_latest_tx_hash(address)
        watched_addresses[address] = {"channel_id": interaction.channel_id, "last_tx_hash": last}
        embed = (
            hikari.Embed(title="👁️ Now Watching", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Monitor", icon=LTC_ICON)
            .add_field("📬 Address",       f"```{address}```",                                       inline=False)
            .add_field("🔔 Notifications", f"DM on new TX\nConf updates: 1 → 3 → {REQUIRED_CONFS}", inline=False)
            .set_footer(text=f"Polling every {POLL_INTERVAL}s • Sochain API")
        )
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE, embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /unwatch ──────────────────────────────────────────────
    elif cmd == "unwatch":
        address = str(get_option(interaction, "address") or "").strip()
        if address in watched_addresses:
            del watched_addresses[address]
            embed = hikari.Embed(title="🛑 Stopped Watching", description=f"`{address}`", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Watching", description=f"`{address}` was not being watched.", color=C_RED)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE, embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /watchlist ────────────────────────────────────────────
    elif cmd == "watchlist":
        if not watched_addresses and not watched_txids:
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="📭 Watch List Empty",
                                   description="Use `/watch <address>` to start.", color=C_GREY),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        embed = hikari.Embed(title="👁️ Watch List", color=C_LTC, timestamp=datetime.now(timezone.utc))
        if watched_addresses:
            embed.add_field(f"📬 Addresses ({len(watched_addresses)})",
                            "\n".join(f"• `{a}`" for a in list(watched_addresses)[:15]), inline=False)
        active = {k: v for k, v in watched_txids.items() if not v.get("done")}
        if active:
            embed.add_field(f"🔗 Active TXs ({len(active)})",
                            "\n".join(f"• `{t[:20]}…` — {s['last_confs']} conf(s)"
                                      for t, s in list(active.items())[:10]), inline=False)
        await interaction.create_initial_response(
            hikari.ResponseType.MESSAGE_CREATE, embed=embed, flags=hikari.MessageFlag.EPHEMERAL)

    # ── /invoice ──────────────────────────────────────────────
    elif cmd == "invoice":
        await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        address     = str(get_option(interaction, "address") or "").strip()
        amount      = float(get_option(interaction, "amount") or 0)
        description = str(get_option(interaction, "description") or "Litecoin Payment")
        if amount <= 0:
            await interaction.edit_initial_response("❌ Amount must be greater than 0.")
            return
        invoice_seq += 1
        inv = {"id": f"{invoice_seq:04d}", "address": address, "amount": round(amount, 8),
               "description": description, "status": "pending", "txid": None,
               "creator": str(interaction.user), "channel_id": interaction.channel_id,
               "user_id": interaction.user.id}
        invoices[inv["id"]] = inv
        if address not in watched_addresses:
            last = await fetch_latest_tx_hash(address)
            watched_addresses[address] = {"channel_id": interaction.channel_id, "last_tx_hash": last}
        qr_bytes = make_qr(address, amount)
        await interaction.edit_initial_response(
            embed=invoice_embed(inv), attachment=hikari.Bytes(qr_bytes, "qr.png"))
        await dm_user(interaction.user.id, invoice_embed(inv), qr_bytes)

    # ── /invoicestatus ────────────────────────────────────────
    elif cmd == "invoicestatus":
        inv_id = str(get_option(interaction, "invoice_id") or "").zfill(4)
        inv    = invoices.get(inv_id)
        if not inv:
            await interaction.create_initial_response(
                hikari.ResponseType.MESSAGE_CREATE,
                embed=hikari.Embed(title="❌ Not Found", description=f"No invoice `{inv_id}`.", color=C_RED),
                flags=hikari.MessageFlag.EPHEMERAL)
            return
        await interaction.create_initial_response(hikari.ResponseType.MESSAGE_CREATE, embed=invoice_embed(inv))

    # ── /ltcstats ─────────────────────────────────────────────
    elif cmd == "ltcstats":
        await interaction.create_initial_response(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data = await fetch_network()
        if not data:
            await interaction.edit_initial_response("❌ Could not reach Sochain API.")
            return
        embed = hikari.Embed(title="⛏️ Litecoin Network Stats", color=C_LTC, timestamp=datetime.now(timezone.utc))
        embed.set_author(name="Litecoin Network", icon=LTC_ICON)
        blocks = data.get("blocks", "N/A")
        embed.add_field("📦 Block Height",    f"`{blocks:,}`" if isinstance(blocks, int) else f"`{blocks}`", inline=True)
        embed.add_field("🌐 Network",         f"`{data.get('network','LTC')}`",                              inline=True)
        try:
            embed.add_field("⛏️ Difficulty",  f"`{float(data.get('difficulty',0)):,.0f}`",                  inline=True)
        except Exception:
            embed.add_field("⛏️ Difficulty",  f"`{data.get('difficulty','N/A')}`",                          inline=True)
        embed.add_field("💵 Price",           f"`${data.get('price','N/A')} USD`",       inline=True)
        embed.add_field("⏱️ Unconfirmed TXs", f"`{data.get('unconfirmed_txs','N/A')}`",  inline=True)
        embed.add_field("🔗 Hashrate",        f"`{data.get('hashrate','N/A')}`",          inline=True)
        embed.set_footer(text="Sochain API • No key required")
        await interaction.edit_initial_response(embed=embed)

    # ── /help ─────────────────────────────────────────────────
    elif cmd == "help":
        embed = (
            hikari.Embed(title="🪙 LTC Bot — Help",
                         description="Litecoin transaction monitor, watcher & invoice bot.",
                         color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="LTC Bot", icon=LTC_ICON)
            .add_field("🔍 Transactions", (
                "`/checktx <txid>` — Look up any LTC transaction\n"
                "`/watch <addr>` — Watch address for new TXs\n"
                "`/unwatch <addr>` — Stop watching\n"
                "`/watchlist` — View all watched addresses & TXs"), inline=False)
            .add_field("🧾 Invoices", (
                "`/invoice <addr> <amount> [desc]` — Create invoice + QR code\n"
                "`/invoicestatus <id>` — Check invoice status"), inline=False)
            .add_field("📡 Network", "`/ltcstats` — Live Litecoin network stats", inline=False)
            .add_field("🔔 Auto Notifications", (
                f"DM alerts on new incoming TXs\n"
                f"Confirmation DMs at **1 → 3 → {REQUIRED_CONFS}** confs\n"
                f"Polls every **{POLL_INTERVAL}s**"), inline=False)
            .set_footer(text="Powered by Sochain API • No API key needed")
        )
        await interaction.create_initial_response(hikari.ResponseType.MESSAGE_CREATE, embed=embed)

# ──────────────────────────────────────────────────────────────
# STARTED EVENT
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    print(f"✅ Bot online | Polling every {POLL_INTERVAL}s | Confs required: {REQUIRED_CONFS}")
    print(f"🔔 NOTIFY_USER_ID = {NOTIFY_USER_ID}")
    print(f"📬 WATCH_ADDRESS  = {WATCH_ADDRESS or 'NOT SET'}")
    if not NOTIFY_USER_ID:
        print("⚠️  WARNING: NOTIFY_USER_ID is not set — DMs will not be sent!")
    if WATCH_ADDRESS:
        last = await fetch_latest_tx_hash(WATCH_ADDRESS)
        watched_addresses[WATCH_ADDRESS] = {"channel_id": None, "last_tx_hash": last}
        print(f"👁️ Auto-watching: {WATCH_ADDRESS}")
    asyncio.create_task(poll_loop())

bot.run()
