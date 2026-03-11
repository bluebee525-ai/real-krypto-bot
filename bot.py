"""
LTC Discord Bot • Python 3.13 • hikari + hikari-lightbulb
No audioop dependency. Works on Python 3.13 out of the box.
"""

import os
import io
import asyncio
from datetime import datetime, timezone

import aiohttp
import hikari
import lightbulb
import qrcode

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
NOTIFY_USER_ID = int(os.environ.get("NOTIFY_USER_ID", "0"))
WATCH_ADDRESS  = os.environ.get("LTC_WATCH_ADDRESS", "")
POLL_INTERVAL  = int(os.environ.get("POLL_INTERVAL", "30"))
REQUIRED_CONFS = int(os.environ.get("REQUIRED_CONFS", "6"))

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
bot = hikari.GatewayBot(token=BOT_TOKEN)
cli = lightbulb.client_from_app(bot)

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

def make_qr(address: str, amount: float) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(f"litecoin:{address}?amount={amount}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#345D9D", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
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

    embed = (
        hikari.Embed(title=title, url=f"https://sochain.com/tx/LTC/{txid}",
                     color=conf_color(confs), timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Network", icon=LTC_ICON)
        .add_field("🆔 TXID",    f"```{txid}```",                             inline=False)
        .add_field("💰 Amount",  f"**{tx['total_ltc']:.8f} LTC**",            inline=True)
        .add_field("⛽ Fee",     f"{tx['fee_ltc']:.8f} LTC",                  inline=True)
        .add_field("📦 Size",    f"{tx['size']} bytes",                       inline=True)
        .add_field("📊 Status",  f"{status_label(confs)}\n{conf_bar(confs)}", inline=False)
        .add_field("📤 From",    sender_str,                                  inline=True)
        .add_field("📥 To",      receiver_str,                                inline=True)
        .add_field("🕐 Time",    fmt_time(tx["time"]),                        inline=False)
        .set_footer(text="Sochain Explorer", icon=LTC_ICON)
    )
    return embed


def invoice_embed(inv: dict) -> hikari.Embed:
    labels = {
        "pending": ("⏳ Awaiting Payment", C_ORANGE),
        "paid":    ("✅ Paid",              C_GREEN),
        "expired": ("❌ Expired",           C_RED),
    }
    label, color = labels.get(inv["status"], ("❓ Unknown", C_GREY))
    embed = (
        hikari.Embed(title=f"🧾 Invoice #{inv['id']}",
                     description=f"**{inv.get('description','Litecoin Payment')}**",
                     color=color, timestamp=datetime.now(timezone.utc))
        .set_author(name="Litecoin Invoice", icon=LTC_ICON)
        .add_field("💎 Amount", f"**{inv['amount']} LTC**", inline=True)
        .add_field("📌 Status", label,                      inline=True)
        .add_field("🔢 ID",     f"`#{inv['id']}`",          inline=True)
        .add_field("📬 Pay To", f"```{inv['address']}```",  inline=False)
        .add_field("📋 Instructions",
                   f"Send exactly **{inv['amount']} LTC** to the address above.\n"
                   f"The bot will auto-detect your payment.", inline=False)
        .set_footer(text=f"Created by {inv['creator']}")
    )
    if inv.get("txid"):
        embed.add_field("🔗 TXID", f"```{inv['txid']}```", inline=False)
    return embed


# ──────────────────────────────────────────────────────────────
# DM / NOTIFY HELPERS
# ──────────────────────────────────────────────────────────────
async def dm_user(user_id: int, embed: hikari.Embed, attachment: bytes | None = None):
    if not user_id:
        return
    try:
        dm = await bot.rest.create_dm_channel(user_id)
        if attachment:
            await bot.rest.create_message(dm.id, embed=embed,
                                          attachment=hikari.Bytes(attachment, "qr.png"))
        else:
            await bot.rest.create_message(dm.id, embed=embed)
    except Exception as e:
        print(f"[DM] {user_id}: {e}")


async def notify(embed: hikari.Embed, channel_id: int | None, user_id: int | None):
    if channel_id:
        try:
            await bot.rest.create_message(channel_id, embed=embed)
        except Exception as e:
            print(f"[Notify channel] {e}")
    if user_id:
        await dm_user(user_id, embed)


# ──────────────────────────────────────────────────────────────
# BACKGROUND POLLING
# ──────────────────────────────────────────────────────────────
async def poll_loop():
    await bot.wait_for(hikari.StartedEvent, timeout=None)
    print("🔄 Polling started")
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        await _poll_addresses()
        await _poll_txids()


async def _poll_addresses():
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
            embed.add_field("📍 Watched Address", f"`{address}`", inline=False)
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


async def _poll_txids():
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
                                url=f"https://sochain.com/tx/LTC/{txid}",
                                color=C_GREEN, timestamp=datetime.now(timezone.utc))
                            .set_author(name="Litecoin Network", icon=LTC_ICON)
                            .add_field("🆔 TXID",          f"```{txid}```", inline=False)
                            .add_field("📊 Confirmations", conf_bar(confs), inline=False)
                        )
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
@cli.register
class CheckTx(lightbulb.SlashCommand, name="checktx", description="Look up a Litecoin transaction by TXID"):
    txid: str = lightbulb.string("txid", "64-character hex transaction ID")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        txid = self.txid.strip().lower()
        if len(txid) != 64 or not all(c in "0123456789abcdef" for c in txid):
            embed = hikari.Embed(title="❌ Invalid TXID", description="Must be a 64-character hex string.", color=C_RED)
            await ctx.respond(embed)
            return
        tx = await fetch_tx(txid)
        if not tx:
            embed = hikari.Embed(title="❌ Not Found", description=f"No transaction found for:\n```{txid}```", color=C_RED)
            await ctx.respond(embed)
            return
        embed = tx_embed(tx)
        if tx["confirmations"] < REQUIRED_CONFS:
            watched_txids[txid] = {
                "channel_id": ctx.channel_id,
                "user_id":    ctx.user.id,
                "last_confs": tx["confirmations"],
                "done":       False,
            }
            embed.set_footer(text=f"👁️ Watching — DM at 1, 3 & {REQUIRED_CONFS} confs | Sochain")
        await ctx.respond(embed)
        await dm_user(ctx.user.id, embed)


