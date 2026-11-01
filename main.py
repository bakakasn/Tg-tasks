import os
import logging
import asyncio
import tempfile
import http.cookiejar
import requests
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from motor.motor_asyncio import AsyncIOMotorClient
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeExpiredError, PhoneCodeInvalidError
from bson import ObjectId

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  ENV CONFIG
# ══════════════════════════════════════════════════════
BOT_TOKEN       = os.getenv("BOT_TOKEN")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "6020799349"))
MONGO_URI       = os.getenv("MONGO_URI", "mongodb+srv://natak20342_db_user:HY5Lr00j1Y0IXuTy@cluster0.pvzgysx.mongodb.net/")
CHANNEL_ID      = os.getenv("CHANNEL_ID", "")
API_ID          = int(os.getenv("API_ID", "39632094"))
API_HASH        = os.getenv("API_HASH", "858376a6592cc67856f47571f580b0d6")
SITE_URL        = os.getenv("SITE_URL", "https://77uai.com")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
COOKIE_FILE     = os.getenv("COOKIE_FILE", "cookies.txt")
TASK_REWARD     = 0.25   # INR per task
TASK_TIMEOUT    = 540    # 9 min

# ══════════════════════════════════════════════════════
#  MONGODB
# ══════════════════════════════════════════════════════
_db          = AsyncIOMotorClient(MONGO_URI)["tgbot"]
users_col    = _db["users"]
accounts_col = _db["tg_accounts"]
tasks_col    = _db["tasks"]
withdraw_col = _db["withdrawals"]

# ══════════════════════════════════════════════════════
#  CONVERSATION STATES
# ══════════════════════════════════════════════════════
ADD_PHONE, ADD_OTP, ADD_2FA, W_AMOUNT, W_UPI = range(5)

# ══════════════════════════════════════════════════════
#  HTTP SERVER  (Render free tier keep-alive)
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

# ══════════════════════════════════════════════════════
#  ASYNCIO FIX  (Python 3.12+)
# ══════════════════════════════════════════════════════
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
#  COOKIES
# ══════════════════════════════════════════════════════
def cookie_jar():
    jar = http.cookiejar.MozillaCookieJar()
    if COOKIES_CONTENT:
        content = COOKIES_CONTENT.replace("\\n", "\n")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(content); tmp.close()
        try:
            jar = http.cookiejar.MozillaCookieJar(tmp.name)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning(f"cookie env: {e}")
    else:
        try:
            jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning(f"cookie file: {e}")
    return jar

def site(method, endpoint, body=None):
    r = requests.request(method, SITE_URL + endpoint,
        cookies=cookie_jar(),
        headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
            "Accept": "application/json, */*",
            "Content-Type": "application/json",
            "Referer": SITE_URL, "Origin": SITE_URL,
        }, json=body, timeout=15)
    try: return r.status_code, r.json()
    except: return r.status_code, {"raw": r.text}

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
    # Reply keyboard — hamesha niche dikhega
    return ReplyKeyboardMarkup([
        ["📋 TG Task"],
        ["➕ Add TG Account", "💸 Withdrawal"],
    ], resize_keyboard=True)

def kb_back():
    # Sirf inline back button jahan zaruri ho
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])

def kb_join():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Channel Join Karo", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"),
        InlineKeyboardButton("✅ Check Kiya", callback_data="check_join"),
    ]])

# ══════════════════════════════════════════════════════
#  FORCE JOIN
# ══════════════════════════════════════════════════════
async def joined(uid, bot):
    if not CHANNEL_ID: return True
    try:
        m = await bot.get_chat_member(CHANNEL_ID, uid)
        return m.status not in ("left", "kicked")
    except: return False

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
            parse_mode="Markdown", reply_markup=kb_join()
        )
        return

    u = await get_user(uid)
    await update.message.reply_text(
        f"👋 *Namaste {name}!*\n\n"
        f"💰 Balance: ₹{u['balance']:.2f}\n\n"
        f"Niche se option choose karo 👇",
        parse_mode="Markdown",
        reply_markup=kb_main()
    )

