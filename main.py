import os
import html
import json
import base64
import logging
import asyncio
import tempfile
import http.cookiejar
import urllib.parse
import requests
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
try:
    from telegram import CopyTextButton   # python-telegram-bot v21.7+
except ImportError:
    CopyTextButton = None
try:
    from telegram import LinkPreviewOptions   # PTB v20.8+
except ImportError:
    LinkPreviewOptions = None


def no_preview():
    """Link preview band (message lamba na ho)."""
    if LinkPreviewOptions:
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    return {"disable_web_page_preview": True}


def copy_button(label, text):
    """Copy button — purane python-telegram-bot me bhi chalta hai (api_kwargs fallback)."""
    if CopyTextButton:
        return InlineKeyboardButton(label, copy_text=CopyTextButton(text=text))
    return InlineKeyboardButton(label, api_kwargs={"copy_text": {"text": text}})

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
#  ENV CONFIG
# ══════════════════════════════════════════════════════
BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
MONGO_URI    = os.environ["MONGO_URI"]
API_ID       = int(os.getenv("API_ID", "26454514"))
API_HASH     = os.getenv("API_HASH", "40b3a76db24fde1367a303c4fa05a72c")
CHANNEL_ID   = os.getenv("CHANNEL_ID", "")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
SITE_URL     = os.getenv("SITE_URL", "https://77uai.com")
COOKIE_FILE  = os.getenv("COOKIE_FILE", "cookies.txt")
PUBLIC_URL   = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://tg-tasks.onrender.com"

TASK_REWARD     = 0.25
WS_TASK_REWARD  = 0.25
WA_LINK_LF      = os.getenv("WA_LINK_LF", "1") == "1"
MIN_WITHDRAW    = 5
WITHDRAW_TAX    = 2
TASK_TIMEOUT    = 540
WS_TASK_TIMEOUT = 600

IST = ZoneInfo("Asia/Kolkata")
TASK_START_HOUR = 8
TASK_END_HOUR   = 22

# ══════════════════════════════════════════════════════
#  MONGODB
# ══════════════════════════════════════════════════════
_db          = AsyncIOMotorClient(MONGO_URI)["tgbot"]
users_col    = _db["users"]
accounts_col = _db["tg_accounts"]
ws_accounts_col = _db["ws_accounts"]
tasks_col    = _db["tasks"]
ws_tasks_col = _db["ws_tasks"]
withdraw_col = _db["withdrawals"]
slots_col    = _db["cookie_slots"]

# ══════════════════════════════════════════════════════
#  CONVERSATION STATES
# ══════════════════════════════════════════════════════
ADD_PHONE, ADD_OTP, ADD_2FA, W_AMOUNT, W_UPI = range(5)