@cli.register
class Watch(lightbulb.SlashCommand, name="watch", description="Watch a Litecoin address for incoming transactions"):
    address: str = lightbulb.string("address", "LTC address (starts with L, M or ltc1)")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        address = self.address.strip()
        if not (address.startswith(("L", "M", "ltc1")) and 26 <= len(address) <= 62):
            embed = hikari.Embed(title="❌ Invalid Address", description="Please provide a valid Litecoin address.", color=C_RED)
            await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        last = await fetch_latest_tx_hash(address)
        watched_addresses[address] = {"channel_id": ctx.channel_id, "last_tx_hash": last}
        embed = (
            hikari.Embed(title="👁️ Now Watching", color=C_LTC, timestamp=datetime.now(timezone.utc))
            .set_author(name="Litecoin Monitor", icon=LTC_ICON)
            .add_field("📬 Address",       f"```{address}```",                                    inline=False)
            .add_field("🔔 Notifications", f"DM on new TX\nConf updates: 1 → 3 → {REQUIRED_CONFS}", inline=False)
            .set_footer(text=f"Polling every {POLL_INTERVAL}s • Sochain API")
        )
        await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)


@cli.register
class Unwatch(lightbulb.SlashCommand, name="unwatch", description="Stop watching a Litecoin address"):
    address: str = lightbulb.string("address", "LTC address to stop monitoring")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        address = self.address.strip()
        if address in watched_addresses:
            del watched_addresses[address]
            embed = hikari.Embed(title="🛑 Stopped Watching", description=f"`{address}`", color=C_GREY)
        else:
            embed = hikari.Embed(title="❓ Not Watching", description=f"`{address}` was not being watched.", color=C_RED)
        await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)


