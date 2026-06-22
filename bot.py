import os, io, base64, logging, sqlite3, string, random
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import httpx
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

load_dotenv()

TOKEN = os.getenv("TOKEN", "")
API_KEY = os.getenv("API_KEY", "")
API_URL = os.getenv("API_URL", "https://api.freemodel.dev/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-5.5")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "TXYZxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path("data.db")

PLANS = {
    "day": {"name": "۱ روزه", "price": 5, "days": 1, "max": -1, "cooldown": 30,
            "desc": "تستی — هر ۳۰ دقیقه یک تحلیل"},
    "month_basic": {"name": "۱ ماهه پایه", "price": 30, "days": 30, "max": 500, "cooldown": 15,
                    "desc": "۵۰۰ تحلیل — هر ۱۵ دقیقه"},
    "month_unlimited": {"name": "۱ ماهه نامحدود", "price": 60, "days": 30, "max": -1, "cooldown": 0,
                        "desc": "نامحدود — بدون محدودیت"},
}

# ═══════════════════════ DATABASE ═══════════════════════

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            plan TEXT NOT NULL DEFAULT '',
            status TEXT DEFAULT 'pending',
            tx_hash TEXT DEFAULT '',
            activated_at TEXT,
            expires_at TEXT,
            usage_count INTEGER DEFAULT 0,
            max_usage INTEGER DEFAULT 0,
            cooldown_minutes INTEGER DEFAULT 0,
            last_analysis_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pending_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            plan TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            status TEXT DEFAULT 'pending'
        );
    """)
    conn.commit(); conn.close()

def get_sub(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(r) if r else None

def is_active(user_id: int) -> bool:
    sub = get_sub(user_id)
    if not sub or sub["status"] != "active": return False
    if sub["expires_at"] and datetime.fromisoformat(sub["expires_at"]) < datetime.now():
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE subscriptions SET status='expired' WHERE user_id=?", (user_id,))
        conn.commit(); conn.close()
        return False
    return True

def can_analyze(user_id: int) -> tuple:
    sub = get_sub(user_id)
    if not sub or sub["status"] != "active":
        return False, "اشتراک فعال ندارید"
    if sub["expires_at"] and datetime.fromisoformat(sub["expires_at"]) < datetime.now():
        return False, "اشتراک شما منقضی شده"
    if sub["max_usage"] > 0 and sub["usage_count"] >= sub["max_usage"]:
        return False, f"سقف تحلیل ({sub['max_usage']}) تمام شده"
    if sub["cooldown_minutes"] > 0 and sub["last_analysis_at"]:
        last = datetime.fromisoformat(sub["last_analysis_at"])
        diff = (datetime.now() - last).total_seconds() / 60
        if diff < sub["cooldown_minutes"]:
            remaining = int(sub["cooldown_minutes"] - diff)
            return False, f"صبر کن {remaining} دقیقه"
    return True, "ok"

def record_analysis(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE subscriptions SET usage_count=usage_count+1, last_analysis_at=? WHERE user_id=?",
                 (datetime.now().isoformat(), user_id))
    conn.commit(); conn.close()

def activate_sub(user_id: int, username: str, plan: str):
    p = PLANS[plan]
    expires = (datetime.now() + timedelta(days=p["days"])).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO subscriptions (user_id, username, plan, status, activated_at, expires_at,
            usage_count, max_usage, cooldown_minutes, last_analysis_at)
        VALUES (?, ?, ?, 'active', ?, ?, 0, ?, ?, '')
        ON CONFLICT(user_id) DO UPDATE SET
            plan=excluded.plan, status='active', activated_at=excluded.activated_at,
            expires_at=excluded.expires_at, usage_count=0, max_usage=excluded.max_usage,
            cooldown_minutes=excluded.cooldown_minutes, last_analysis_at=''
    """, (user_id, username, plan, datetime.now().isoformat(), expires, p["max"], p["cooldown"]))
    conn.commit(); conn.close()

# ═══════════════════════ SYSTEM PROMPT ═══════════════════════

