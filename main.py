import os
import logging
import asyncio
import tempfile
import http.cookiejar
import requests
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
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

# ─── ENV ───────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "6020799349"))
MONGO_URI       = os.getenv("MONGO_URI", "mongodb://hewopox808_db_user:TRtA39GIhW9i0amT@ac-uvsc6lm-shard-00-00.q1fms7k.mongodb.net:27017,ac-uvsc6lm-shard-00-01.q1fms7k.mongodb.net:27017,ac-uvsc6lm-shard-00-02.q1fms7k.mongodb.net:27017/?ssl=true&replicaSet=atlas-uslskk-shard-0&authSource=admin&appName=Cluster0")
CHANNEL_ID      = os.getenv("CHANNEL_ID")
API_ID          = int(os.getenv("API_ID", "39632094"))
API_HASH        = os.getenv("API_HASH", "858376a6592cc67856f47571f580b0d6")
SITE_URL        = os.getenv("SITE_URL", "https://77uai.com")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
COOKIE_FILE     = os.getenv("COOKIE_FILE", "cookies.txt")
TASK_REWARD     = 0.25
TASK_TIMEOUT    = 540  # 9 minutes

# ─── MongoDB ───────────────────────────────────────────
client_db    = AsyncIOMotorClient(MONGO_URI)
db           = client_db["tgbot"]
users_col    = db["users"]
accounts_col = db["tg_accounts"]
tasks_col    = db["tasks"]
withdraw_col = db["withdrawals"]

# ─── Conversation states ───────────────────────────────
ADD_PHONE, ADD_OTP, ADD_2FA, WITHDRAW_AMOUNT, WITHDRAW_UPI = range(5)

# ─── HTTP Server (Render ke liye) ──────────────────────
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running OK")
    def log_message(self, *a):
        pass

def start_http_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"[HTTP] Running on port {port}")

# ─── asyncio loop fix (Python 3.10+) ───────────────────
def get_or_create_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop

# ─── Cookie loader ─────────────────────────────────────
def get_cookie_jar():
    jar = http.cookiejar.MozillaCookieJar()
    if COOKIES_CONTENT:
        content = COOKIES_CONTENT.replace("\\n", "\n")
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write(content)
        tmp.close()
        try:
            jar = http.cookiejar.MozillaCookieJar(tmp.name)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning(f"Cookie env error: {e}")
    else:
        try:
            jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            logger.warning(f"Cookie file error: {e}")
    return jar

# ─── Site API ──────────────────────────────────────────
def site_call(method, endpoint, body=None):
    jar = get_cookie_jar()
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": SITE_URL, "Origin": SITE_URL,
    }
    r = requests.request(method, SITE_URL + endpoint, cookies=jar, headers=hdrs, json=body, timeout=15)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text}

# ─── DB helpers ────────────────────────────────────────
async def get_user(uid):
    u = await users_col.find_one({"uid": uid})
    if not u:
        u = {"uid": uid, "balance": 0.0, "joined": datetime.utcnow()}
        await users_col.insert_one(u)
    return u

async def add_balance(uid, amount):
    await users_col.update_one({"uid": uid}, {"$inc": {"balance": amount}}, upsert=True)

# ─── Force Join ────────────────────────────────────────
async def check_joined(user_id, bot):
    if not CHANNEL_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status not in ("left", "kicked")
    except Exception:
        return False

def force_join_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📢 Channel Join Karo", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"),
        InlineKeyboardButton("✅ Joined!", callback_data="check_join")
    ]])

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 TG Task",        callback_data="tg_task")],
        [InlineKeyboardButton("➕ Add TG Account", callback_data="add_account")],
        [InlineKeyboardButton("💸 Withdrawal",     callback_data="withdrawal")],
    ])

# ─── /start ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await get_user(uid)
    if not await check_joined(uid, ctx.bot):
        await update.message.reply_text(
            "🔒 *Bot use karne ke liye pehle channel join karo!*",
            parse_mode="Markdown", reply_markup=force_join_kb()
        )
        return
    u = await get_user(uid)
    await update.message.reply_text(
        f"👋 *Welcome!*\n\n💰 Balance: ₹{u['balance']:.2f}\n\nKya karna hai?",
        parse_mode="Markdown", reply_markup=main_menu_kb()
    )

