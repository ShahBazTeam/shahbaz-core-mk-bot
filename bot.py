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
    "day": {
        "name": "۱ روزه", "price": 5, "days": 1, "max": -1, "cooldown": 30,
        "desc": "تستی — هر ۳۰ دقیقه یک تحلیل", "emoji": "⚡"
    },
    "month_basic": {
        "name": "۱ ماهه پایه", "price": 30, "days": 30, "max": 500, "cooldown": 15,
        "desc": "۵۰۰ تحلیل — هر ۱۵ دقیقه", "emoji": "📦"
    },
    "month_unlimited": {
        "name": "۱ ماهه نامحدود", "price": 60, "days": 30, "max": -1, "cooldown": 0,
        "desc": "نامحدود — بدون محدودیت", "emoji": "👑"
    },
    "year_basic": {
        "name": "۱ ساله پایه", "price": 250, "days": 365, "max": 5000, "cooldown": 25,
        "desc": "۵۰۰۰ تحلیل — هر ۲۵ دقیقه", "emoji": "📅"
    },
    "year_unlimited": {
        "name": "۱ ساله نامحدود", "price": 350, "days": 365, "max": -1, "cooldown": 0,
        "desc": "نامحدود — بدون محدودیت", "emoji": "💎"
    },
}

# ═══════════════════════ BUTTONS ═══════════════════════

def btn(text, data):
    return InlineKeyboardButton(f"✧ {text}", callback_data=data)

def btn_main(text, data):
    return InlineKeyboardButton(f"◈ {text}", callback_data=data)

def btn_back(data="main_menu"):
    return InlineKeyboardButton("◂ بازگشت", callback_data=data)

def btn_action(text, data):
    return InlineKeyboardButton(f"▸ {text}", callback_data=data)

def btn_link(text, url):
    return InlineKeyboardButton(text, url=url)

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
        return False, "سقف تحلیل تمام شده"
    if sub["cooldown_minutes"] > 0 and sub["last_analysis_at"]:
        last = datetime.fromisoformat(sub["last_analysis_at"])
        diff = (datetime.now() - last).total_seconds() / 60
        if diff < sub["cooldown_minutes"]:
            remaining = int(sub["cooldown_minutes"] - diff)
            return False, "صبر کن " + str(remaining) + " دقیقه"
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

def get_all_user_ids():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT user_id FROM subscriptions WHERE status='active'").fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_all_subscriber_ids():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT user_id FROM subscriptions").fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_stats():
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active'").fetchone()[0]
    expired = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='expired'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM pending_payments WHERE status='pending'").fetchone()[0]
    total_analyses = conn.execute("SELECT SUM(usage_count) FROM subscriptions").fetchone()[0] or 0
    revenue = conn.execute("""
        SELECT SUM(p.price) FROM pending_payments pp
        JOIN subscriptions s ON pp.user_id = s.user_id
        WHERE pp.status='approved'
    """).fetchone()[0] or 0
    conn.close()
    return {
        "total": total, "active": active, "expired": expired,
        "pending": pending, "total_analyses": total_analyses, "revenue": revenue
    }

def revoke_sub(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE subscriptions SET status='revoked' WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()

def extend_sub(user_id: int, days: int):
    conn = sqlite3.connect(str(DB_PATH))
    sub = conn.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    if sub and sub[0]:
        exp = datetime.fromisoformat(sub[0])
        if exp < datetime.now():
            exp = datetime.now()
        new_exp = (exp + timedelta(days=days)).isoformat()
    else:
        new_exp = (datetime.now() + timedelta(days=days)).isoformat()
    conn.execute("UPDATE subscriptions SET expires_at=?, status='active' WHERE user_id=?", (new_exp, user_id))
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
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}})
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        "temperature": 0.15, "max_tokens": 1500
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(API_URL, headers=headers, json=payload)
        if r.status_code != 200:
            raise Exception("API error " + str(r.status_code))
        return r.json()["choices"][0]["message"]["content"]