SYSTEM_PROMPT = (
    "شما 'شهباز کور' هستید — متخصص حرفه‌ای Mortal Kombat.\n"
    "خروجی فقط فارسی.\n"
    "قوانین:\n"
    "1. تحلیل عمیق کاراکترها: سبک بازی، برتری رنج.\n"
    "2. ضرایب را استخراج کن.\n"
    "3. اولویت با ضریب >= ۱.۸ برای مارتینگل.\n"
    "4. احتمال واقع‌بینانه.\n"
    "5. اگر نامشخص: وضعیت: خطرناک. وارد نشو.\n\n"
    "فرمت:\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "مچ‌آپ: [A] vs [B]\n\n"
    "تحلیل:\n"
    "▸ [A]: [خلاصه]\n"
    "▸ [B]: [خلاصه]\n"
    "▸ برتری: [خلاصه]\n\n"
    "پیشنهاد مارتینگل:\n"
    "▸ زیر/بالای X.X ثانیه — ضریب X.XXX\n"
    "▸ احتمال: XX–XX٪\n"
    "▸ دلیل: [۲-۳ کلمه]\n"
    "━━━━━━━━━━━━━━━━━━"
)

MAX_IMAGE_SIZE = 8 * 1024 * 1024
MAX_DIMENSION = 2048

# ═══════════════════════ HELPERS ═══════════════════════

def compress_image(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA": img = img.convert("RGB")
    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        r = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
        img = img.resize((int(w * r), int(h * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()

def generate_key() -> str:
    c = string.ascii_uppercase + string.digits
    return "-".join("".join(random.choices(c, k=4)) for _ in range(3))

def split_msg(text: str, ml: int = 3800) -> list:
    if len(text) <= ml: return [text]
    parts = []
    while text:
        if len(text) <= ml: parts.append(text); break
        s = text.rfind("\n", 0, ml)
        if s < ml // 2: s = ml
        parts.append(text[:s]); text = text[s:].strip()
    return parts

# ═══════════════════════ AI CALL ═══════════════════════

async def call_ai(image_b64: str, prompt: str = "") -> str:
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    content = [{"type": "text", "text": prompt or "اسکرین‌شات مسابقه Mortal Kombat را تحلیل کن."}]
    if image_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        "temperature": 0.15, "max_tokens": 1500
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(API_URL, headers=headers, json=payload)
        if r.status_code != 200:
            raise Exception(f"API error {r.status_code}")
        return r.json()["choices"][0]["message"]["content"]

# ═══════════════════════ GATE ═══════════════════════

def gate(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        ok, reason = can_analyze(uid)
        if not ok:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 مشاهده پلن‌ها", callback_data="show_plans")]])
            await update.message.reply_text(
                f"╔══════════════════════╗\n"
                f"  🔐 **شهباز کور**\n"
                f"  {reason}\n"
                f"╚══════════════════════╝\n\n"
                "برای خرید اشتراک:\n`/plans`",
                parse_mode="Markdown", reply_markup=kb
            )
            return
        return await func(update, context)
    return wrapper

# ═══════════════════════ COMMANDS ═══════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"
    sub = get_sub(uid)

    if sub and sub["status"] == "active":
        p = PLANS.get(sub["plan"], {})
        expires = sub["expires_at"][:10] if sub["expires_at"] else "—"
        usage = sub["usage_count"]
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 ارسال عکس", callback_data="send_photo_hint")],
            [InlineKeyboardButton("📊 آمار من", callback_data="my_stats")],
            [InlineKeyboardButton("🔄 تمدید اشتراک", callback_data="show_plans")]
        ])
        await update.message.reply_text(
            f"⚔ **شهباز کور** ⚔\n"
            f"═━━━━━━━━━━━━━━━╕\n"
            f"👤 **{name}**\n"
            f"📦 پلن: {p.get('name', sub['plan'])}\n"
            f"📊 تحلیلها: {usage}/{max_u}\n"
            f"📅 انقضا: {expires}\n\n"
            f"📸 عکس بفرست تا تحلیل کنم\n"
            f"یا `/analyze Smoke vs Sub-Zero`",
            parse_mode="Markdown", reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 مشاهده پلن‌ها", callback_data="show_plans")],
            [InlineKeyboardButton("📩 پشتیبانی", url="https://t.me/admin")]
        ])
        await update.message.reply_text(
            "⚔ **شهباز کور** ⚔\n"
            "ترمینال تحلیلی شرط‌بندی MK\n"
            "مخصوص سیستم مارتینگل\n\n"
            "🔐 **نیاز به اشتراک**\n\n"
            "برای مشاهده پلن‌ها و خرید:\n`/plans`",
            parse_mode="Markdown", reply_markup=kb
        )

async def plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎁 ۱ روزه — $5", callback_data="plan_day")],
        [InlineKeyboardButton(f"📦 ۱ ماهه پایه — $30", callback_data="plan_month_basic")],
        [InlineKeyboardButton(f"👑 ۱ ماهه نامحدود — $60", callback_data="plan_month_unlimited")]
    ])
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "     📦 **پلن‌های اشتراک**\n"
        "╚══════════════════════╝\n\n"
        "🎁 **۱ روزه — $5**\n"
        "   هر ۳۰ دقیقه یک تحلیل\n\n"
        "📦 **۱ ماهه پایه — $30**\n"
        "   ۵۰۰ تحلیل — هر ۱۵ دقیقه\n\n"
        "👑 **۱ ماهه نامحدود — $60**\n"
        "   بدون محدودیت\n\n"
        "💰 پرداخت: **USDT (TRC20)**\n\n"
        "پلن مورد نظر را انتخاب کنید:",
        parse_mode="Markdown", reply_markup=kb
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sub = get_sub(uid)
    if not sub or sub["status"] != "active":
        return await update.message.reply_text("اشتراک فعال ندارید. `/plans`", parse_mode="Markdown")
    p = PLANS.get(sub["plan"], {})
    name = update.effective_user.first_name or "کاربر"
    max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
    await update.message.reply_text(
        f"👤 **{name}**\n"
        f"📦 پلن: {p.get('name', sub['plan'])}\n"
        f"📊 تحلیلها: **{sub['usage_count']}/{max_u}**\n"
        f"📅 انقضا: **{sub['expires_at'][:10]}**\n"
        f"💎 وضعیت: **فعال**",
        parse_mode="Markdown"
    )