async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if await check_joined(uid, ctx.bot):
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *Join confirmed!*\n\n💰 Balance: ₹{u['balance']:.2f}\n\nKya karna hai?",
            parse_mode="Markdown", reply_markup=main_menu_kb()
        )
    else:
        await q.answer("❌ Abhi join nahi kiya!", show_alert=True)

# ══════════════════════════════════════════════════════
#  TG TASK
# ══════════════════════════════════════════════════════
async def cb_tg_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    acc = await accounts_col.find_one({"uid": uid, "status": "accepted"})
    if not acc:
        await q.edit_message_text(
            "⚠️ *Koi accepted TG account nahi hai.*\n"
            "Pehle '➕ Add TG Account' se account add karo aur admin approval ka wait karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        )
        return

    existing = await tasks_col.find_one({"uid": uid, "status": "pending"})
    if existing:
        left = int((existing["expires_at"] - datetime.utcnow()).total_seconds())
        if left > 0:
            await _show_task(q, existing, left)
            return
        else:
            await tasks_col.update_one({"_id": existing["_id"]}, {"$set": {"status": "expired"}})

    await q.edit_message_text("⏳ Task le rahe hain...")
    account_id = acc.get("site_account_id")
    if not account_id:
        await q.edit_message_text("❌ Account ID configure nahi hai. Admin se contact karo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return

    loop = asyncio.get_event_loop()
    _, data = await loop.run_in_executor(None, site_call, "POST", "/api/tasks/claim",
                                          {"channel": "telegram", "account_id": account_id})
    if not data.get("ok"):
        _, data = await loop.run_in_executor(None, site_call, "GET", "/api/tasks/current?channel=telegram")

    task_data = data.get("data")
    if not task_data:
        await q.edit_message_text("❌ Abhi koi task available nahi hai. Baad mein try karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]))
        return

    expires_at = datetime.utcnow() + timedelta(seconds=TASK_TIMEOUT)
    task_doc = {
        "uid": uid, "account_id": account_id,
        "target_phone": task_data.get("target_phone", ""),
        "ad_copy": task_data.get("ad_copy", ""),
        "reward_points": task_data.get("reward_points", 0),
        "status": "pending",
        "created_at": datetime.utcnow(),
        "expires_at": expires_at,
    }
    res = await tasks_col.insert_one(task_doc)
    task_doc["_id"] = res.inserted_id
    await _show_task(q, task_doc, TASK_TIMEOUT)

async def _show_task(q, task, time_left):
    mins, secs = time_left // 60, time_left % 60
    text = (
        f"📋 *TASK*\n{'─'*30}\n"
        f"📱 *Number:* `+{task['target_phone']}`\n"
        f"⏳ *Time left:* {mins}m {secs}s\n"
        f"💰 *Reward:* ₹{TASK_REWARD:.2f}\n"
        f"{'─'*30}\n"
        f"📝 *Message bhejo:*\n\n`{task['ad_copy']}`\n\n"
        f"⬆️ Upar diya number pe yeh message Telegram se bhejo, phir *Done* dabao."
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Done! Check Karo", callback_data=f"task_done_{task['_id']}")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]))

async def cb_task_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Verify ho raha hai...")
    uid = q.from_user.id
    task_id_str = q.data.replace("task_done_", "")

    task = await tasks_col.find_one({"_id": ObjectId(task_id_str), "uid": uid})
    if not task:
        await q.edit_message_text("❌ Task nahi mila.")
        return
    if task["status"] != "pending":
        await q.edit_message_text(f"ℹ️ Task already {task['status']} hai.")
        return
    if datetime.utcnow() > task["expires_at"]:
        await tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "expired"}})
        await q.edit_message_text("⏰ Task expire ho gaya! Naya task lo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Naya Task", callback_data="tg_task")]]))
        return

    await q.edit_message_text("⏳ Check ho raha hai...")
    await asyncio.sleep(5)

    loop = asyncio.get_event_loop()
    _, result = await loop.run_in_executor(None, site_call, "POST", "/api/tasks/check", {"channel": "telegram"})
    success = result.get("ok") or result.get("data", {}).get("verified", False)

    if success:
        await tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "completed"}})
        await add_balance(uid, TASK_REWARD)
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *TASK COMPLETE!*\n\n💰 +₹{TASK_REWARD:.2f} mili!\n📊 New Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Agla Task", callback_data="tg_task")],
                [InlineKeyboardButton("🏠 Home", callback_data="back_main")]
            ])
        )
    else:
        await q.edit_message_text(
            f"❌ *Verify nahi hua!*\n\n• Message nahi bheja\n• Galat account se bheja\n• Delivery pending hai\n\nFir se try karo.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Fir Check Karo", callback_data=f"task_done_{task_id_str}")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ])
        )