async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if await joined(uid, ctx.bot):
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *Join confirm!*\n\n💰 Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown"
        )
        await ctx.bot.send_message(uid,
            "Ab bot use kar sakte ho 👇",
            reply_markup=kb_main()
        )
    else:
        await q.answer("❌ Abhi join nahi kiya!", show_alert=True)

# ══════════════════════════════════════════════════════
#  TG TASK  (Reply keyboard se trigger)
# ══════════════════════════════════════════════════════
async def msg_tg_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await joined(uid, ctx.bot):
        await update.message.reply_text("🔒 Pehle channel join karo!", reply_markup=kb_join())
        return

    acc = await accounts_col.find_one({"uid": uid, "status": "accepted"})
    if not acc:
        await update.message.reply_text(
            "⚠️ *Koi accepted account nahi hai.*\n\n"
            "➕ Add TG Account se pehle account add karo, admin approve karega.",
            parse_mode="Markdown"
        )
        return

    # Existing pending task check
    existing = await tasks_col.find_one({"uid": uid, "status": "pending"})
    if existing:
        left = int((existing["expires_at"] - datetime.now()).total_seconds())
        if left > 0:
            await _send_task(update.message, existing, left)
            return
        else:
            await tasks_col.update_one({"_id": existing["_id"]}, {"$set": {"status": "expired"}})

    # Naya task fetch
    wait_msg = await update.message.reply_text("⏳ Task le rahe hain...")
    account_id = acc.get("site_account_id")

    loop = asyncio.get_event_loop()
    _, data = await loop.run_in_executor(None, site, "POST", "/api/tasks/claim",
                                          {"channel": "telegram", "account_id": account_id})
    if not data.get("ok"):
        _, data = await loop.run_in_executor(None, site, "GET", "/api/tasks/current?channel=telegram")

    task_data = data.get("data")
    await wait_msg.delete()

    if not task_data:
        await update.message.reply_text("❌ Abhi koi task nahi hai. Thodi der baad try karo.")
        return

    expires_at = datetime.now() + timedelta(seconds=TASK_TIMEOUT)
    doc = {
        "uid": uid, "account_id": account_id,
        "target_phone": task_data.get("target_phone", ""),
        "ad_copy": task_data.get("ad_copy", ""),
        "status": "pending",
        "created_at": datetime.now(),
        "expires_at": expires_at,
    }
    res = await tasks_col.insert_one(doc)
    doc["_id"] = res.inserted_id
    await _send_task(update.message, doc, TASK_TIMEOUT)

async def _send_task(msg, task, time_left):
    mins, secs = time_left // 60, time_left % 60
    text = (
        f"📋 *TASK*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 *Number:* `+{task['target_phone']}`\n"
        f"⏳ *Time:* {mins}m {secs}s\n"
        f"💰 *Reward:* ₹{TASK_REWARD:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 *Yeh message bhejo:*\n\n"
        f"`{task['ad_copy']}`\n\n"
        f"👆 Upar wala message copy karo, Telegram mein number pe bhejo, phir Done dabao."
    )
    # Sirf Done button — Back nahi chahiye task screen pe
    await msg.reply_text(text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Done — Check Karo", callback_data=f"task_done_{task['_id']}")]
        ])
    )