# ─── RECEIPT ───

async def receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args
    if not args:
        return await update.message.reply_text(
            "فرمت: `/receipt USDT-TRC20-TXHASH`\n\n"
            "آدرس USDT TRC20 پرداخت شده + TX Hash را بفرست.",
            parse_mode="Markdown"
        )

    tx = " ".join(args).strip()
    if len(tx) < 10:
        return await update.message.reply_text("❌ TX Hash نامعتبر است.")

    # Find pending plan selection or ask user
    # Store pending payment
    conn = sqlite3.connect(str(DB_PATH))
    # Check if user already has pending
    existing = conn.execute("SELECT id FROM pending_payments WHERE user_id=? AND status='pending'", (uid,)).fetchone()
    if existing:
        conn.execute("UPDATE pending_payments SET tx_hash=?, created_at=datetime('now') WHERE id=?", (tx, existing[0]))
    else:
        conn.execute("INSERT INTO pending_payments (user_id, username, plan, tx_hash) VALUES (?, ?, ?, ?)",
                     (uid, update.effective_user.username or "", "unknown", tx))
    conn.commit(); conn.close()

    # Notify admin
    if ADMIN_ID:
        try:
            name = update.effective_user.first_name or "کاربر"
            uname = update.effective_user.username or "—"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تایید", callback_data=f"approve_{uid}"),
                 InlineKeyboardButton("❌ رد", callback_data=f"reject_{uid}")]
            ])
            await context.bot.send_message(ADMIN_ID,
                f"💰 **رسید پرداخت**\n\n"
                f"👤 {name} (@{uname})\n"
                f"🆔 `{uid}`\n"
                f"📝 TX: `{tx[:20]}...`\n\n"
                "تایید یا رد کن:",
                parse_mode="Markdown", reply_markup=kb
            )
        except: pass

    await update.message.reply_text(
        "✅ **رسید شما ثبت شد**\n\n"
        "ادمین در حال بررسی است.\n"
        "بعد از تایید، اشتراک شما فعال می‌شود.",
        parse_mode="Markdown"
    )