@cli.register
class Watchlist(lightbulb.SlashCommand, name="watchlist", description="Show all watched addresses and transactions"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        if not watched_addresses and not watched_txids:
            embed = hikari.Embed(title="📭 Watch List Empty", description="Use `/watch <address>` to start.", color=C_GREY)
            await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        embed = hikari.Embed(title="👁️ Watch List", color=C_LTC, timestamp=datetime.now(timezone.utc))
        if watched_addresses:
            embed.add_field(f"📬 Addresses ({len(watched_addresses)})",
                            "\n".join(f"• `{a}`" for a in list(watched_addresses)[:15]), inline=False)
        active = {k: v for k, v in watched_txids.items() if not v.get("done")}
        if active:
            embed.add_field(f"🔗 Active TXs ({len(active)})",
                            "\n".join(f"• `{t[:20]}…` — {s['last_confs']} conf(s)" for t, s in list(active.items())[:10]),
                            inline=False)
        await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)


@cli.register
class Invoice(lightbulb.SlashCommand, name="invoice", description="Create a Litecoin payment invoice with QR code"):
    address:     str   = lightbulb.string("address",     "Your LTC receiving address")
    amount:      float = lightbulb.number("amount",      "Amount in LTC")
    description: str   = lightbulb.string("description", "What the payment is for", default="Litecoin Payment")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        global invoice_seq
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        if self.amount <= 0:
            await ctx.respond("❌ Amount must be greater than 0.")
            return
        invoice_seq += 1
        inv = {
            "id":          f"{invoice_seq:04d}",
            "address":     self.address.strip(),
            "amount":      round(self.amount, 8),
            "description": self.description,
            "status":      "pending",
            "txid":        None,
            "creator":     str(ctx.user),
            "channel_id":  ctx.channel_id,
            "user_id":     ctx.user.id,
        }
        invoices[inv["id"]] = inv
        if self.address not in watched_addresses:
            last = await fetch_latest_tx_hash(self.address)
            watched_addresses[self.address] = {"channel_id": ctx.channel_id, "last_tx_hash": last}
        embed = invoice_embed(inv)
        qr_bytes = make_qr(self.address, self.amount)
        await ctx.respond(embed, attachment=hikari.Bytes(qr_bytes, "qr.png"))
        await dm_user(ctx.user.id, invoice_embed(inv), qr_bytes)


@cli.register
class InvoiceStatus(lightbulb.SlashCommand, name="invoicestatus", description="Check the status of an invoice"):
    invoice_id: str = lightbulb.string("invoice_id", "Invoice ID e.g. 0001")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        inv = invoices.get(self.invoice_id.zfill(4))
        if not inv:
            embed = hikari.Embed(title="❌ Not Found", description=f"No invoice `{self.invoice_id}`.", color=C_RED)
            await ctx.respond(embed, flags=hikari.MessageFlag.EPHEMERAL)
            return
        await ctx.respond(invoice_embed(inv))


@cli.register
class LtcStats(lightbulb.SlashCommand, name="ltcstats", description="Show live Litecoin network stats"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
        await ctx.respond(hikari.ResponseType.DEFERRED_MESSAGE_CREATE)
        data = await fetch_network()
        if not data:
            await ctx.respond("❌ Could not reach Sochain API.")
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
        embed.add_field("💵 Price",           f"`${data.get('price','N/A')} USD`",     inline=True)
        embed.add_field("⏱️ Unconfirmed TXs", f"`{data.get('unconfirmed_txs','N/A')}`", inline=True)
        embed.add_field("🔗 Hashrate",        f"`{data.get('hashrate','N/A')}`",        inline=True)
        embed.set_footer(text="Sochain API • No key required")
        await ctx.respond(embed)


@cli.register
class Help(lightbulb.SlashCommand, name="help", description="Show all bot commands"):
    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context) -> None:
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
        await ctx.respond(embed)


# ──────────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────────
@bot.listen(hikari.StartedEvent)
async def on_started(event: hikari.StartedEvent) -> None:
    print(f"✅ Bot started | Polling every {POLL_INTERVAL}s | Required confs: {REQUIRED_CONFS}")
    if WATCH_ADDRESS:
        last = await fetch_latest_tx_hash(WATCH_ADDRESS)
        watched_addresses[WATCH_ADDRESS] = {"channel_id": None, "last_tx_hash": last}
        print(f"👁️ Auto-watching: {WATCH_ADDRESS}")
    asyncio.create_task(poll_loop())


if __name__ == "__main__":
    bot.run()