# ═══════════════════════ GATE ═══════════════════════

def gate(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        ok, reason = can_analyze(uid)
        if not ok:
            kb = InlineKeyboardMarkup([
                [btn_main("خرید اشتراک", "show_plans")],
                [btn_action("پشتیبانی", "support")]
            ])
            await update.message.reply_text(
                "╔══════════════════════╗\n"
                "  🔐 **شهباز کور**\n"
                "  " + reason + "\n"
                "╚══════════════════════╝\n\n"
                "برای خرید اشتراک دکمه زیر را بزنید:",
                parse_mode="Markdown", reply_markup=kb
            )
            return
        return await func(update, context)
    return wrapper

# ═══════════════════════ MENUS ═══════════════════════

def main_menu_kb(uid):
    sub = get_sub(uid)
    if sub and sub["status"] == "active":
        return InlineKeyboardMarkup([
            [btn_main("📸 تحلیل مسابقه", "send_photo_hint")],
            [btn_main("📊 آمار و اطلاعات من", "my_stats")],
            [btn_main("📦 تمدید اشتراک", "show_plans")],
            [btn_action("💬 پشتیبانی", "support")],
        ])
    return InlineKeyboardMarkup([
        [btn_main("📦 مشاهده پلن‌ها", "show_plans")],
        [btn_action("💬 پشتیبانی", "support")],
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup([
        [btn_main("📊 آمار کلی", "admin_stats")],
        [btn_main("💰 رسیدهای در انتظار", "admin_pending")],
        [btn_main("👥 لیست کاربران", "admin_users")],
        [btn_main("🔍 جستجوی کاربر", "admin_search")],
        [btn_action("📢 ارسال همگانی", "admin_broadcast")],
    ])

# ═══════════════════════ START ═══════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"
    sub = get_sub(uid)

    if sub and sub["status"] == "active":
        p = PLANS.get(sub["plan"], {})
        expires = sub["expires_at"][:10] if sub["expires_at"] else "—"
        usage = sub["usage_count"]
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        cd = sub["cooldown_minutes"]
        cd_text = "هر " + str(cd) + " دقیقه" if cd > 0 else "بدون محدودیت"
        remaining = "∞"
        if sub["expires_at"]:
            exp = datetime.fromisoformat(sub["expires_at"])
            diff = exp - datetime.now()
            remaining = str(diff.days) + " روز"
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ⚔ **شهباز کور** ⚔\n"
            "╚══════════════════════╝\n\n"
            "سلام **" + name + "** 👋\n\n"
            "┌─────────────────────┐\n"
            "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "│ 📊 تحلیل‌ها: **" + str(usage) + "/" + str(max_u) + "**\n"
            "│ ⏰ کول‌داون: " + cd_text + "\n"
            "│ 📅 باقی‌مانده: " + remaining + "\n"
            "└─────────────────────┘\n\n"
            "برای تحلیل، عکس اسکرین‌شات بفرستید\n"
            "یا از دکمه‌های زیر استفاده کنید:",
            parse_mode="Markdown", reply_markup=main_menu_kb(uid)
        )
    else:
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ⚔ **شهباز کور** ⚔\n"
            "╚══════════════════════╝\n\n"
            "🎮 **سیستم تحلیل شرط‌بندی MK**\n"
            "مخصوص سیستم مارتینگل\n\n"
            "🔐 **نیاز به اشتراک**\n\n"
            "برای شروع دکمه زیر را بزنید:",
            parse_mode="Markdown", reply_markup=main_menu_kb(uid)
        )

# ═══════════════════════ ADMIN START ═══════════════════════

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    stats = get_stats()
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "  🛡 **پنل مدیریت** 🛡\n"
        "╚══════════════════════╝\n\n"
        "┌─────────────────────┐\n"
        "│ 👥 کل کاربران: **" + str(stats['total']) + "**\n"
        "│ ✅ فعال: **" + str(stats['active']) + "**\n"
        "│ ❌ منقضی: **" + str(stats['expired']) + "**\n"
        "│ ⏳ در انتظار: **" + str(stats['pending']) + "**\n"
        "│ 📊 کل تحلیل‌ها: **" + str(stats['total_analyses']) + "**\n"
        "│ 💰 درآمد: **$" + str(stats['revenue']) + "**\n"
        "└─────────────────────┘\n\n"
        "عملیات مورد نظر را انتخاب کنید:",
        parse_mode="Markdown", reply_markup=admin_menu_kb()
    )