# ─── ADMIN COMMANDS ───

def is_admin(uid): return uid == ADMIN_ID

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT id, user_id, username, plan, tx_hash, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC").fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("📭 رسید در انتظار نیست.")
    msg = "💰 **رسیدهای در انتظار**\n\n"
    for r in rows:
        msg += f"🆔 `{r[1]}` — @{r[2] or '—'} — `{r[4][:15]}...` — {r[5][:16]}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def activate_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    if len(context.args) < 2:
        return await update.message.reply_text("فرمت: `/act USER_ID PLAN`\n Plans: day, month_basic, month_unlimited", parse_mode="Markdown")
    try:
        target_uid = int(context.args[0])
        plan = context.args[1]
    except: return await update.message.reply_text("فرمت: `/act USER_ID PLAN`", parse_mode="Markdown")
    if plan not in PLANS:
        return await update.message.reply_text(f"پلن نامعتبر. یکی از: {', '.join(PLANS.keys())}")
    activate_sub(target_uid, "", plan)
    p = PLANS[plan]
    try:
        await context.bot.send_message(target_uid,
            f"✅ **اشتراک شما فعال شد!**\n\n"
            f"📦 پلن: {p['name']}\n"
            f"📊 محدودیت: {p['max'] if p['max'] > 0 else '∞'} تحلیل\n"
            f"⏰ کول‌دawn: هر {p['cooldown']} دقیقه\n"
            f"📅 مدت: {p['days']} روز\n\n"
            "📸 عکس بفرست برای تحلیل!",
            parse_mode="Markdown"
        )
    except: pass
    await update.message.reply_text(f"✅ اشتراک {plan} برای {target_uid} فعال شد.")