# ══════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════
def _b64e(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")

def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")

def copy_page_url(text, target):
    payload = json.dumps({"m": text, "t": target}, ensure_ascii=False)
    url = f"{PUBLIC_URL.rstrip('/')}/m?d={_b64e(payload)}"
    return url if len(url) <= 2000 else ""

_COPY_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WhatsApp Task</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f5f5f5;color:#111;min-height:100vh}

.topbar{background:#c0392b;height:5px;width:100%}

.wrap{max-width:540px;margin:0 auto;padding:20px 16px 40px}

.title{font-size:22px;font-weight:700;color:#c0392b;margin:16px 0 20px}

.label{font-size:11px;font-weight:800;letter-spacing:.1em;color:#555;
       margin:20px 0 8px;text-transform:uppercase}

.numbox{background:#fff;border:1.5px solid #e0e0e0;border-radius:10px;
        padding:14px 16px;font-size:19px;font-weight:700;color:#111;
        display:flex;align-items:center;justify-content:space-between}
.numbox .icon{color:#c0392b;font-size:20px;cursor:pointer;padding:4px 8px}

.msgbox{background:#fff;border:1.5px solid #e0e0e0;border-radius:10px;
        padding:16px;font-size:16px;line-height:1.6;
        white-space:pre-wrap;word-wrap:break-word;word-break:break-word;
        color:#111;position:relative;min-height:80px;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
        margin:0;}
.msgbox .icon{position:absolute;top:12px;right:12px;color:#c0392b;
              font-size:20px;cursor:pointer}

.btn{display:block;width:100%;border:0;border-radius:10px;padding:15px;
     font-size:16px;font-weight:700;text-align:center;text-decoration:none;
     margin-top:14px;cursor:pointer;transition:opacity .15s}
.btn:active{opacity:.8}
.btn-copy{background:#c0392b;color:#fff}
.btn-wa{background:#25d366;color:#fff}
.btn-num{background:#fff;color:#111;border:1.5px solid #ddd;margin-top:10px}

.toast{display:none;position:fixed;bottom:30px;left:50%;transform:translateX(-50%);
       background:#333;color:#fff;padding:10px 24px;border-radius:24px;
       font-size:15px;font-weight:600;z-index:999}
.toast.show{display:block}
</style></head>
<body>
<div class="topbar"></div>
<div class="wrap">
  <div class="title">WhatsApp tasks</div>

  __NUMBLOCK__

  <div class="label">Message to send</div>
  <pre class="msgbox" id="msg">__MSG__<span class="icon" onclick="copyMsg()" title="Copy">&#x2947;</span></pre>

  <button class="btn btn-copy" onclick="copyMsg()">&#x1F4CB; Copy Message</button>
  __OPENBTN__
  __NUMCOPYBTN__
</div>

<div class="toast" id="toast">&#x2705; Copied!</div>

<script>
function copyMsg(){
  var el = document.getElementById('msg');
  var iconSpan = el.querySelector('.icon');
  var iconText = iconSpan ? (iconSpan.innerText || iconSpan.textContent) : '';
  var fullText = el.innerText || el.textContent;
  var t = fullText;
  if(iconText && t.endsWith(iconText)){
    t = t.slice(0, t.length - iconText.length);
  }
  t = t.replace(/^[\r\n\s]+|[\r\n\s]+$/g, '');
  doCopy(t);
}
function copyEl(id){
  var el = document.getElementById(id);
  var iconSpan = el.querySelector('.icon');
  var iconText = iconSpan ? (iconSpan.innerText || iconSpan.textContent) : '';
  var t = el.innerText || el.textContent;
  if(iconText && t.endsWith(iconText)){
    t = t.slice(0, t.length - iconText.length);
  }
  t = t.replace(/^[\r\n\s]+|[\r\n\s]+$/g, '');
  doCopy(t);
}
function doCopy(t){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){showToast();}).catch(function(){fallback(t);});
  } else { fallback(t); }
}
function fallback(t){
  var ta=document.createElement('textarea');
  ta.value=t;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.focus();ta.select();
  try{document.execCommand('copy');}catch(e){}
  document.body.removeChild(ta);
  showToast();
}
function showToast(){
  var d=document.getElementById('toast');
  d.classList.add('show');
  setTimeout(function(){d.classList.remove('show');},1600);
}
</script>
</body></html>"""


def render_copy_page(text, target):
    num_block = ""
    open_btn = ""
    num_copy_btn = ""
    if target:
        num_block = (
            '<div class="label">Send to this WhatsApp number</div>'
            f'<div class="numbox" id="num">'
            f'<span>+{html.escape(target)}</span>'
            f'<span class="icon" onclick="copyEl(\'num\')" title="Copy">&#x2947;</span>'
            f'</div>'
        )
        wa = f"https://wa.me/{target}?text={urllib.parse.quote(text, safe='')}"
        open_btn = f'<a class="btn btn-wa" href="{html.escape(wa)}">&#x1F4F2; Open in WhatsApp</a>'
        num_copy_btn = '<button class="btn btn-num" onclick="copyEl(\'num\')">&#x1F4DE; Copy Number</button>'

    escaped_text = html.escape(text)

    return (_COPY_PAGE
            .replace("__NUMBLOCK__", num_block)
            .replace("__OPENBTN__", open_btn)
            .replace("__NUMCOPYBTN__", num_copy_btn)
            .replace("__MSG__", escaped_text))


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/m":
            try:
                raw = urllib.parse.parse_qs(parsed.query).get("d", [""])[0]
                data = json.loads(_b64d(raw))
                msg_text = str(data.get("m", ""))
                body = render_copy_page(msg_text, str(data.get("t", ""))).encode("utf-8")
            except Exception:
                self.send_response(400); self.end_headers()
                self.wfile.write(b"bad link"); return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
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
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            "Accept": "application/json, */*",
            "Content-Type": "application/json",
            "Referer": SITE_URL + "/tasks/whatsapp",
            "Origin": SITE_URL,
            "X-Upliora-Client-Version": "20261104-task-read-retry-v1",
            "X-Upliora-Telegram": "v2",
            "Verification": "",
        }, json=body, timeout=15)
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, {"raw": r.text}

# ══════════════════════════════════════════════════════
#  SLOT LOCK SYSTEM
# ══════════════════════════════════════════════════════
EPOCH = datetime(2000, 1, 1)
CHANNELS = ["tg", "wa", "wa_pair"]


async def init_slots():
    for i in range(len(JARS)):
        for ch in CHANNELS:
            await slots_col.update_one(
                {"idx": i, "channel": ch},
                {"$setOnInsert": {
                    "idx": i, "channel": ch,
                    "busy_until": EPOCH, "uid": None
                }},
                upsert=True)


async def take_slot(idx, channel, uid, timeout_secs):
    now = datetime.now()
    return await slots_col.find_one_and_update(
        {"idx": idx, "channel": channel, "busy_until": {"$lte": now}},
        {"$set": {
            "busy_until": now + timedelta(seconds=timeout_secs),
            "uid": uid
        }},
        return_document=ReturnDocument.AFTER)


async def free_slot(idx, channel):
    await slots_col.update_one(
        {"idx": idx, "channel": channel},
        {"$set": {"busy_until": EPOCH, "uid": None}})


async def slot_busy_until(idx, channel):
    s = await slots_col.find_one({"idx": idx, "channel": channel})
    if not s or s["busy_until"] <= datetime.now():
        return 0
    return max(1, int((s["busy_until"] - datetime.now()).total_seconds() // 60) + 1)


async def is_slot_free(idx, channel):
    s = await slots_col.find_one({"idx": idx, "channel": channel})
    if not s:
        return True
    return s["busy_until"] <= datetime.now()


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
        ["📋 TG Task", "💬 WA Task"],
        ["➕ Add TG Account", "➕ Add WA Account"],
        ["💸 Withdrawal"],
    ], resize_keyboard=True)


_link_cache = {"url": None}


async def channel_link(bot):
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
        f"📋 TG Task → Telegram number pe message bhejo\n"
        f"💬 WA Task → WhatsApp number pe message bhejo\n\n"
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
            "⚠ *Koi accepted TG account nahi hai.*\n\n"
            "➕ Add TG Account se pehle account add karo, admin approve karega.",
            parse_mode="Markdown")
        return

    existing = await tasks_col.find_one({"uid": uid, "status": "pending"})
    if existing:
        left = int((existing["expires_at"] - datetime.now()).total_seconds())
        if left > 0:
            await _send_tg_task(update.message, existing, left)
            return
        await tasks_col.update_one({"_id": existing["_id"]}, {"$set": {"status": "expired"}})
        await free_slot(existing.get("cookie_idx", 0), "tg")

    if not task_window_open():
        await update.message.reply_text(
            f"⏰ *Abhi task available nahi hai.*\n\n"
            f"Task roz *subah 8 baje se raat 10 baje* tak milte hain.\n"
            f"Agla task {next_open_text()} milega.",
            parse_mode="Markdown")
        return

    if len(accs) == 1:
        await start_tg_task(uid, accs[0], update.message)
        return

    kb = [[InlineKeyboardButton(acc_label(a), callback_data=f"pick_acc_{a['_id']}")] for a in accs]
    await update.message.reply_text(
        "📲 *Kis TG account se task lena hai?*",
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
        await q.edit_message_text("⚠ Aapka ek TG task already chal raha hai. Pehle usse complete karo.")
        return
    if not task_window_open():
        await q.edit_message_text("⏰ Task ka time (8 AM - 10 PM) nahi hai."); return
    await q.edit_message_text(f"✅ Selected: {acc_label(acc)}")
    await start_tg_task(uid, acc, q.message)


async def start_tg_task(uid, acc, msg):
    idx = acc.get("cookie_idx", 0)
    if idx >= len(JARS):
        await msg.reply_text("❌ Is account ki cookie configured nahi hai. Admin se contact karo.")
        return

    slot = await take_slot(idx, "tg", uid, TASK_TIMEOUT)
    if not slot:
        mins = await slot_busy_until(idx, "tg")
        await msg.reply_text(
            f"⏳ TG task slot abhi busy hai.\n"
            f"Lagbhag *{mins} minute* baad try karo.", parse_mode="Markdown")
        return

    wait_msg = await msg.reply_text("⏳ TG Task le rahe hain...")
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
            await free_slot(idx, "tg")
            await msg.reply_text("❌ Abhi koi TG task nahi hai. Thodi der baad try karo.")
            return
        _, me_data = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
        bal_at_claim = me_data.get("data", {}).get("points_balance", 0)
        logger.info(f"[TG TASK] uid={uid} cookie={idx} bal_at_claim={bal_at_claim}")
    except Exception as e:
        logger.error(f"[TG TASK] {e}")
        await free_slot(idx, "tg")
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
    await _send_tg_task(msg, doc, TASK_TIMEOUT)


async def _send_tg_task(msg, task, time_left):
    mins, secs = time_left // 60, time_left % 60
    ad_copy = clean_ad_copy(task.get("ad_copy", ""))

    text = (
        f"📋 <b>TELEGRAM TASK</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 <b>Account:</b> <code>{html.escape(str(task.get('acc_phone', '')))}</code>\n"
        f"📱 <b>Number:</b> <code>+{html.escape(str(task.get('target_phone', '')))}</code>\n"
        f"⏳ <b>Time:</b> {mins}m {secs}s\n"
        f"💰 <b>Reward:</b> ₹{TASK_REWARD:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Yeh message bhejo:</b>\n\n"
        f"<pre>{html.escape(ad_copy)}</pre>\n"
        f"👆 Copy button dabao, Telegram number pe bhejo, phir Done dabao."
    )

    buttons = []
    if ad_copy and len(ad_copy) <= 256:
        buttons.append([copy_button("📋 Copy Message", ad_copy)])
    buttons.append([InlineKeyboardButton(
        "✅ Done — Check Karo", callback_data=f"task_done_{task['_id']}")])

    await msg.reply_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


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
        await free_slot(idx, "tg")
        await q.edit_message_text("⏰ *Task expire ho gaya!*\n\nNaya task lo.", parse_mode="Markdown")
        return

    await q.edit_message_text("⏳ Checking... (5 sec)")
    loop = asyncio.get_event_loop()

    chk_code, chk_resp = await loop.run_in_executor(
        None, site, "POST", "/api/tasks/check", {"channel": "telegram"}, idx)
    logger.info(f"[TG CHECK-CALL] uid={uid} http={chk_code} resp={chk_resp}")
    await asyncio.sleep(5)

    _, me_now = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
    bal_now = me_now.get("data", {}).get("points_balance", 0)
    bal_at_claim = task.get("bal_at_claim", 0)
    logger.info(f"[TG CHECK] uid={uid} cookie={idx} bal_at_claim={bal_at_claim} bal_now={bal_now}")

    if bal_now > bal_at_claim:
        new_expires = datetime.now() + timedelta(seconds=TASK_TIMEOUT)
        done = await tasks_col.find_one_and_update(
            {"_id": task["_id"], "status": "pending"},
            {"$set": {"status": "completed", "completed_at": datetime.now(),
                      "expires_at": new_expires}})
        await free_slot(idx, "tg")
        if not done:
            await q.edit_message_text("ℹ Task already process ho chuka hai."); return
        await add_bal(uid, TASK_REWARD)
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *TG TASK COMPLETE!*\n\n"
            f"💰 +₹{TASK_REWARD:.2f} mili!\n"
            f"📊 Total Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown")
    else:
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
#  WHATSAPP TASK
# ══════════════════════════════════════════════════════

def ws_acc_label(a):
    return f"📱 {a.get('phone', 'WA Account')} | ID {a.get('site_account_id')}"


async def msg_ws_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await joined(uid, ctx.bot):
        await update.message.reply_text("🔒 Pehle channel join karo!", reply_markup=await kb_join(ctx.bot))
        return

    existing = await ws_tasks_col.find_one({"uid": uid, "status": "pending"})
    if existing:
        left = int((existing["expires_at"] - datetime.now()).total_seconds())
        if left > 0:
            await _send_ws_task(update.message, existing, left)
            return
        await ws_tasks_col.update_one({"_id": existing["_id"]}, {"$set": {"status": "expired"}})
        await free_slot(existing.get("cookie_idx", 0), "wa")

    if not task_window_open():
        await update.message.reply_text(
            f"⏰ *Abhi WA task available nahi hai.*\n\n"
            f"Task roz *subah 8 baje se raat 10 baje* tak milte hain.\n"
            f"Agla task {next_open_text()} milega.",
            parse_mode="Markdown")
        return

    ws_accs = await ws_accounts_col.find({"uid": uid, "status": "accepted"}).to_list(50)
    if not ws_accs:
        await update.message.reply_text(
            "⚠ *Koi WhatsApp account linked nahi hai.*\n\n"
            "Admin se WhatsApp account add karwao.\n"
            "Admin ko contact karo aur apna WhatsApp number do.",
            parse_mode="Markdown")
        return

    if len(ws_accs) == 1:
        await start_ws_task(uid, ws_accs[0], update.message)
        return

    kb = [[InlineKeyboardButton(ws_acc_label(a), callback_data=f"pick_ws_{a['_id']}")] for a in ws_accs]
    await update.message.reply_text(
        "📲 *Kis WhatsApp account se task lena hai?*",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def cb_pick_ws(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    acc = await ws_accounts_col.find_one(
        {"_id": ObjectId(q.data.replace("pick_ws_", "")), "uid": uid, "status": "accepted"})
    if not acc:
        await q.edit_message_text("❌ WA Account nahi mila."); return
    if await ws_tasks_col.find_one({"uid": uid, "status": "pending", "expires_at": {"$gt": datetime.now()}}):
        await q.edit_message_text("⚠ Ek WA task already chal raha hai. Pehle usse complete karo.")
        return
    if not task_window_open():
        await q.edit_message_text("⏰ Task ka time (8 AM - 10 PM) nahi hai."); return
    await q.edit_message_text(f"✅ Selected: {ws_acc_label(acc)}")
    await start_ws_task(uid, acc, q.message)


async def start_ws_task(uid, acc, msg):
    idx = acc.get("cookie_idx", 0)
    site_account_id = acc.get("site_account_id")

    if idx >= len(JARS):
        await msg.reply_text("❌ Is WA account ki cookie configured nahi hai. Admin se contact karo.")
        return

    if not await is_slot_free(idx, "wa_pair"):
        mins = await slot_busy_until(idx, "wa_pair")
        await msg.reply_text(
            f"⏳ Is cookie pe WA account add ho raha hai.\n"
            f"Lagbhag *{mins} minute* baad try karo (pairing complete hone do).",
            parse_mode="Markdown")
        return

    slot = await take_slot(idx, "wa", uid, WS_TASK_TIMEOUT)
    if not slot:
        mins = await slot_busy_until(idx, "wa")
        await msg.reply_text(
            f"⏳ WA task slot abhi busy hai.\n"
            f"Lagbhag *{mins} minute* baad try karo.", parse_mode="Markdown")
        return

    wait_msg = await msg.reply_text("⏳ WA Task le rahe hain...")
    loop = asyncio.get_event_loop()

    try:
        _, status_data = await loop.run_in_executor(
            None, site, "GET", f"/api/ws/status?account_id={site_account_id}", None, idx)

        status_info = status_data.get("data", {}).get("status", {})
        phase = status_info.get("phase", "")
        is_authorized = status_data.get("data", {}).get("binding", {}).get("authorized", False)

        if not is_authorized:
            await wait_msg.delete()
            await free_slot(idx, "wa")
            await msg.reply_text(
                "❌ *WhatsApp account authorized nahi hai.*\n\n"
                "Account ko site pe reconnect karo ya admin se contact karo.",
                parse_mode="Markdown")
            return

        if phase not in ("online", "sleeping"):
            await wait_msg.delete()
            await free_slot(idx, "wa")
            await msg.reply_text(
                f"⚠ *Account ka phase: `{phase}`*\n\n"
                "Account abhi ready nahi hai. Thodi der baad try karo.",
                parse_mode="Markdown")
            return

        _, claim_resp = await loop.run_in_executor(
            None, site, "POST", "/api/tasks/claim",
            {"channel": "whatsapp", "account_id": site_account_id}, idx)

        logger.info(f"[WS CLAIM] uid={uid} account_id={site_account_id} resp={claim_resp}")

        if claim_resp.get("ok") and claim_resp.get("data"):
            task_resp = claim_resp
        else:
            _, task_resp = await loop.run_in_executor(
                None, site, "GET",
                f"/api/tasks/current?channel=whatsapp&account_id={site_account_id}",
                None, idx)

        await wait_msg.delete()
        task_data = task_resp.get("data")

        if not task_data:
            await free_slot(idx, "wa")
            await msg.reply_text("❌ Abhi koi WA task nahi hai. Thodi der baad try karo.")
            return

        binding = status_data.get("data", {}).get("binding", {})
        reward_points_at_claim = binding.get("reward_points", 0)
        _, me_data = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
        bal_at_claim = me_data.get("data", {}).get("points_balance", 0)

        logger.info(f"[WS TASK] uid={uid} cookie={idx} account_id={site_account_id} "
                    f"reward_points_at_claim={reward_points_at_claim} phase={phase}")

        site_expires = task_data.get("expires_at")
        if site_expires:
            try:
                expires_at = datetime.fromisoformat(site_expires.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                expires_at = datetime.now() + timedelta(seconds=WS_TASK_TIMEOUT)
        else:
            expires_at = datetime.now() + timedelta(seconds=WS_TASK_TIMEOUT)

        doc = {
            "uid": uid,
            "site_account_id": site_account_id,
            "cookie_idx": idx,
            "ws_phone": acc.get("phone"),
            "task_id": task_data.get("id"),
            "target_phone": task_data.get("target_phone", ""),
            "target_username": task_data.get("target_username", ""),
            "ad_copy": task_data.get("ad_copy", ""),
            "reward_min_points": task_data.get("reward_min_points", 100),
            "reward_max_points": task_data.get("reward_max_points", 150),
            "reward_points_at_claim": reward_points_at_claim,
            "bal_at_claim": bal_at_claim,
            "status": "pending",
            "created_at": datetime.now(),
            "expires_at": expires_at,
        }
        res = await ws_tasks_col.insert_one(doc)
        doc["_id"] = res.inserted_id

        time_left = max(0, int((expires_at - datetime.now()).total_seconds()))
        await _send_ws_task(msg, doc, time_left)

    except Exception as e:
        logger.error(f"[WS TASK] {e}")
        try: await wait_msg.delete()
        except Exception: pass
        await free_slot(idx, "wa")
        await msg.reply_text("❌ Error aaya, dobara try karo.")


# ══════════════════════════════════════════════════════
#  AD COPY HELPERS — FIXED
# ══════════════════════════════════════════════════════
def clean_ad_copy(text):
    """Display ke liye — paragraph gaps preserve, sirf line ending fix."""
    if not text:
        return ""
    text = text.replace("\r\n\r\n", "\n\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def raw_ad_copy(text):
    """WA pe bhejne ke liye — paragraph gaps preserve, sirf line ending fix."""
    if not text:
        return ""
    text = text.replace("\r\n\r\n", "\n\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


async def _send_ws_task(msg, task, time_left):
    mins, secs = time_left // 60, time_left % 60
    target = task.get("target_phone", "")
    username = task.get("target_username", "")
    pts_min = task.get("reward_min_points", 150)
    pts_max = task.get("reward_max_points", 250)

    raw_msg = raw_ad_copy(task.get("ad_copy", ""))
    show_msg = raw_msg

    wa_link = (
        f"https://wa.me/{target}?text={urllib.parse.quote(raw_msg, safe='')}"
        if target else ""
    )

    text = (
        f"💬 <b>WhatsApp tasks</b>\n\n"
        f"⏳ <b>YOUR CURRENT TASK</b>  {mins:02d}:{secs:02d}   (+{pts_min}–{pts_max} pts)\n"
        f"💰 Reward: ₹{WS_TASK_REWARD:.2f}\n\n"
        f"<b>SEND TO THIS WHATSAPP NUMBER</b>\n"
        f"<code>+{html.escape(str(target))}</code>\n"
    )
    if username:
        text += f"👤 @{html.escape(str(username))}\n"
    text += (
        f"\n<b>MESSAGE TO SEND</b>\n"
        f"<blockquote>{html.escape(show_msg)}</blockquote>\n"
        f"📱 Isi WA account se bhejo: <code>{html.escape(str(task.get('ws_phone', '')))}</code>"
    )

    buttons = []
    if wa_link:
        buttons.append([InlineKeyboardButton("📲 Open WhatsApp — Send Karo", url=wa_link)])
    page_url = copy_page_url(show_msg, target) if raw_msg else ""
    if page_url:
        buttons.append([InlineKeyboardButton("🌐 Web Page — Message Copy Karo", url=page_url)])
    copy_row = []
    if raw_msg and len(show_msg) <= 256:
        copy_row.append(copy_button("📋 Copy Message", show_msg))
    if target:
        copy_row.append(copy_button("📞 Copy Number", f"+{target}"))
    if copy_row:
        buttons.append(copy_row)
    buttons.append([InlineKeyboardButton("✅ Done — Check Karo", callback_data=f"ws_done_{task['_id']}")])

    await msg.reply_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons), **no_preview())


async def cb_ws_task_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Check ho raha hai...")
    uid = q.from_user.id
    tid = q.data.replace("ws_done_", "")

    task = await ws_tasks_col.find_one({"_id": ObjectId(tid), "uid": uid})
    if not task:
        await q.edit_message_text("❌ Task nahi mila."); return
    if task["status"] != "pending":
        await q.edit_message_text(f"ℹ Task {task['status']} hai."); return

    idx = task.get("cookie_idx", 0)

    if datetime.now() > task["expires_at"]:
        await ws_tasks_col.update_one({"_id": task["_id"]}, {"$set": {"status": "expired"}})
        await free_slot(idx, "wa")
        await q.edit_message_text("⏰ *Task expire ho gaya!*\n\nNaya WA task lo.", parse_mode="Markdown")
        return

    await q.edit_message_text("⏳ Checking... (kuch seconds lagenge, wait karo)")
    loop = asyncio.get_event_loop()
    site_account_id = task.get("site_account_id")
    task_site_id = task.get("task_id")

    verified = False
    points_earned = 0
    debug_log = []
    MAX_ROUNDS = 4
    bal_at_claim = task.get("bal_at_claim")

    for rnd in range(1, MAX_ROUNDS + 1):
        await asyncio.sleep(6)

        _, me_now = await loop.run_in_executor(None, site, "GET", "/api/me", None, idx)
        bal_now = me_now.get("data", {}).get("points_balance", 0)
        logger.info(f"[WS CHECK-BAL] uid={uid} round={rnd} bal_at_claim={bal_at_claim} bal_now={bal_now}")
        debug_log.append(f"R{rnd} bal_at_claim={bal_at_claim} bal_now={bal_now}")
        if bal_at_claim is not None and bal_now > bal_at_claim:
            verified = True
            points_earned = bal_now - bal_at_claim
            break

        _, task_status = await loop.run_in_executor(
            None, site, "GET",
            f"/api/tasks/current?channel=whatsapp&account_id={site_account_id}", None, idx)
        current_task = task_status.get("data") or {}
        current_status = current_task.get("status", "") if isinstance(current_task, dict) else ""
        logger.info(f"[WS CHECK-CUR] round={rnd} status={current_status} resp={task_status}")
        debug_log.append(f"R{rnd} current status={current_status!r} resp={str(task_status)[:300]}")
        if current_status in ("completed", "done", "success", "verified"):
            verified = True
            points_earned = (current_task.get("total_reward_points", 0)
                             or current_task.get("reward_points", 0))
            break

    if verified:
        new_expires = datetime.now() + timedelta(seconds=WS_TASK_TIMEOUT)
        done = await ws_tasks_col.find_one_and_update(
            {"_id": task["_id"], "status": "pending"},
            {"$set": {"status": "completed", "completed_at": datetime.now(),
                      "expires_at": new_expires,
                      "reward_points_earned": points_earned}})
        await free_slot(idx, "wa")
        if not done:
            await q.edit_message_text("ℹ Task already process ho chuka hai."); return
        await add_bal(uid, WS_TASK_REWARD)
        u = await get_user(uid)
        await q.edit_message_text(
            f"✅ *WA TASK COMPLETE!*\n\n"
            f"💰 +₹{WS_TASK_REWARD:.2f} mili!\n"
            f"🎯 Points earned: {points_earned}\n"
            f"📊 Total Balance: ₹{u['balance']:.2f}",
            parse_mode="Markdown")
    else:
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔧 WA VERIFY FAIL\nUser: {uid}\nTask: {task_site_id}\n"
                f"Account: {site_account_id}\n\n" + "\n".join(debug_log)[:3400])
        except Exception:
            pass
        await q.edit_message_text(
            "❌ *Verify nahi hua*\n\n"
            "• WhatsApp pe message bheja?\n"
            "• Sahi number pe bheja?\n"
            "• Sahi WA account se bheja?\n\n"
            "Bhejo phir dobara check karo.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Dobara Check Karo", callback_data=f"ws_done_{tid}")]
            ]))

# ══════════════════════════════════════════════════════
#  WA ACCOUNT ADD — USER FLOW (pairing code)
# ══════════════════════════════════════════════════════
ADD_WA_PHONE = 10


async def msg_add_wa_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await joined(uid, ctx.bot):
        await update.message.reply_text("🔒 Pehle channel join karo!", reply_markup=await kb_join(ctx.bot))
        return ConversationHandler.END

    pending = await ws_accounts_col.find_one({"uid": uid, "status": "pairing"})
    if pending:
        exp = pending.get("pair_expires_at")
        if exp and datetime.now() < exp:
            left = int((exp - datetime.now()).total_seconds())
            code = pending.get("pairing_code", "????")
            await update.message.reply_text(
                f"⏳ *Pehle se ek pairing chal rahi hai!*\n\n"
                f"📱 Number: `{pending.get('phone')}`\n"
                f"🔑 Code: `{code}`\n"
                f"⏰ {left} sec baaki\n\n"
                f"WA app mein enter karo ya /cancel karo.",
                parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            return ConversationHandler.END
        await ws_accounts_col.update_one({"_id": pending["_id"]}, {"$set": {"status": "expired"}})

    await update.message.reply_text(
        "📱 *WhatsApp Account Add Karo*\n\n"
        "Apna Indian WhatsApp number bhejo:\n"
        "Format: `+91XXXXXXXXXX`\n\n"
        "_(Yeh number site pe linked hoga)_\n\n"
        "/cancel to cancel",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return ADD_WA_PHONE


async def got_wa_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()

    clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not clean.isdigit() or len(clean) < 10:
        await update.message.reply_text("❌ Valid phone number do (e.g. `+919876543210`)", parse_mode="Markdown")
        return ADD_WA_PHONE

    dup = await ws_accounts_col.find_one({"phone": phone, "status": "accepted"})
    if dup:
        loop = asyncio.get_event_loop()
        dup_idx = dup.get("cookie_idx", 0)
        dup_site_id = dup.get("site_account_id")
        is_active = False
        try:
            if dup_site_id and dup_idx < len(JARS):
                _, st = await loop.run_in_executor(
                    None, site, "GET",
                    f"/api/ws/status?account_id={dup_site_id}", None, dup_idx)
                binding = st.get("data", {}).get("binding", {})
                is_active = binding.get("authorized", False) and binding.get("phase") in ("online", "sleeping")
        except Exception:
            pass

        if is_active:
            await update.message.reply_text(
                "⚠ *Yeh number pehle se active hai.*\n\n"
                "WA Task kar sakte ho — 💬 WA Task button dabao.",
                parse_mode="Markdown", reply_markup=kb_main())
            return ConversationHandler.END
        else:
            await ws_accounts_col.update_one(
                {"_id": dup["_id"]}, {"$set": {"status": "removed"}})
            logger.info(f"[WA PAIR] uid={uid} phone={phone} — old disconnected account removed, re-pairing")

    wait_msg = await update.message.reply_text("⏳ Pairing code generate ho raha hai...")

    pair_idx = int(os.getenv("WA_PAIR_COOKIE_IDX", "0"))
    if pair_idx >= len(JARS):
        await wait_msg.delete()
        await update.message.reply_text(
            "❌ Pairing cookie configured nahi hai. Admin se contact karo.",
            reply_markup=kb_main())
        return ConversationHandler.END

    pair_slot = await take_slot(pair_idx, "wa_pair", uid, 200)
    if not pair_slot:
        await wait_msg.delete()
        mins = await slot_busy_until(pair_idx, "wa_pair")
        await update.message.reply_text(
            f"⏳ Abhi kisi aur ka WA account add ho raha hai.\n"
            f"*{mins} minute* baad try karo.",
            parse_mode="Markdown", reply_markup=kb_main())
        return ConversationHandler.END

    loop = asyncio.get_event_loop()
    try:
        _, pair_resp = await loop.run_in_executor(
            None, site, "POST", "/api/ws/pair",
            {"phone": clean}, pair_idx)

        logger.info(f"[WA PAIR] uid={uid} phone={phone} resp={pair_resp}")

        pair_data = pair_resp.get("data", {})
        pairing_code = pair_data.get("pairing_code", "")
        expires_in   = pair_data.get("expires_in_seconds", 180)
        attempt_id   = pair_data.get("attempt_id", "")
        binding      = pair_data.get("binding", {})
        site_acc_id  = binding.get("id")

        if not pairing_code:
            await free_slot(pair_idx, "wa_pair")
            await wait_msg.delete()
            err_msg = pair_resp.get("message") or pair_resp.get("error") or "Unknown error"
            await update.message.reply_text(
                f"❌ Pairing code nahi mila.\n\nError: `{err_msg}`\n\nAdmin se contact karo.",
                parse_mode="Markdown", reply_markup=kb_main())
            return ConversationHandler.END

        pair_expires_at = datetime.now() + timedelta(seconds=expires_in)
        doc = {
            "uid": uid,
            "phone": phone,
            "clean_phone": clean,
            "site_account_id": site_acc_id,
            "pairing_code": pairing_code,
            "attempt_id": attempt_id,
            "pair_expires_at": pair_expires_at,
            "cookie_idx": pair_idx,
            "status": "pairing",
            "created_at": datetime.now(),
        }
        result = await ws_accounts_col.insert_one(doc)
        doc_id = result.inserted_id

        await wait_msg.delete()

        mins = expires_in // 60
        secs = expires_in % 60
        await update.message.reply_text(
            f"✅ *Pairing Code Ready!*\n\n"
            f"📱 Number: `{phone}`\n"
            f"🔑 *Code: `{pairing_code}`*\n"
            f"⏰ Valid: {mins}m {secs}s\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"*WhatsApp pe yeh karo:*\n\n"
            f"1⃣ WhatsApp app kholo\n"
            f"2⃣ 3 dots (⋮) → Linked Devices\n"
            f"3⃣ *Link with Phone Number* dabao\n"
            f"4⃣ Upar diya hua code enter karo\n\n"
            f"⚠ *{mins} minute ke andar enter karo!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Code Enter Kar Diya", callback_data=f"wa_pair_check_{doc_id}")
            ]]))

        await Bot(BOT_TOKEN).send_message(
            ADMIN_ID,
            f"🔔 *New WA Pairing Started*\n\n"
            f"👤 User: `{uid}`\n"
            f"📱 Phone: `{phone}`\n"
            f"🔑 Code: `{pairing_code}`\n"
            f"🆔 Site ID: `{site_acc_id}`\n"
            f"⏰ Expires: {mins}m {secs}s",
            parse_mode="Markdown")

        asyncio.create_task(
            _poll_wa_pairing(uid, str(doc_id), site_acc_id, pair_idx, ctx.bot))

    except Exception as e:
        logger.error(f"[WA PAIR] {e}")
        await free_slot(pair_idx, "wa_pair")
        try: await wait_msg.delete()
        except Exception: pass
        await update.message.reply_text(
            f"❌ Error aaya: `{e}`\n\nDobara try karo.", parse_mode="Markdown", reply_markup=kb_main())

    return ConversationHandler.END


async def _poll_wa_pairing(uid, doc_id, site_acc_id, cookie_idx, bot, max_wait=200, interval=10):
    logger.info(f"[WA POLL] start uid={uid} site_acc_id={site_acc_id}")
    loop = asyncio.get_event_loop()
    elapsed = 0

    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        try:
            _, status_resp = await loop.run_in_executor(
                None, site, "GET", f"/api/ws/status?account_id={site_acc_id}", None, cookie_idx)

            binding = status_resp.get("data", {}).get("binding", {})
            authorized = binding.get("authorized", False)
            phase = binding.get("phase", "")
            reward_points = binding.get("reward_points", 0)

            logger.info(f"[WA POLL] uid={uid} elapsed={elapsed} phase={phase} authorized={authorized}")

            if authorized and phase == "online":
                await ws_accounts_col.update_one(
                    {"_id": ObjectId(doc_id)},
                    {"$set": {
                        "status": "accepted",
                        "authorized": True,
                        "phase": phase,
                        "reward_points": reward_points,
                        "accepted_at": datetime.now(),
                    }})

                await free_slot(cookie_idx, "wa_pair")

                await bot.send_message(
                    uid,
                    f"🎉 *WhatsApp Account Link Ho Gaya!*\n\n"
                    f"📱 `{binding.get('requested_phone', '')}`\n"
                    f"✅ Status: Online\n\n"
                    f"Ab *💬 WA Task* kar sakte ho!",
                    parse_mode="Markdown", reply_markup=kb_main())

                await bot.send_message(
                    ADMIN_ID,
                    f"✅ *WA Pairing Complete*\n\n"
                    f"👤 User: `{uid}`\n"
                    f"📱 `{binding.get('requested_phone', '')}`\n"
                    f"🆔 Site ID: `{site_acc_id}`\n"
                    f"🍪 Cookie: `{cookie_idx + 1}`",
                    parse_mode="Markdown")
                return

        except Exception as e:
            logger.error(f"[WA POLL] {e}")

    logger.info(f"[WA POLL] timeout uid={uid}")
    await free_slot(cookie_idx, "wa_pair")
    existing = await ws_accounts_col.find_one({"_id": ObjectId(doc_id)})
    if existing and existing.get("status") == "pairing":
        await ws_accounts_col.update_one(
            {"_id": ObjectId(doc_id)}, {"$set": {"status": "expired"}})
        try:
            await bot.send_message(
                uid,
                "⏰ *Pairing expire ho gayi!*\n\n"
                "Code 3 minute mein enter nahi hua.\n"
                "Dobara *➕ Add WA Account* dabao.",
                parse_mode="Markdown", reply_markup=kb_main())
        except Exception:
            pass


async def cb_wa_pair_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Check ho raha hai...")
    uid = q.from_user.id
    doc_id = q.data.replace("wa_pair_check_", "")

    doc = await ws_accounts_col.find_one({"_id": ObjectId(doc_id), "uid": uid})
    if not doc:
        await q.edit_message_text("❌ Pairing record nahi mila."); return

    if doc["status"] == "accepted":
        await q.edit_message_text(
            "✅ *Pehle se link ho chuka hai!*\n\nAb WA Task kar sakte ho.",
            parse_mode="Markdown"); return

    if doc["status"] == "expired":
        await q.edit_message_text(
            "⏰ *Pairing expire ho gayi.*\n\nDobara Add WA Account dabao.",
            parse_mode="Markdown"); return

    site_acc_id = doc.get("site_account_id")
    cookie_idx  = doc.get("cookie_idx", 0)
    loop = asyncio.get_event_loop()

    try:
        _, status_resp = await loop.run_in_executor(
            None, site, "GET", f"/api/ws/status?account_id={site_acc_id}", None, cookie_idx)

        binding    = status_resp.get("data", {}).get("binding", {})
        authorized = binding.get("authorized", False)
        phase      = binding.get("phase", "")

        if authorized and phase == "online":
            await ws_accounts_col.update_one(
                {"_id": ObjectId(doc_id)},
                {"$set": {"status": "accepted", "authorized": True,
                           "phase": phase, "accepted_at": datetime.now()}})
            await q.edit_message_text(
                "🎉 *WhatsApp Account Link Ho Gaya!*\n\n"
                "Ab 💬 WA Task kar sakte ho!",
                parse_mode="Markdown")
            await ctx.bot.send_message(uid, "👇", reply_markup=kb_main())
        else:
            left_secs = max(0, int((doc["pair_expires_at"] - datetime.now()).total_seconds()))
            await q.edit_message_text(
                f"⏳ *Abhi linked nahi hua.*\n\n"
                f"Phase: `{phase}` | Authorized: `{authorized}`\n"
                f"⏰ {left_secs} sec baaki\n\n"
                f"WA app mein code enter karo phir dobara check karo.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Dobara Check Karo", callback_data=f"wa_pair_check_{doc_id}")
                ]]))
    except Exception as e:
        await q.edit_message_text(f"❌ Check failed: `{e}`", parse_mode="Markdown")

# ══════════════════════════════════════════════════════
#  ADMIN — WA ACCOUNT MANAGEMENT
# ══════════════════════════════════════════════════════

async def cmd_ws_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        uid = update.effective_user.id
        accs = await ws_accounts_col.find({"uid": uid}).sort("created_at", -1).to_list(20)
        if not accs:
            await update.message.reply_text(
                "📱 *Koi WA account nahi hai.*\n\n"
                "➕ Add WA Account button dabao.",
                parse_mode="Markdown")
        else:
            lines = []
            for a in accs:
                emoji = {"accepted": "✅", "pairing": "⏳", "expired": "⏰", "removed": "🗑"}.get(a["status"], "❓")
                lines.append(f"{emoji} `{a.get('phone')}` — {a['status']}")
            await update.message.reply_text(
                "📱 *Aapke WhatsApp Accounts:*\n\n" + "\n".join(lines),
                parse_mode="Markdown")
        return

    accs = await ws_accounts_col.find({"status": "accepted"}).sort("accepted_at", -1).to_list(60)
    if not accs:
        await update.message.reply_text("Koi accepted WA account nahi hai."); return
    kb = [[InlineKeyboardButton(
        f"{a.get('phone', '?')} | ID {a.get('site_account_id')} | 🍪{a.get('cookie_idx', 0)+1}",
        callback_data=f"wsadm_ed_{a['_id']}")] for a in accs]
    await update.message.reply_text(
        "📋 *WA Accepted Accounts*\nEdit karne ke liye account chuno:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


async def cmd_del_ws(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sirf admin!"); return
    parts = update.message.text.split()
    if len(parts) < 2:
        await update.message.reply_text("Format: `/delws <site_account_id>`", parse_mode="Markdown"); return
    try:
        site_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Valid site account ID do."); return
    acc = await ws_accounts_col.find_one({"site_account_id": site_id})
    if not acc:
        await update.message.reply_text(f"❌ Site ID {site_id} wala WA account nahi mila."); return
    await ws_accounts_col.update_one({"_id": acc["_id"]}, {"$set": {"status": "removed"}})
    await update.message.reply_text(
        f"✅ WA Account remove kar diya:\n`{acc.get('phone')}` (ID: {site_id})",
        parse_mode="Markdown")


async def cb_wsadm_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    aid = q.data.replace("wsadm_ed_", "")
    a = await ws_accounts_col.find_one({"_id": ObjectId(aid)})
    if not a:
        await q.edit_message_text("❌ Account nahi mila."); return
    await q.edit_message_text(
        f"📱 WA: `{a.get('phone')}`\n"
        f"👤 User: `{a['uid']}`\n"
        f"🔢 Site ID: {a.get('site_account_id')}\n"
        f"🍪 Cookie: {a.get('cookie_idx', 0)+1}\n\nKya badalna hai?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🍪 Cookie badlo", callback_data=f"wsadm_ckp_{aid}")],
            [InlineKeyboardButton("❌ Remove Account", callback_data=f"wsadm_del_{aid}")],
        ]))


async def cb_wsadm_ckpick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    aid = q.data.replace("wsadm_ckp_", "")
    rows, row = [], []
    for i in range(len(JARS)):
        row.append(InlineKeyboardButton(f"🍪 Cookie {i+1}", callback_data=f"wsadm_cks_{aid}_{i}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row: rows.append(row)
    await q.edit_message_text("🍪 Naya cookie chuno:", reply_markup=InlineKeyboardMarkup(rows))


async def cb_wsadm_ckset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    parts = q.data.split("_")
    aid = parts[2]
    idx = int(parts[3])
    await ws_accounts_col.update_one({"_id": ObjectId(aid)}, {"$set": {"cookie_idx": idx}})
    await q.edit_message_text(f"✅ WA Account cookie {idx+1} set ho gaya.")


async def cb_wsadm_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()
    aid = q.data.replace("wsadm_del_", "")
    acc = await ws_accounts_col.find_one({"_id": ObjectId(aid)})
    if not acc:
        await q.edit_message_text("❌ Account nahi mila."); return
    await ws_accounts_col.update_one({"_id": ObjectId(aid)}, {"$set": {"status": "removed"}})
    await q.edit_message_text(f"✅ WA Account `{acc.get('phone')}` remove kar diya.", parse_mode="Markdown")

# ══════════════════════════════════════════════════════
#  ADD TG ACCOUNT (Telethon OTP flow)
# ══════════════════════════════════════════════════════
async def msg_add_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📱 *TG Account Add Karo*\n\n"
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
        f"🔔 *New TG Account Request*\n\n"
        f"👤 Name: {name}\n"
        f"📱 Phone: `{phone}`\n"
        f"🆔 TG ID: `{me.id}`\n"
        f"👤 User ID: `{uid}`\n\n"
        f"Accept karte waqt site account ID bhejo.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"acc_accept_{uid}_{me.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"acc_reject_{uid}_{me.id}"),
            InlineKeyboardButton("📨 Fetch OTP", callback_data=f"fetch_otp_{uid}_{me.id}"),
        ]]))
    await tg.disconnect()

# ══════════════════════════════════════════════════════
#  FETCH OTP
# ══════════════════════════════════════════════════════
async def cb_fetch_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True)
        return
    await q.answer("⏳ OTP fetch ho raha hai...")

    parts = q.data.split("_")
    uid   = int(parts[2])
    tg_id = int(parts[3])

    acc = await accounts_col.find_one({"uid": uid, "tg_id": tg_id})
    if not acc:
        await ctx.bot.send_message(ADMIN_ID, "❌ Account nahi mila DB mein.")
        return

    phone = acc["phone"]
    safe  = phone.replace("+", "")
    session_path = f"sessions/{uid}_{safe}"

    tg = TelegramClient(session_path, API_ID, API_HASH)
    try:
        await tg.connect()
        if not await tg.is_user_authorized():
            await ctx.bot.send_message(
                ADMIN_ID,
                f"⚠ `{phone}` ka session authorized nahi hai.\nUser ko dobara login karwao.",
                parse_mode="Markdown")
            return

        messages = await tg.get_messages(777000, limit=5)
        if not messages:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"📭 `{phone}` ke inbox mein Telegram se koi message nahi mila.",
                parse_mode="Markdown")
            return

        latest = messages[0]
        msg_time = latest.date.strftime("%d/%m/%Y %H:%M:%S UTC") if latest.date else "N/A"
        await ctx.bot.send_message(
            ADMIN_ID,
            f"📨 *Latest OTP — `{phone}`*\n\n"
            f"🕐 *Time:* `{msg_time}`\n\n"
            f"📝 *Message:*\n{latest.text or '(empty)'}",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(f"[FETCH OTP] {e}")
        await ctx.bot.send_message(
            ADMIN_ID, f"❌ OTP fetch nahi hua `{phone}`:\n`{e}`", parse_mode="Markdown")
    finally:
        try: await tg.disconnect()
        except Exception: pass

# ── Admin Accept / Reject TG ─────────────────────────
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
        uid, "❌ Aapka TG account reject ho gaya.\nDobara try karo ya admin se contact karo.",
        reply_markup=kb_main())


async def admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    txt = update.message.text.strip()

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
        "🍪 Ye TG account site pe kis cookie wale login me hai?",
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
    await q.edit_message_text(f"✅ TG Account Accepted!\nSite ID: {site_id}\n🍪 Cookie: {idx+1}")
    await Bot(BOT_TOKEN).send_message(
        uid, "🎉 *Aapka TG Account Accept Ho Gaya!*\n\nAb TG Task kar sakte ho.",
        parse_mode="Markdown", reply_markup=kb_main())

# ── Admin: TG accounts list / edit ──────────────────────
async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    accs = await accounts_col.find({"status": "accepted"}).sort("accepted_at", -1).to_list(60)
    if not accs:
        await update.message.reply_text("Koi accepted TG account nahi hai."); return
    kb = [[InlineKeyboardButton(
        f"{a['phone']} | ID {a.get('site_account_id')} | 🍪{a.get('cookie_idx', 0)+1}",
        callback_data=f"adm_ed_{a['_id']}")] for a in accs]
    await update.message.reply_text(
        "📋 *Accepted TG Accounts*\nEdit karne ke liye account chuno:",
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
        f"Kitna withdraw karna hai?\n"
        f"_(Minimum ₹{MIN_WITHDRAW} | Har withdrawal pe ₹{WITHDRAW_TAX} tax kategi)_\n\n"
        f"/cancel to cancel",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    return W_AMOUNT


async def got_w_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = await get_user(uid)
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Valid amount likho."); return W_AMOUNT
    if amt < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ Minimum ₹{MIN_WITHDRAW}!"); return W_AMOUNT
    if amt > u["balance"]:
        await update.message.reply_text(f"❌ Balance kam hai! Available: ₹{u['balance']:.2f}"); return W_AMOUNT
    ctx.user_data["w_amt"] = amt
    payout = amt - WITHDRAW_TAX
    await update.message.reply_text(
        f"💰 Withdraw: ₹{amt:.2f}\n"
        f"🧾 Tax: ₹{WITHDRAW_TAX:.2f}\n"
        f"✅ Aapko milega: ₹{payout:.2f}\n\n"
        f"📲 Ab apna UPI ID bhejo:\n(e.g. name@upi)\n\n/cancel to cancel")
    return W_UPI


async def got_w_upi(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    amt = ctx.user_data.get("w_amt", 0)
    upi = update.message.text.strip()

    res = await users_col.update_one(
        {"uid": uid, "balance": {"$gte": amt}}, {"$inc": {"balance": -amt}})
    if res.modified_count == 0:
        await update.message.reply_text("❌ Balance kam hai!", reply_markup=kb_main())
        return ConversationHandler.END

    payout = amt - WITHDRAW_TAX
    wd_res = await withdraw_col.insert_one({
        "uid": uid, "amount": amt, "tax": WITHDRAW_TAX, "payout": payout,
        "upi": upi, "status": "pending", "requested_at": datetime.now()
    })
    await update.message.reply_text(
        f"✅ *Withdrawal Request Bheja!*\n\n"
        f"💰 Amount: ₹{amt:.2f}\n"
        f"🧾 Tax: ₹{WITHDRAW_TAX:.2f}\n"
        f"✅ Aapko milega: ₹{payout:.2f}\n"
        f"📲 UPI: {upi}\n\n"
        f"24-48 hours mein process hogi.",
        parse_mode="Markdown", reply_markup=kb_main())
    await Bot(BOT_TOKEN).send_message(
        ADMIN_ID,
        f"💸 *Withdrawal Request*\n\n"
        f"👤 User: `{uid}`\n"
        f"💰 Amount: ₹{amt:.2f}\n"
        f"🧾 Tax: ₹{WITHDRAW_TAX:.2f}\n"
        f"✅ *Pay karna hai: ₹{payout:.2f}*\n"
        f"📲 `{upi}`",
        parse_mode="Markdown",
        reply_markup=kb_wd_actions(wd_res.inserted_id))
    return ConversationHandler.END

# ══════════════════════════════════════════════════════
#  ADMIN PANEL  (/admin)
# ══════════════════════════════════════════════════════
def kb_admin_panel(refresh=False):
    label = "🔄 Refresh All IDs" if refresh else "🔍 Check All IDs"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data="admp_checkall")],
        [InlineKeyboardButton("💸 Pending Withdrawals", callback_data="admp_wd")],
    ])


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🛠 ADMIN PANEL\n\n"
        f"🍪 Cookies loaded: {len(JARS)}\n\n"
        "Website pe jitne bhi accounts hain unki ID dekhne ke liye button dabao 👇",
        reply_markup=kb_admin_panel())


async def cb_admp_checkall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer("⏳ Fetch ho raha hai...")
    await q.edit_message_text("⏳ Website se saari IDs la rahe hain...")

    loop = asyncio.get_event_loop()

    linked = {}
    async for a in accounts_col.find({"status": "accepted"}):
        linked[(a.get("cookie_idx", 0), a.get("site_account_id"))] = a["uid"]

    lines = ["📋 WEBSITE ACCOUNTS — ALL IDs", ""]
    total = online_n = 0

    for idx in range(len(JARS)):
        try:
            code, data = await loop.run_in_executor(
                None, site, "GET", "/api/telegram/accounts", None, idx)
        except Exception as e:
            lines += [f"🍪 Cookie {idx+1}", f"  ❌ Error: {e}", ""]
            continue

        accs = data.get("data") if isinstance(data, dict) else None
        if code != 200 or not isinstance(accs, list):
            lines += [f"🍪 Cookie {idx+1}",
                      f"  ⚠ Response {code} (cookie expire ho sakti hai)", ""]
            continue

        lines.append(f"🍪 Cookie {idx+1} — {len(accs)} accounts")
        for acc in accs:
            is_online = bool(acc.get("online"))
            total += 1
            online_n += 1 if is_online else 0
            name = (acc.get("display_name") or acc.get("username")
                    or acc.get("first_name") or "N/A")
            phone = acc.get("requested_phone") or acc.get("phone") or "N/A"
            sid = acc.get("id")
            status = "✅ Online" if is_online else "❌ Offline"
            owner = linked.get((idx, sid))
            bot_tag = f" | 🤖 User {owner}" if owner else " | ⚪ Bot me nahi"
            lines.append(f"  ID: {sid} | {name} | +{phone} | {status}{bot_tag}")
        lines.append("")

    lines.append(f"📊 Total: {total} | ✅ Online: {online_n} | ❌ Offline: {total - online_n}")

    chunks, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > 3800:
            chunks.append(cur); cur = ""
        cur += ln + "\n"
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks):
        last = (i == len(chunks) - 1)
        markup = kb_admin_panel(refresh=True) if last else None
        if i == 0:
            await q.edit_message_text(chunk, reply_markup=markup)
        else:
            await ctx.bot.send_message(q.message.chat_id, chunk, reply_markup=markup)


async def cmd_wsdebug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    t = await ws_tasks_col.find_one({}, sort=[("created_at", -1)])
    if not t:
        await update.message.reply_text("Koi WA task DB me nahi hai."); return
    raw = t.get("ad_copy", "") or ""
    odd_map = {"\r": "CR", "\t": "TAB", "\u00a0": "NBSP", "\u200b": "ZWSP",
               "\u200c": "ZWNJ", "\u200d": "ZWJ", "\u2028": "LS",
               "\u2029": "PS", "\ufeff": "BOM"}
    odd = {name: raw.count(ch) for ch, name in odd_map.items() if ch in raw}
    text = (
        f"🔧 WA DEBUG (last task)\n"
        f"Status: {t.get('status')}\n"
        f"Target: {t.get('target_phone')}\n"
        f"Length: {len(raw)}\n"
        f"LF count: {raw.count(chr(10))}\n"
        f"Double spaces: {raw.count('  ')}\n"
        f"Starts: {raw[:3]!r} | Ends: {raw[-3:]!r}\n"
        f"Hidden chars: {odd or 'none'}\n"
        f"WA_LINK_LF: {WA_LINK_LF}\n\n"
        f"RAW:\n{raw!r}"
    )
    await update.message.reply_text(text[:3900])

# ── Admin: Withdrawal management ─────────────────────
def kb_wd_actions(wid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Paid (Accept)", callback_data=f"wd_ok_{wid}"),
        InlineKeyboardButton("❌ Reject (Refund)", callback_data=f"wd_no_{wid}"),
    ]])


def wd_text(w):
    amount = w.get("amount", 0)
    tax = w.get("tax", 0)
    payout = w.get("payout", amount)
    when = w.get("requested_at")
    when_s = when.strftime("%d/%m %H:%M") if when else "-"
    return (
        f"💸 WITHDRAWAL REQUEST\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: {w.get('uid')}\n"
        f"💰 Amount: ₹{amount:.2f}\n"
        f"🧾 Tax: ₹{tax:.2f}\n"
        f"✅ Pay karna hai: ₹{payout:.2f}\n"
        f"📲 UPI: {w.get('upi')}\n"
        f"🕐 {when_s}"
    )


async def cb_admp_wd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return
    await q.answer()

    pend = await withdraw_col.find({"status": "pending"}).sort("requested_at", 1).to_list(30)
    if not pend:
        await q.edit_message_text(
            "✅ Koi pending withdrawal nahi hai.", reply_markup=kb_admin_panel())
        return

    total = sum(w.get("payout", w.get("amount", 0)) for w in pend)
    await q.edit_message_text(
        f"💸 Pending Withdrawals: {len(pend)}\n"
        f"💰 Total pay karna hai: ₹{total:.2f}\n\n"
        f"Neeche har request ke saath Paid / Reject button hai 👇",
        reply_markup=kb_admin_panel())

    for w in pend:
        await ctx.bot.send_message(
            q.message.chat_id, wd_text(w), reply_markup=kb_wd_actions(w["_id"]))


async def cb_wd_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ Sirf admin!", show_alert=True); return

    _, action, wid = q.data.split("_", 2)
    new_status = "paid" if action == "ok" else "rejected"

    w = await withdraw_col.find_one_and_update(
        {"_id": ObjectId(wid), "status": "pending"},
        {"$set": {"status": new_status, "processed_at": datetime.now()}})
    if not w:
        await q.answer("ℹ Ye request pehle hi process ho chuki hai.", show_alert=True)
        try: await q.edit_message_reply_markup(reply_markup=None)
        except Exception: pass
        return
    await q.answer("✅ Done")

    uid = w["uid"]
    amount = w.get("amount", 0)
    payout = w.get("payout", amount)

    if new_status == "paid":
        tag = "\n\n✅ PAID / ACCEPTED"
        user_msg = (
            f"✅ Aapka withdrawal accept ho gaya!\n\n"
            f"💰 ₹{payout:.2f} aapke UPI ({w.get('upi')}) pe bhej diye gaye hain.")
    else:
        await add_bal(uid, amount)
        tag = "\n\n❌ REJECTED (balance refund ho gaya)"
        user_msg = (
            f"❌ Aapka withdrawal reject ho gaya.\n\n"
            f"💰 ₹{amount:.2f} aapke balance me wapas add kar diye gaye hain.")

    try:
        await ctx.bot.send_message(uid, user_msg)
    except Exception as e:
        logger.warning(f"[WD NOTIFY] {e}")

    try:
        await q.edit_message_text((q.message.text or "") + tag)
    except Exception:
        pass


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

    add_wa_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Add WA Account$"), msg_add_wa_account)],
        states={
            ADD_WA_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_wa_phone)],
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
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("wsdebug", cmd_wsdebug))
    app.add_handler(CallbackQueryHandler(cb_admp_checkall, pattern="^admp_checkall$"))
    app.add_handler(CallbackQueryHandler(cb_admp_wd, pattern="^admp_wd$"))
    app.add_handler(CallbackQueryHandler(cb_wd_action, pattern="^wd_(ok|no)_"))
    app.add_handler(add_conv)
    app.add_handler(add_wa_conv)
    app.add_handler(w_conv)

    app.add_handler(MessageHandler(filters.Regex("^📋 TG Task$"), msg_tg_task))
    app.add_handler(CallbackQueryHandler(cb_pick_acc,    pattern="^pick_acc_"))
    app.add_handler(CallbackQueryHandler(cb_task_done,   pattern="^task_done_"))

    app.add_handler(MessageHandler(filters.Regex("^💬 WA Task$"), msg_ws_task))
    app.add_handler(CallbackQueryHandler(cb_pick_ws,     pattern="^pick_ws_"))
    app.add_handler(CallbackQueryHandler(cb_ws_task_done, pattern="^ws_done_"))

    app.add_handler(CallbackQueryHandler(cb_wa_pair_check, pattern="^wa_pair_check_"))

    app.add_handler(CommandHandler("wsaccounts", cmd_ws_accounts))
    app.add_handler(CommandHandler("delws", cmd_del_ws))
    app.add_handler(CallbackQueryHandler(cb_wsadm_edit,   pattern="^wsadm_ed_"))
    app.add_handler(CallbackQueryHandler(cb_wsadm_ckpick, pattern="^wsadm_ckp_"))
    app.add_handler(CallbackQueryHandler(cb_wsadm_ckset,  pattern="^wsadm_cks_"))
    app.add_handler(CallbackQueryHandler(cb_wsadm_del,    pattern="^wsadm_del_"))

    app.add_handler(CallbackQueryHandler(cb_check_join,  pattern="^check_join$"))
    app.add_handler(CallbackQueryHandler(cb_back_main,   pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(cb_acc_accept,  pattern="^acc_accept_"))
    app.add_handler(CallbackQueryHandler(cb_acc_reject,  pattern="^acc_reject_"))
    app.add_handler(CallbackQueryHandler(cb_fetch_otp,   pattern="^fetch_otp_"))
    app.add_handler(CallbackQueryHandler(cb_acc_cookie,  pattern="^acc_ck_"))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CallbackQueryHandler(cb_adm_edit,    pattern="^adm_ed_"))
    app.add_handler(CallbackQueryHandler(cb_adm_sid,     pattern="^adm_sid_"))
    app.add_handler(CallbackQueryHandler(cb_adm_ckpick,  pattern="^adm_ckp_"))
    app.add_handler(CallbackQueryHandler(cb_adm_ckset,   pattern="^adm_cks_"))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(ADMIN_ID), admin_text))

    logger.info(f"[API] id={API_ID} hash={API_HASH[:4]}...{API_HASH[-4:]} len={len(API_HASH)}")
    logger.info("🤖 Bot starting (TG + WA tasks)...")
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