# ══════════════════════════════════════════════════════
#  ADD ACCOUNT (Telethon)
# ══════════════════════════════════════════════════════
async def cb_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📱 *TG Account Add Karo*\n\nApna Indian mobile number bhejo:\nFormat: `+91XXXXXXXXXX`\n\n/cancel to cancel",
        parse_mode="Markdown"
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
        result = await tg.send_code_request(phone)
        ctx.user_data["phone_code_hash"] = result.phone_code_hash
        ctx.user_data["tg_client"] = tg
        await update.message.reply_text("✅ OTP gaya! Telegram app se OTP enter karo:\n\n/cancel to cancel")
        return ADD_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}\n\nFir se /start karo.")
        return ConversationHandler.END

async def got_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    otp = update.message.text.strip()
    uid = update.effective_user.id
    tg: TelegramClient = ctx.user_data.get("tg_client")
    try:
        await tg.sign_in(phone=ctx.user_data["phone"], code=otp, phone_code_hash=ctx.user_data["phone_code_hash"])
        me = await tg.get_me()
        await _save_account_request(uid, ctx.user_data["phone"], me, tg)
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 2FA password enter karo:")
        return ADD_2FA
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        await update.message.reply_text("❌ OTP galat/expire! /start se dobara try karo.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def got_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tg: TelegramClient = ctx.user_data.get("tg_client")
    try:
        await tg.sign_in(password=update.message.text.strip())
        me = await tg.get_me()
        await _save_account_request(uid, ctx.user_data["phone"], me, tg)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA Error: {e}")
        return ConversationHandler.END

async def _save_account_request(uid, phone, me, tg):
    name = f"{me.first_name or ''} {me.last_name or ''}".strip()
    await accounts_col.insert_one({
        "uid": uid, "phone": phone, "tg_id": me.id,
        "name": name, "username": me.username,
        "status": "pending", "site_account_id": None,
        "requested_at": datetime.utcnow(),
    })
    bot = Bot(BOT_TOKEN)
    await bot.send_message(uid,
        f"✅ *Account Request Bheja!*\n\n👤 Name: {name}\n📱 Phone: {phone}\n\nAdmin approval ka wait karo.",
        parse_mode="Markdown"
    )
    await bot.send_message(ADMIN_ID,
        f"🔔 *New Account Request*\n\n👤 Name: {name}\n📱 Phone: `{phone}`\n"
        f"🆔 TG ID: `{me.id}`\n👤 User ID: `{uid}`\n\nSite account ID dalo niche accept karte waqt.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"acc_accept_{uid}_{me.id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"acc_reject_{uid}_{me.id}"),
        ]])
    )
    await tg.disconnect()

# ─── Admin Accept / Reject ─────────────────────────────
async def cb_acc_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True)
        return
    parts = q.data.split("_")
    uid, tg_id = int(parts[2]), int(parts[3])
    await q.answer()
    await q.edit_message_text(q.message.text + "\n\n⏳ Site Account ID bhejo (sirf number):", parse_mode="Markdown")
    ctx.user_data["pending_uid"]    = uid
    ctx.user_data["pending_tg_id"] = tg_id
    ctx.user_data["awaiting_site_id"] = True

async def cb_acc_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True)
        return
    parts = q.data.split("_")
    uid, tg_id = int(parts[2]), int(parts[3])
    await q.answer()
    await accounts_col.update_one({"uid": uid, "tg_id": tg_id}, {"$set": {"status": "rejected"}})
    await q.edit_message_text(q.message.text + "\n\n❌ *REJECTED*", parse_mode="Markdown")
    await Bot(BOT_TOKEN).send_message(uid, "❌ Aapka TG account reject ho gaya. Dobara try karo ya admin se contact karo.")