# ═══════════════════════ CALLBACKS ═══════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    uid = q.from_user.id
    name = q.from_user.first_name or "کاربر"

    # ─── MAIN MENU ───
    if d == "main_menu":
        sub = get_sub(uid)
        if sub and sub["status"] == "active":
            p = PLANS.get(sub["plan"], {})
            expires = sub["expires_at"][:10] if sub["expires_at"] else "—"
            usage = sub["usage_count"]
            max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
            cd = sub["cooldown_minutes"]
            cd_text = "هر " + str(cd) + " دقیقه" if cd > 0 else "بدون محدودیت"
            remaining = "∞"
            if sub["expires_at"]:
                exp = datetime.fromisoformat(sub["expires_at"])
                diff = exp - datetime.now()
                remaining = str(diff.days) + " روز"
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⚔ **شهباز کور** ⚔\n"
                "╚══════════════════════╝\n\n"
                "سلام **" + name + "** 👋\n\n"
                "┌─────────────────────┐\n"
                "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
                "│ 📊 تحلیل‌ها: **" + str(usage) + "/" + str(max_u) + "**\n"
                "│ ⏰ کول‌داون: " + cd_text + "\n"
                "│ 📅 باقی‌مانده: " + remaining + "\n"
                "└─────────────────────┘\n\n"
                "برای تحلیل، عکس اسکرین‌شات بفرستید\n"
                "یا از دکمه‌های زیر استفاده کنید:",
                parse_mode="Markdown", reply_markup=main_menu_kb(uid)
            )
        else:
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⚔ **شهباز کور** ⚔\n"
                "╚══════════════════════╝\n\n"
                "🎮 **سیستم تحلیل شرط‌بندی MK**\n"
                "مخصوص سیستم مارتینگل\n\n"
                "🔐 **نیاز به اشتراک**\n\n"
                "برای شروع دکمه زیر را بزنید:",
                parse_mode="Markdown", reply_markup=main_menu_kb(uid)
            )

    # ─── PLANS ───
    elif d == "show_plans":
        kb = InlineKeyboardMarkup([
            [btn("⚡ ۱ روزه — $5", "plan_day")],
            [btn("📦 ۱ ماهه پایه — $30", "plan_month_basic")],
            [btn("👑 ۱ ماهه نامحدود — $60", "plan_month_unlimited")],
            [btn("📅 ۱ ساله پایه — $250", "plan_year_basic")],
            [btn("💎 ۱ ساله نامحدود — $350", "plan_year_unlimited")],
            [btn_back("main_menu")],
        ])
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "     📦 **پلن‌های اشتراک**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────────┐\n"
            "│ ⚡ **۱ روزه — $5**\n"
            "│ هر ۳۰ دقیقه یک تحلیل\n"
            "├─────────────────────────┤\n"
            "│ 📦 **۱ ماهه پایه — $30**\n"
            "│ ۵۰۰ تحلیل — هر ۱۵ دقیقه\n"
            "├─────────────────────────┤\n"
            "│ 👑 **۱ ماهه نامحدود — $60**\n"
            "│ بدون محدودیت\n"
            "├─────────────────────────┤\n"
            "│ 📅 **۱ ساله پایه — $250**\n"
            "│ ۵۰۰۰ تحلیل — هر ۲۵ دقیقه\n"
            "├─────────────────────────┤\n"
            "│ 💎 **۱ ساله نامحدود — $350**\n"
            "│ بدون محدودیت\n"
            "└─────────────────────────┘\n\n"
            "💰 پرداخت: **USDT (TRC20)**\n\n"
            "پلن مورد نظر را انتخاب کنید:",
            parse_mode="Markdown", reply_markup=kb
        )

    # ─── PLAN SELECTION ───
    elif d.startswith("plan_"):
        plan = d.replace("plan_", "")
        if plan not in PLANS:
            return await q.edit_message_text("پلن نامعتبر.")
        p = PLANS[plan]
        kb = InlineKeyboardMarkup([
            [btn_action("📋 کپی آدرس", "copy_addr")],
            [btn_action("📝 ارسال رسید", "how_receipt")],
            [btn_back("show_plans")],
        ])
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  💰 **پرداخت — " + p['name'] + "**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 💵 مبلغ: **$" + str(p['price']) + "**\n"
            "│ 📦 پلن: **" + p['name'] + "**\n"
            "│ 📝 " + p['desc'] + "\n"
            "└─────────────────────┘\n\n"
            "🔹 **آدرس USDT (TRC20):**\n"
            "`" + PAYMENT_ADDRESS + "`\n\n"
            "⚠️ فقط **USDT TRC20** بفرستید.\n\n"
            "بعد از پرداخت، رسید (TX Hash) را\n"
            "از طریق دکمه **ارسال رسید** بفرستید.",
            parse_mode="Markdown", reply_markup=kb
        )

    # ─── HOW TO SEND RECEIPT ───
    elif d == "how_receipt":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📝 **ارسال رسید پرداخت**\n"
            "╚══════════════════════╝\n\n"
            "مرحله ۱: پرداخت را انجام دهید\n"
            "مرحله ۲: TX Hash را کپی کنید\n"
            "مرحله ۳: آن را اینجا بفرستید\n\n"
            "فرمت ارسال:\n"
            "`TX_HASH_HERE`\n\n"
            "⚠️ TX Hash معمولاً ۶۴ کاراکتر است\n"
            "و با حروف و اعداد شروع می‌شود.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("main_menu")]
            ])
        )

    # ─── PHOTO HINT ───
    elif d == "send_photo_hint":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📸 **تحلیل مسابقه**\n"
            "╚══════════════════════╝\n\n"
            "برای تحلیل کافیست:\n\n"
            "۱. اسکرین‌شات مسابقه را بفرستید\n"
            "۲. منتظر تحلیل شوید\n\n"
            "⚡ تحلیل خودکار و فوری\n"
            "🎯 مخصوص سیستم مارتینگل\n\n"
            "عکس را همینجا ارسال کنید 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("main_menu")]
            ])
        )

    # ─── MY STATS ───
    elif d == "my_stats":
        sub = get_sub(uid)
        if not sub or sub["status"] != "active":
            return await q.edit_message_text(
                "اشتراک فعال ندارید.\n\n"
                "برای خرید اشتراک دکمه زیر را بزنید:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [btn_main("📦 مشاهده پلن‌ها", "show_plans")]
                ])
            )
        p = PLANS.get(sub["plan"], {})
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        remaining = "∞"
        if sub["expires_at"]:
            exp = datetime.fromisoformat(sub["expires_at"])
            diff = exp - datetime.now()
            remaining = str(diff.days) + " روز"
        cd = sub["cooldown_minutes"]
        cd_text = "هر " + str(cd) + " دقیقه" if cd > 0 else "بدون محدودیت"
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📊 **آمار شما**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 👤 **" + name + "**\n"
            "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "│ 📊 تحلیل‌ها: **" + str(sub['usage_count']) + "/" + str(max_u) + "**\n"
            "│ ⏰ کول‌داون: " + cd_text + "\n"
            "│ 📅 باقی‌مانده: " + remaining + "\n"
            "│ 💎 وضعیت: ✅ فعال\n"
            "└─────────────────────┘",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_action("📸 تحلیل مسابقه", "send_photo_hint")],
                [btn_main("📦 تمدید اشتراک", "show_plans")],
                [btn_back("main_menu")],
            ])
        )

    # ─── SUPPORT ───
    elif d == "support":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  💬 **پشتیبانی**\n"
            "╚══════════════════════╝\n\n"
            "برای ارتباط با ادمین:\n"
            "پیام مستقیم بفرستید.\n"
            "یا از طریق دکمه زیر:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_link("💬 ارسال پیام به ادمین", "https://t.me/admin")],
                [btn_back("main_menu")],
            ])
        )

    # ─── COPY ADDRESS ───
    elif d == "copy_addr":
        await q.answer("📋 آدرس کپی شد:\n" + PAYMENT_ADDRESS, show_alert=True)

    # ─── ADMIN: APPROVE ───
    elif d.startswith("approve_"):
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        try:
            target = int(d.split("_")[1])
        except:
            return await q.answer("❌ خطا در پردازش", show_alert=True)
        plan = "month_basic"
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute("SELECT plan FROM pending_payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
        if row and row[0] in PLANS:
            plan = row[0]
        conn.execute("UPDATE pending_payments SET status='approved' WHERE user_id=? AND status='pending'", (target,))
        conn.commit(); conn.close()
        activate_sub(target, "", plan)
        p = PLANS[plan]
        try:
            cd_text = "بدون محدودیت" if p['cooldown'] == 0 else "هر " + str(p['cooldown']) + " دقیقه"
            max_text = "∞" if p['max'] <= 0 else str(p['max'])
            await context.bot.send_message(target,
                "╔══════════════════════╗\n"
                "  ✅ **اشتراک فعال شد!**\n"
                "╚══════════════════════╝\n\n"
                "┌─────────────────────┐\n"
                "│ 📦 پلن: " + p['name'] + "\n"
                "│ 📊 محدودیت: " + max_text + " تحلیل\n"
                "│ ⏰ کول‌داون: " + cd_text + "\n"
                "│ 📅 مدت: " + str(p['days']) + " روز\n"
                "└─────────────────────┘\n\n"
                "📸 عکس بفرست برای تحلیل!",
                parse_mode="Markdown", reply_markup=main_menu_kb(target)
            )
            log.info("Subscription activated for user %d, plan: %s", target, plan)
        except Exception as e:
            log.error("Failed to notify user %d: %s", target, str(e))
        await q.edit_message_text("✅ تایید شد — اشتراک " + plan + " برای " + str(target) + " فعال شد.")

    # ─── ADMIN: REJECT ───
    elif d.startswith("reject_"):
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        try:
            target = int(d.split("_")[1])
        except:
            return await q.answer("❌ خطا در پردازش", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE pending_payments SET status='rejected' WHERE user_id=? AND status='pending'", (target,))
        conn.commit(); conn.close()
        try:
            await context.bot.send_message(target,
                "╔══════════════════════╗\n"
                "  ❌ **رسید تایید نشد**\n"
                "╚══════════════════════╝\n\n"
                "رسید شما بررسی شد و تایید نشد.\n"
                "لطفاً با ادمین تماس بگیرید.",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [btn_action("💬 پشتیبانی", "support")]
                ])
            )
        except Exception as e:
            log.error("Failed to notify rejection to user %d: %s", target, str(e))
        await q.edit_message_text("❌ رد شد.")

    # ─── ADMIN: STATS ───
    elif d == "admin_stats":
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        stats = get_stats()
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📊 **آمار کلی سیستم**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 👥 کل کاربران: **" + str(stats['total']) + "**\n"
            "│ ✅ فعال: **" + str(stats['active']) + "**\n"
            "│ ❌ منقضی: **" + str(stats['expired']) + "**\n"
            "│ ⏳ در انتظار تایید: **" + str(stats['pending']) + "**\n"
            "│ 📊 کل تحلیل‌ها: **" + str(stats['total_analyses']) + "**\n"
            "│ 💰 درآمد تایید شده: **$" + str(stats['revenue']) + "**\n"
            "└─────────────────────┘",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("admin_panel")]
            ])
        )

    # ─── ADMIN: PENDING ───
    elif d == "admin_pending":
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT id, user_id, username, plan, tx_hash, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text(
                "📭 رسید در انتظار نیست.",
                reply_markup=InlineKeyboardMarkup([[btn_back("admin_panel")]])
            )
        msg = "💰 **رسیدهای در انتظار**\n\n"
        for r in rows:
            plan_name = PLANS.get(r[3], {}).get('name', r[3])
            msg += "🆔 `" + str(r[1]) + "` — @" + (r[2] or "—") + "\n"
            msg += "📦 " + plan_name + " — `" + r[4][:15] + "...`\n"
            msg += "🕐 " + r[5][:16] + "\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [btn_back("admin_panel")]
        ]))

    # ─── ADMIN: USERS ───
    elif d == "admin_users":
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, username, plan, status, usage_count, expires_at FROM subscriptions ORDER BY status='active' DESC, expires_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text(
                "📭 کاربری نیست.",
                reply_markup=InlineKeyboardMarkup([[btn_back("admin_panel")]])
            )
        msg = "👥 **لیست کاربران**\n\n"
        for r in rows:
            status_emoji = "✅" if r[3] == "active" else "❌"
            plan_name = PLANS.get(r[2], {}).get('name', r[2])
            expires = r[5][:10] if r[5] else "—"
            msg += status_emoji + " `" + str(r[0]) + "` — @" + (r[1] or "—") + "\n"
            msg += "  📦 " + plan_name + " | 📊 " + str(r[4]) + " | 📅 " + expires + "\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
            [btn_back("admin_panel")]
        ]))

    # ─── ADMIN: SEARCH ───
    elif d == "admin_search":
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        await q.edit_message_text(
            "🔍 **جستجوی کاربر**\n\n"
            "آیدی عددی کاربر را بفرستید:\n"
            "`/search USER_ID`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("admin_panel")]
            ])
        )

    # ─── ADMIN: BROADCAST ───
    elif d == "admin_broadcast":
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        await q.edit_message_text(
            "📢 **ارسال همگانی**\n\n"
            "متن پیام را بفرستید:\n"
            "`/broadcast متن پیام`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("admin_panel")]
            ])
        )