# ═══════════════════════ CALLBACKS ═══════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "show_plans":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 ۱ روزه — $5", callback_data="plan_day")],
            [InlineKeyboardButton("📦 ۱ ماهه پایه — $30", callback_data="plan_month_basic")],
            [InlineKeyboardButton("👑 ۱ ماهه نامحدود — $60", callback_data="plan_month_unlimited")]
        ])
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "     📦 **پلن‌های اشتراک**\n"
            "╚══════════════════════╝\n\n"
            "🎁 **۱ روزه — $5**\n   هر ۳۰ دقیقه یک تحلیل\n\n"
            "📦 **۱ ماهه پایه — $30**\n   ۵۰۰ تحلیل — هر ۱۵ دقیقه\n\n"
            "👑 **۱ ماهه نامحدود — $60**\n   بدون محدودیت\n\n"
            "💰 پرداخت: **USDT (TRC20)**\n\n"
            "پلن را انتخاب کنید:",
            parse_mode="Markdown", reply_markup=kb
        )

    elif d.startswith("plan_"):
        plan = d.replace("plan_", "")
        if plan not in PLANS:
            return await q.edit_message_text("پلن نامعتبر.")
        p = PLANS[plan]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 کپی آدرس", callback_data=f"copy_addr")]
        ])
        await q.edit_message_text(
            f"╔══════════════════════╗\n"
            f"  💰 **پرداخت — {p['name']}**\n"
            f"╚══════════════════════╝\n\n"
            f"💵 مبلغ: **${p['price']}**\n"
            f"📦 پلن: **{p['name']}**\n"
            f"📝 {p['desc']}\n\n"
            f"🔹 **آدرس USDT (TRC20):**\n"
            f"`{PAYMENT_ADDRESS}`\n\n"
            f"⚠️ فقط **USDT TRC20** بفرستید.\n"
            f"بعد از پرداخت:\n"
            f"`/receipt TX_HASH`\n\n"
            f"رسید را به ادمین نیز بفرستید.",
            parse_mode="Markdown"
        )

    elif d == "send_photo_hint":
        await q.edit_message_text(
            "📸 فقط عکس اسکرین‌شات رو بفرست.\n"
            "شهباز خودش تحلیل می‌کنه.\n\n"
            "یا: `/analyze Smoke vs Sub-Zero`",
            parse_mode="Markdown"
        )

    elif d == "my_stats":
        uid = q.from_user.id
        sub = get_sub(uid)
        if not sub or sub["status"] != "active":
            return await q.edit_message_text("اشتراک فعال نیست.")
        p = PLANS.get(sub["plan"], {})
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        await q.edit_message_text(
            f"📊 **آمار شما**\n\n"
            f"📦 پلن: {p.get('name', sub['plan'])}\n"
            f"📊 تحلیلها: **{sub['usage_count']}/{max_u}**\n"
            f"📅 انقضا: **{sub['expires_at'][:10]}**",
            parse_mode="Markdown"
        )

    elif d.startswith("approve_"):
        if not is_admin(q.from_user.id):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        target = int(d.split("_")[1])
        plan = "month_basic"  # default
        # Try to find what plan the user was trying to buy
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT plan FROM pending_payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
        if row and row[0] in PLANS:
            plan = row[0]
        conn.execute("UPDATE pending_payments SET status='approved' WHERE user_id=? AND status='pending'", (target,))
        conn.commit(); conn.close()
        activate_sub(target, "", plan)
        p = PLANS[plan]
        try:
            await context.bot.send_message(target,
                f"✅ **اشتراک شما فعال شد!**\n\n"
                f"📦 پلن: {p['name']}\n"
                f"📊 محدودیت: {p['max'] if p['max'] > 0 else '∞'} تحلیل\n"
                f"⏰ کول‌دawn: هر {p['cooldown']} دقیقه\n"
                f"📅 مدت: {p['days']} روز\n\n"
                "📸 عکس بفرست برای تحلیل!",
                parse_mode="Markdown"
            )
        except: pass
        await q.edit_message_text(f"✅ تایید شد — اشتراک {plan} فعال شد.")

    elif d.startswith("reject_"):
        if not is_admin(q.from_user.id):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        target = int(d.split("_")[1])
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE pending_payments SET status='rejected' WHERE user_id=? AND status='pending'", (target,))
        conn.commit(); conn.close()
        try:
            await context.bot.send_message(target, "❌ **رسید شما تایید نشد.**\n\nبا ادمین تماس بگیرید.", parse_mode="Markdown")
        except: pass
        await q.edit_message_text("❌ رد شد.")

# ═══════════════════════ ANALYZE ═══════════════════════

@gate
async def analyze_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or "vs" not in text.lower():
        return await update.message.reply_text("فرمت: `/analyze Smoke vs Sub-Zero`", parse_mode="Markdown")
    msg = await update.message.reply_text("⏳ **شهباز در حال تحلیل...**", parse_mode="Markdown")
    try:
        result = await call_ai("", f"تحلیل مچ‌آپ: {text}")
        record_analysis(update.effective_user.id)
        await msg.delete()
        for p in split_msg(result):
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")

@gate
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ **شهباز در حال تحلیل تصویر...**", parse_mode="Markdown")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        raw = io.BytesIO()
        await file.download_to_memory(raw)
        raw = raw.getvalue()
        if len(raw) > MAX_IMAGE_SIZE:
            return await msg.edit_text("❌ حجم تصویر زیاد. حداکثر ۸MB.")
        compressed = compress_image(raw)
        b64 = base64.b64encode(compressed).decode()
        analysis = await call_ai(b64)
        record_analysis(update.effective_user.id)
        await msg.delete()
        for p in split_msg(analysis):
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:300]}")

# ═══════════════════════ MAIN ═══════════════════════

def main():
    if not TOKEN:   log.error("TOKEN empty"); return
    if not API_KEY: log.error("API_KEY empty"); return
    if ADMIN_ID == 0: log.warning("ADMIN_ID not set")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plans", plans))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("receipt", receipt))
    app.add_handler(CommandHandler("analyze", analyze_text))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("act", activate_manual))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("👑 Shahbaz Core VIP Bot running")
    app.run_polling()

if __name__ == "__main__":
    main()