async def cb_task_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Check ho raha hai...")
    uid = q.from_user.id
    tid = q.data.replace("task_done_", "")

    task = await tasks_col.find_one({"_id": ObjectId(tid), "uid": uid})
    if not task:
        await q.edit_message_text("❌ Task nahi mila.")
        return
    if task["status"] != "pending":
        await q.edit_message_text(f"ℹ️ Task {task['status']} hai.")
        return
    if datetime.now() > task["expires_at"]:
        await tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "expired"}})
        await q.edit_message_text(
            "⏰ *Task expire ho gaya!*\n\nNaya task lo.",
            parse_mode="Markdown"
        )
        return

    await q.edit_message_text("⏳ Verify ho raha hai... (10 sec)")

    loop = asyncio.get_event_loop()

    # Balance pehle
    _, mb = await loop.run_in_executor(None, site, "GET", "/api/me")
    bal_before = mb.get("data", {}).get("points_balance", 0)

    # Check call
    await loop.run_in_executor(None, site, "POST", "/api/tasks/check", {"channel": "telegram"})
    await asyncio.sleep(10)

    # Balance baad mein
    _, ma = await loop.run_in_executor(None, site, "GET", "/api/me")
    bal_after = ma.get("data", {}).get("points_balance", 0)

    if bal_after > bal_before:
        await tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "completed"}})
        await add_bal(uid, TASK_REWARD)
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *TASK COMPLETE!*\n\n"
            f"💰 +₹{TASK_REWARD:.2f} mili!\n"
            f"📊 Total Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown"
        )
    else:
        await q.edit_message_text(
            f"❌ *Verify nahi hua*\n\n"
            f"• Message bheja?\n"
            f"• Sahi number pe bheja?\n"
            f"• Sahi account se bheja?\n\n"
            f"Message bhejo phir check karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Dobara Check Karo", callback_data=f"task_done_{tid}")]
            ])
        )

# ══════════════════════════════════════════════════════
#  ADD ACCOUNT  (Telethon OTP flow)
# ══════════════════════════════════════════════════════
async def msg_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 *Account Add Karo*\n\n"
        "Apna Indian number bhejo:\n"
        "Format: `+91XXXXXXXXXX`\n\n"
        "/cancel to cancel",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_PHONE

async def got_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    uid = update.effective_user.id
    ctx.user_data["phone"] = phone
    await update.message.reply_text(f"⏳ `{phone}` pe OTP bhej rahe hain...", parse_mode="Markdown")
    try:
        tg = TelegramClient(f"sessions/{uid}", API_ID, API_HASH)
        await tg.connect()
        res = await tg.send_code_request(phone)
        ctx.user_data["phone_code_hash"] = res.phone_code_hash
        ctx.user_data["tg_client"] = tg
        await update.message.reply_text(
            "✅ OTP bheja!\n\nTelegram app mein OTP aaya hoga, woh yahan type karo:\n\n/cancel to cancel"
        )
        return ADD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=kb_main())
        return ConversationHandler.END

async def got_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tg: TelegramClient = ctx.user_data.get("tg_client")
    try:
        await tg.sign_in(phone=ctx.user_data["phone"], code=update.message.text.strip(),
                         phone_code_hash=ctx.user_data["phone_code_hash"])
        me = await tg.get_me()
        await _save_request(uid, ctx.user_data["phone"], me, tg)
        await update.message.reply_text(
            "✅ *Request bhej di!*\n\nAdmin approve karega, notification aayega.",
            parse_mode="Markdown", reply_markup=kb_main()
        )
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 2FA password enter karo:\n\n/cancel to cancel")
        return ADD_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text("❌ OTP galat ya expire! Dobara /start karo.", reply_markup=kb_main())
        return ConversationHandler.END
    except Exception as e:
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
            parse_mode="Markdown", reply_markup=kb_main()
        )
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA Error: {e}", reply_markup=kb_main())
        return ConversationHandler.END

async def _save_request(uid, phone, me, tg):
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await accounts_col.insert_one({
        "uid": uid, "phone": phone, "tg_id": me.id,
        "name": name, "username": me.username,
        "status": "pending", "site_account_id": None,
        "requested_at": datetime.now(),
    })
    bot = Bot(BOT_TOKEN)
    # Admin notification — Accept/Reject inline buttons
    await bot.send_message(ADMIN_ID,
        f"🔔 *New Account Request*\n\n"
        f"👤 Name: {name}\n"
        f"📱 Phone: `{phone}`\n"
        f"🆔 TG ID: `{me.id}`\n"
        f"👤 User ID: `{uid}`\n\n"
        f"Accept karte waqt site account ID bhejo.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"acc_accept_{uid}_{me.id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"acc_reject_{uid}_{me.id}"),
        ]])
    )
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
        q.message.text + "\n\n⏳ *Site Account ID type karo (sirf number):*",
        parse_mode="Markdown"
    )
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
    await q.edit_message_text(q.message.text + "\n\n❌ *REJECTED*", parse_mode="Markdown")
    await Bot(BOT_TOKEN).send_message(uid,
        "❌ Aapka account reject ho gaya.\nDobara try karo ya admin se contact karo.",
        reply_markup=kb_main()
    )