# ═══════════════════════ TEXT HANDLER ═══════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # ─── ADMIN: SEARCH ───
    if is_admin(uid) and text.startswith("/search ") and len(context.args) == 1:
        try:
            target = int(context.args[0])
        except:
            return await update.message.reply_text("آیدی نامعتبر.")
        sub = get_sub(target)
        if not sub:
            return await update.message.reply_text("کاربر یافت نشد.")
        p = PLANS.get(sub["plan"], {})
        status_emoji = "✅" if sub["status"] == "active" else "❌"
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        await update.message.reply_text(
            "🔍 **اطلاعات کاربر**\n\n"
            "┌─────────────────────┐\n"
            "│ 🆔 آیدی: `" + str(target) + "`\n"
            "│ 👤 یوزرنیم: @" + (sub["username"] or "—") + "\n"
            "│ " + status_emoji + " وضعیت: " + sub["status"] + "\n"
            "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "│ 📊 تحلیل‌ها: " + str(sub['usage_count']) + "/" + str(max_u) + "\n"
            "│ 📅 انقضا: " + (sub['expires_at'][:10] if sub['expires_at'] else "—") + "\n"
            "└─────────────────────┘\n\n"
            "عملیات:\n"
            "`/act " + str(target) + " day` — فعال‌سازی ۱ روزه\n"
            "`/act " + str(target) + " month_basic` — فعال‌سازی ماهانه پایه\n"
            "`/act " + str(target) + " month_unlimited` — فعال‌سازی ماهانه نامحدود\n"
            "`/act " + str(target) + " year_basic` — فعال‌سازی سالانه پایه\n"
            "`/act " + str(target) + " year_unlimited` — فعال‌سازی سالانه نامحدود\n"
            "`/revoke " + str(target) + "` — لغو اشتراک\n"
            "`/extend " + str(target) + " 7` — افزودن ۷ روز",
            parse_mode="Markdown"
        )
        return

    # ─── ADMIN: REVOKE ───
    if is_admin(uid) and text.startswith("/revoke ") and len(context.args) == 1:
        try:
            target = int(context.args[0])
        except:
            return await update.message.reply_text("آیدی نامعتبر.")
        revoke_sub(target)
        try:
            await context.bot.send_message(target,
                "╔══════════════════════╗\n"
                "  ❌ **اشتراک لغو شد**\n"
                "╚══════════════════════╝\n\n"
                "اشتراک شما توسط ادمین لغو شد.",
                parse_mode="Markdown"
            )
        except: pass
        await update.message.reply_text("✅ اشتراک " + str(target) + " لغو شد.")
        return

    # ─── ADMIN: EXTEND ───
    if is_admin(uid) and text.startswith("/extend ") and len(context.args) == 2:
        try:
            target = int(context.args[0])
            days = int(context.args[1])
        except:
            return await update.message.reply_text("فرمت: `/extend USER_ID DAYS`", parse_mode="Markdown")
        extend_sub(target, days)
        try:
            await context.bot.send_message(target,
                "╔══════════════════════╗\n"
                "  🔄 **اشتراک تمدید شد**\n"
                "╚══════════════════════╝\n\n"
                "اشتراک شما " + str(days) + " روز تمدید شد.",
                parse_mode="Markdown"
            )
        except: pass
        await update.message.reply_text("✅ اشتراک " + str(target) + " به مدت " + str(days) + " روز تمدید شد.")
        return

    # ─── ADMIN: BROADCAST ───
    if is_admin(uid) and text.startswith("/broadcast ") and len(context.args) >= 1:
        msg_text = " ".join(context.args)
        user_ids = get_all_user_ids()
        sent = 0
        failed = 0
        for user_id in user_ids:
            try:
                await context.bot.send_message(user_id,
                    "📢 **پیام ادمین**\n\n" + msg_text,
                    parse_mode="Markdown"
                )
                sent += 1
            except:
                failed += 1
        await update.message.reply_text(
            "✅ ارسال شد: " + str(sent) + "\n❌ ناموفق: " + str(failed)
        )
        return

    # ─── CHECK FOR TX HASH ───
    if len(text) >= 10 and not text.startswith("/"):
        # Store pending payment
        conn = sqlite3.connect(str(DB_PATH))
        existing = conn.execute("SELECT id FROM pending_payments WHERE user_id=? AND status='pending'", (uid,)).fetchone()
        if existing:
            conn.execute("UPDATE pending_payments SET tx_hash=?, created_at=datetime('now') WHERE id=?", (text, existing[0]))
        else:
            conn.execute("INSERT INTO pending_payments (user_id, username, plan, tx_hash) VALUES (?, ?, ?, ?)",
                         (uid, update.effective_user.username or "", "unknown", text))
        conn.commit(); conn.close()

        # Notify admin
        if ADMIN_ID:
            try:
                name = update.effective_user.first_name or "کاربر"
                uname = update.effective_user.username or "—"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تایید", callback_data="approve_" + str(uid)),
                     InlineKeyboardButton("❌ رد", callback_data="reject_" + str(uid))]
                ])
                await context.bot.send_message(ADMIN_ID,
                    "💰 **رسید پرداخت جدید**\n\n"
                    "┌─────────────────────┐\n"
                    "│ 👤 " + name + " (@" + uname + ")\n"
                    "│ 🆔 `" + str(uid) + "`\n"
                    "│ 📝 TX: `" + text[:30] + "...`\n"
                    "└─────────────────────┘\n\n"
                    "تایید یا رد کن:",
                    parse_mode="Markdown", reply_markup=kb
                )
                log.info("Receipt sent to admin from user %d", uid)
            except Exception as e:
                log.error("Failed to send to admin: %s", str(e))

        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ✅ **رسید ثبت شد**\n"
            "╚══════════════════════╝\n\n"
            "رسید شما دریافت شد.\n"
            "ادمین در حال بررسی است.\n"
            "بعد از تایید، اشتراک فعال می‌شود.\n\n"
            "⏳ لطفاً صبر کنید...",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("main_menu")]
            ])
        )
        return