async def admin_msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.user_data.get("awaiting_site_id"):
        return
    site_id_str = update.message.text.strip()
    if not site_id_str.isdigit():
        await update.message.reply_text("❌ Sirf number bhejo.")
        return
    site_id = int(site_id_str)
    uid    = ctx.user_data["pending_uid"]
    tg_id  = ctx.user_data["pending_tg_id"]
    await accounts_col.update_one(
        {"uid": uid, "tg_id": tg_id},
        {"$set": {"status": "accepted", "site_account_id": site_id, "accepted_at": datetime.utcnow()}}
    )
    ctx.user_data["awaiting_site_id"] = False
    await update.message.reply_text(f"✅ Account accepted! Site ID: {site_id}")
    await Bot(BOT_TOKEN).send_message(uid,
        "🎉 *Aapka TG Account Accept Ho Gaya!*\n\nAb aap TG Task kar sakte hain.",
        parse_mode="Markdown", reply_markup=main_menu_kb()
    )

# ══════════════════════════════════════════════════════
#  WITHDRAWAL
# ══════════════════════════════════════════════════════
async def cb_withdrawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await get_user(q.from_user.id)
    await q.edit_message_text(
        f"💸 *Withdrawal*\n\n💰 Available: ₹{u['balance']:.2f}\n\nKitna withdraw karna hai? (Min ₹10)\n/cancel to cancel",
        parse_mode="Markdown"
    )
    return WITHDRAW_AMOUNT

async def got_withdraw_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Valid amount likho (e.g. 50)")
        return WITHDRAW_AMOUNT
    if amount < 10:
        await update.message.reply_text("❌ Minimum ₹10 chahiye!")
        return WITHDRAW_AMOUNT
    if amount > u["balance"]:
        await update.message.reply_text(f"❌ Insufficient balance! Available: ₹{u['balance']:.2f}")
        return WITHDRAW_AMOUNT
    ctx.user_data["withdraw_amount"] = amount
    await update.message.reply_text(
        "📲 Apna UPI ID bhejo:\n(e.g. `name@upi`)\n\n/cancel to cancel",
        parse_mode="Markdown"
    )
    return WITHDRAW_UPI

async def got_withdraw_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    amount = ctx.user_data.get("withdraw_amount", 0)
    upi    = update.message.text.strip()
    await users_col.update_one({"uid": uid}, {"$inc": {"balance": -amount}})
    await withdraw_col.insert_one({"uid": uid, "amount": amount, "upi": upi, "status": "pending", "requested_at": datetime.utcnow()})
    await update.message.reply_text(
        f"✅ *Withdrawal Request Bheja!*\n\n💰 Amount: ₹{amount:.2f}\n📲 UPI: {upi}\n\nProcessing 24-48 hours mein hogi.",
        parse_mode="Markdown", reply_markup=main_menu_kb()
    )
    await Bot(BOT_TOKEN).send_message(ADMIN_ID,
        f"💸 *Withdrawal Request*\n\n👤 User: `{uid}`\n💰 Amount: ₹{amount:.2f}\n📲 UPI: `{upi}`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancel ho gaya.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    u = await get_user(q.from_user.id)
    await q.edit_message_text(
        f"🏠 *Main Menu*\n\n💰 Balance: ₹{u['balance']:.2f}",
        parse_mode="Markdown", reply_markup=main_menu_kb()
    )

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════
async def run_bot():
    os.makedirs("sessions", exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    add_acc_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_add_account, pattern="^add_account$")],
        states={
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_phone)],
            ADD_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_otp)],
            ADD_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, got_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True,
    )

    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_withdrawal, pattern="^withdrawal$")],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_withdraw_amount)],
            WITHDRAW_UPI:    [MessageHandler(filters.TEXT & ~filters.COMMAND, got_withdraw_upi)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True, per_chat=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(add_acc_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(CallbackQueryHandler(cb_check_join, pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_tg_task,    pattern="^tg_task$"))
    app.add_handler(CallbackQueryHandler(cb_back_main,  pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cb_task_done,  pattern="^task_done_"))
    app.add_handler(CallbackQueryHandler(cb_acc_accept, pattern="^acc_accept_"))
    app.add_handler(CallbackQueryHandler(cb_acc_reject, pattern="^acc_reject_"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID),
        admin_msg_handler
    ))

    logger.info("🤖 Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    # Bot chalata rahega jab tak process band na ho
    await asyncio.Event().wait()

if __name__ == "__main__":
    start_http_server()
    loop = get_or_create_loop()
    try:
        loop.run_until_complete(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