async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not ctx.user_data.get("wait_id"): return
    txt = update.message.text.strip()
    if not txt.isdigit():
        await update.message.reply_text("❌ Sirf number bhejo."); return
    site_id = int(txt)
    uid    = ctx.user_data["p_uid"]
    tg_id  = ctx.user_data["p_tg_id"]
    await accounts_col.update_one(
        {"uid": uid, "tg_id": tg_id},
        {"$set": {"status": "accepted", "site_account_id": site_id, "accepted_at": datetime.now()}}
    )
    ctx.user_data["wait_id"] = False
    await update.message.reply_text(f"✅ Accepted! Site ID: {site_id}")
    await Bot(BOT_TOKEN).send_message(uid,
        "🎉 *Aapka TG Account Accept Ho Gaya!*\n\nAb TG Task kar sakte ho.",
        parse_mode="Markdown", reply_markup=kb_main()
    )

# ══════════════════════════════════════════════════════
#  WITHDRAWAL  (Reply keyboard se trigger)
# ══════════════════════════════════════════════════════
async def msg_withdrawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    await update.message.reply_text(
        f"💸 *Withdrawal*\n\n"
        f"💰 Available: ₹{u['balance']:.2f}\n\n"
        f"Kitna withdraw karna hai?\n_(Minimum ₹10)_\n\n/cancel to cancel",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
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
        f"📲 Apna UPI ID bhejo:\n_(e.g. name@upi)_\n\n/cancel to cancel",
        parse_mode="Markdown"
    )
    return W_UPI

async def got_w_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    amt = ctx.user_data.get("w_amt", 0)
    upi = update.message.text.strip()
    await users_col.update_one({"uid": uid}, {"$inc": {"balance": -amt}})
    await withdraw_col.insert_one({
        "uid": uid, "amount": amt, "upi": upi,
        "status": "pending", "requested_at": datetime.now()
    })
    await update.message.reply_text(
        f"✅ *Withdrawal Request Bheja!*\n\n"
        f"💰 Amount: ₹{amt:.2f}\n"
        f"📲 UPI: {upi}\n\n"
        f"24-48 hours mein process hogi.",
        parse_mode="Markdown", reply_markup=kb_main()
    )
    await Bot(BOT_TOKEN).send_message(ADMIN_ID,
        f"💸 *Withdrawal Request*\n\n"
        f"👤 User: `{uid}`\n💰 ₹{amt:.2f}\n📲 `{upi}`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancel.", reply_markup=kb_main())
    return ConversationHandler.END

# ── Back to main (inline back buttons ke liye) ──────────
async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await get_user(q.from_user.id)
    await q.edit_message_text(
        f"💰 Balance: ₹{u['balance']:.2f}\n\nNiche se option choose karo 👇",
        parse_mode="Markdown"
    )

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
async def run_bot():
    os.makedirs("sessions", exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).build()

    # Add Account conversation
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

    # Withdrawal conversation
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

    # Reply keyboard triggers
    app.add_handler(MessageHandler(filters.Regex("^📋 TG Task$"), msg_tg_task))

    # Inline callbacks
    app.add_handler(CallbackQueryHandler(cb_check_join,  pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_back_main,   pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cb_task_done,   pattern="^task_done_"))
    app.add_handler(CallbackQueryHandler(cb_acc_accept,  pattern="^acc_accept_"))
    app.add_handler(CallbackQueryHandler(cb_acc_reject,  pattern="^acc_reject_"))

    # Admin text (site ID dene ke liye)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID),
        admin_text
    ))

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