# ═══════════════════════ PHOTO HANDLER ═══════════════════════

@gate
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ **شهباز در حال تحلیل...**\n\n"
        "لطفاً صبر کنید ⚔️",
        parse_mode="Markdown"
    )
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        raw = io.BytesIO()
        await file.download_to_memory(raw)
        raw = raw.getvalue()
        if len(raw) > MAX_IMAGE_SIZE:
            return await msg.edit_text(
                "❌ حجم تصویر زیاد است.\nحداکثر ۸MB.",
                reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
            )
        compressed = compress_image(raw)
        b64 = base64.b64encode(compressed).decode()
        analysis = await call_ai(b64)
        record_analysis(update.effective_user.id)
        await msg.delete()
        for p in split_msg(analysis):
            await update.message.reply_text(p, parse_mode="Markdown")
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━\n"
            "📸 تحلیل بعدی؟ عکس بفرستید 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_back("main_menu")]
            ])
        )
    except Exception as e:
        await msg.edit_text(
            "❌ خطا در تحلیل:\n`" + str(e)[:200] + "`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_action("💬 پشتیبانی", "support")]
            ])
        )

# ═══════════════════════ ADMIN COMMANDS ═══════════════════════

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
        msg += "🆔 `" + str(r[1]) + "` — @" + (r[2] or "—") + " — `" + r[4][:15] + "...` — " + r[5][:16] + "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def activate_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    if len(context.args) < 2:
        return await update.message.reply_text(
            "فرمت: `/act USER_ID PLAN`\n\n"
            "Plans: day, month_basic, month_unlimited, year_basic, year_unlimited",
            parse_mode="Markdown"
        )
    try:
        target_uid = int(context.args[0])
        plan = context.args[1]
    except:
        return await update.message.reply_text("فرمت: `/act USER_ID PLAN`", parse_mode="Markdown")
    if plan not in PLANS:
        return await update.message.reply_text("پلن نامعتبر. یکی از: " + ", ".join(PLANS.keys()))
    activate_sub(target_uid, "", plan)
    p = PLANS[plan]
    try:
        cd_text = "بدون محدودیت" if p['cooldown'] == 0 else "هر " + str(p['cooldown']) + " دقیقه"
        max_text = "∞" if p['max'] <= 0 else str(p['max'])
        await context.bot.send_message(target_uid,
            "╔══════════════════════╗\n"
            "  ✅ **اشتراک فعال شد!**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 📦 پلن: " + p['name'] + "\n"
            "│ 📊 محدودیت: " + max_text + " تحلیل\n"
            "│ ⏰ کول‌داون: " + cd_text + "\n"
            "│ 📅 مدت: " + str(p['days']) + " روز\n"
            "└─────────────────────┘\n\n"
            "📸 عکس بفرست برای تحلیل!",
            parse_mode="Markdown", reply_markup=main_menu_kb(target_uid)
        )
    except: pass
    await update.message.reply_text("✅ اشتراک " + plan + " برای " + str(target_uid) + " فعال شد.")

# ═══════════════════════ MAIN ═══════════════════════

def main():
    if not TOKEN:   log.error("TOKEN empty"); return
    if not API_KEY: log.error("API_KEY empty"); return
    if ADMIN_ID == 0: log.warning("ADMIN_ID not set")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_start))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("act", activate_manual))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("👑 Shahbaz Core VIP Bot running")
    app.run_polling()

if __name__ == "__main__":
    main()
