import os, io, base64, logging, sqlite3, json
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
API_URL = os.getenv("API_URL", "https://api.unli.dev/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "auto")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_ADDRESS = os.getenv("PAYMENT_ADDRESS", "TXYZxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
BOT_NAME = "انالیزور مورتال"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path("data.db")

PLANS = {
    "day": {"name": "۱ روزه", "price": 5, "days": 1, "max": -1, "cooldown": 30, "desc": "تستی — هر ۳۰ دقیقه"},
    "month_basic": {"name": "۱ ماهه پایه", "price": 30, "days": 30, "max": 500, "cooldown": 15, "desc": "۵۰۰ تحلیل — هر ۱۵ دقیقه"},
    "month_unlimited": {"name": "۱ ماهه نامحدود", "price": 60, "days": 30, "max": -1, "cooldown": 0, "desc": "نامحدود"},
    "year_basic": {"name": "۱ ساله پایه", "price": 250, "days": 365, "max": 5000, "cooldown": 25, "desc": "۵۰۰۰ تحلیل — هر ۲۵ دقیقه"},
    "year_unlimited": {"name": "۱ ساله نامحدود", "price": 350, "days": 365, "max": -1, "cooldown": 0, "desc": "نامحدود"},
}

FREE_COOLDOWN_HOURS = 72  # 3 days

# ═══════════════════════ BUTTONS ═══════════════════════

def btn(text, data):
    return InlineKeyboardButton(f"✧ {text}", callback_data=data)

def btn_main(text, data):
    return InlineKeyboardButton(f"◈ {text}", callback_data=data)

def btn_back(data="main_menu"):
    return InlineKeyboardButton("◂ بازگشت", callback_data=data)

def btn_action(text, data):
    return InlineKeyboardButton(f"▸ {text}", callback_data=data)

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
            last_analysis_at TEXT DEFAULT '',
            discount_percent INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS free_users (
            user_id INTEGER PRIMARY KEY,
            last_free_at TEXT DEFAULT '',
            free_count INTEGER DEFAULT 0
        );
    """)
    conn.execute("DELETE FROM pending_payments WHERE tx_hash IN ('awaiting_payment', '') AND status='pending'")
    conn.commit(); conn.close()

def backup_db():
    try:
        with open(DB_PATH, "rb") as f:
            return f.read()
    except:
        return None

def restore_db(data: bytes):
    try:
        with open(DB_PATH, "wb") as f:
            f.write(data)
        return True
    except:
        return False

def export_subs_json():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM subscriptions").fetchall()
    conn.close()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)

def import_subs_json(data: str):
    try:
        subs = json.loads(data)
        conn = sqlite3.connect(str(DB_PATH))
        for s in subs:
            conn.execute("""
                INSERT INTO subscriptions (user_id, username, plan, status, tx_hash, activated_at,
                    expires_at, usage_count, max_usage, cooldown_minutes, last_analysis_at, discount_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username, plan=excluded.plan, status=excluded.status,
                    tx_hash=excluded.tx_hash, activated_at=excluded.activated_at,
                    expires_at=excluded.expires_at, usage_count=excluded.usage_count,
                    max_usage=excluded.max_usage, cooldown_minutes=excluded.cooldown_minutes,
                    last_analysis_at=excluded.last_analysis_at, discount_percent=excluded.discount_percent
            """, (s['user_id'], s.get('username',''), s.get('plan',''), s.get('status','pending'),
                  s.get('tx_hash',''), s.get('activated_at',''), s.get('expires_at',''),
                  s.get('usage_count',0), s.get('max_usage',0), s.get('cooldown_minutes',0),
                  s.get('last_analysis_at',''), s.get('discount_percent',0)))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        log.error("Import failed: %s", str(e))
        return False

def get_sub(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(r) if r else None

def can_analyze(user_id: int) -> tuple:
    # Admin always can
    if is_admin(user_id):
        return True, "ok"
    # Check subscription
    sub = get_sub(user_id)
    if sub and sub["status"] == "active":
        if sub["expires_at"] and datetime.fromisoformat(sub["expires_at"]) < datetime.now():
            return False, "اشتراک شما منقضی شده"
        if sub["max_usage"] > 0 and sub["usage_count"] >= sub["max_usage"]:
            return False, "سقف تحلیل تمام شده"
        if sub["cooldown_minutes"] > 0 and sub["last_analysis_at"]:
            last = datetime.fromisoformat(sub["last_analysis_at"])
            diff = (datetime.now() - last).total_seconds() / 60
            if diff < sub["cooldown_minutes"]:
                return False, "صبر کن " + str(int(sub["cooldown_minutes"] - diff)) + " دقیقه"
        return True, "ok"
    # Check free tier
    if can_use_free(user_id):
        return True, "free"
    return False, "اشتراک فعال ندارید"

def record_analysis(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE subscriptions SET usage_count=usage_count+1, last_analysis_at=? WHERE user_id=?",
                 (datetime.now().isoformat(), user_id))
    conn.commit(); conn.close()

def record_free_analysis(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO free_users (user_id, last_free_at, free_count) VALUES (?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET last_free_at=?, free_count=free_count+1
    """, (user_id, datetime.now().isoformat(), datetime.now().isoformat()))
    conn.commit(); conn.close()

def can_use_free(user_id: int) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM free_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not r:
        return True
    r = dict(r)
    if not r["last_free_at"]:
        return True
    last = datetime.fromisoformat(r["last_free_at"])
    diff_hours = (datetime.now() - last).total_seconds() / 3600
    return diff_hours >= FREE_COOLDOWN_HOURS

def get_free_remaining(user_id: int) -> str:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM free_users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if not r:
        return "۳ روز"
    r = dict(r)
    if not r["last_free_at"]:
        return "۳ روز"
    last = datetime.fromisoformat(r["last_free_at"])
    diff_hours = (datetime.now() - last).total_seconds() / 3600
    remaining = FREE_COOLDOWN_HOURS - diff_hours
    if remaining <= 0:
        return "آماده"
    days = int(remaining // 24)
    hours = int(remaining % 24)
    if days > 0:
        return str(days) + " روز و " + str(hours) + " ساعت"
    return str(hours) + " ساعت"

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

def revoke_sub(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE subscriptions SET status='revoked' WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()

def extend_sub(user_id: int, days: int):
    conn = sqlite3.connect(str(DB_PATH))
    sub = conn.execute("SELECT expires_at FROM subscriptions WHERE user_id=?", (user_id,)).fetchone()
    if sub and sub[0]:
        exp = datetime.fromisoformat(sub[0])
        if exp < datetime.now(): exp = datetime.now()
        new_exp = (exp + timedelta(days=days)).isoformat()
    else:
        new_exp = (datetime.now() + timedelta(days=days)).isoformat()
    conn.execute("UPDATE subscriptions SET expires_at=?, status='active' WHERE user_id=?", (new_exp, user_id))
    conn.commit(); conn.close()

def set_discount(user_id: int, percent: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE subscriptions SET discount_percent=? WHERE user_id=?", (percent, user_id))
    conn.commit(); conn.close()

def get_all_user_ids():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT user_id FROM subscriptions WHERE status='active'").fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_stats():
    conn = sqlite3.connect(str(DB_PATH))
    total = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active'").fetchone()[0]
    expired = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='expired'").fetchone()[0]
    revoked = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='revoked'").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM pending_payments WHERE status='pending'").fetchone()[0]
    total_analyses = conn.execute("SELECT SUM(usage_count) FROM subscriptions").fetchone()[0] or 0
    free_users = conn.execute("SELECT COUNT(*) FROM free_users").fetchone()[0]
    conn.close()
    return {"total": total, "active": active, "expired": expired, "revoked": revoked,
            "pending": pending, "total_analyses": total_analyses, "free_users": free_users}

# ═══════════════════════ SYSTEM PROMPT ═══════════════════════

SYSTEM_PROMPT = (
    "تو 'تحلیلگر ارشد Mortal Kombat' هستی — متخصص ۲۰ ساله MK.\n"
    "فقط فارسی خروجی بده.\n\n"

    "دانش تو:\n"
    "- تمام کاراکترها و move set ها (MK11, MK1, MK1 Mobile)\n"
    "- frame data: startup, recovery, plus/minus on-block\n"
    "- combo damage: کومبوهای بهینه\n"
    "- matchup: چه کاراکتری در برابر چه کاراکتری برتره\n"
    "- tournament meta و tier list\n"
    "- betting markets: moneyline, props, round duration\n\n"

    "قوانین:\n"
    "1. عکس رو دقیق بررسی کن — کاراکترها، ضرایب، نوع شرط\n"
    "2. تحلیل matchup + frame advantage + combo potential\n"
    "3. اولویت props (زیر/بالای ثانیه) بیشتر از moneyline\n"
    "4. فقط پیشنهاد با ضریب >= ۱.۸\n"
    "5. احتمال حداکثر ۶۵٪\n"
    "6. اگر مناسب نیست: 'وضعیت: خطرناک. وارد نشو.'\n\n"

    "فرمت:\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "⚔ [کاراکتر۱] vs [کاراکتر۲]\n\n"
    "📊 تحلیل:\n"
    "▸ [کاراکتر۱]: [سبک + قوت + ضعف]\n"
    "▸ [کاراکتر۲]: [سبک + قوت + ضعف]\n"
    "▸ مقایسه: [برتری در رنج + frame advantage]\n"
    "▸ کومبو: [damage هر کاراکتر]\n\n"
    "🎯 پیشنهاد مارتینگل:\n"
    "▸ [شرط] — ضریب [X.XX]\n"
    "▸ احتمال: [XX–XX٪]\n"
    "▸ دلیل: [منطقی با اشاره به frame data]\n\n"
    "⚠️ هشدار: [نکات ریسک]\n"
    "━━━━━━━━━━━━━━━━━━\n\n"

    "نکات:\n"
    "- راند اول محافظه‌کارانه → props زمان بهترینه\n"
    "- ضریب < ۱.۵ → ارزش نداره\n"
    "- rushdown vs zoner → round 1 کشیده\n"
    "- mirror match → وارد نشو\n"
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
    headers = {"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json"}
    content = [{"type": "text", "text": prompt or "اسکرین‌شات مسابقه Mortal Kombat رو تحلیل کن."}]
    if image_b64:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}})
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
        "temperature": 0.15, "max_tokens": 2000
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(API_URL, headers=headers, json=payload)
        if r.status_code != 200:
            body = r.text[:200] if r.text else ""
            if "Unavailable" in body or r.status_code == 503:
                raise Exception("سرویس AI موقتاً در دسترس نیست.")
            raise Exception("خطای API: " + str(r.status_code))
        result = r.json()
        if "choices" not in result or not result["choices"]:
            raise Exception("پاسخی از API دریافت نشد")
        return result["choices"][0]["message"]["content"]

# ═══════════════════════ GATE ═══════════════════════

def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID

def gate(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if is_admin(uid):
            return await func(update, context)
        ok, reason = can_analyze(uid)
        if not ok:
            kb = InlineKeyboardMarkup([
                [btn_main("خرید اشتراک", "show_plans")],
                [btn_main("🎯 تحلیل رایگان", "free_analysis")],
                [btn_action("پشتیبانی", "support")]
            ])
            await update.message.reply_text(
                "╔══════════════════════╗\n"
                "  🔐 **" + BOT_NAME + "**\n"
                "  " + reason + "\n"
                "╚══════════════════════╝\n\n"
                "برای خرید یا تحلیل رایگان:",
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
            [btn_main("📊 آمار من", "my_stats")],
            [btn_main("📦 تمدید اشتراک", "show_plans")],
            [btn_action("💬 پشتیبانی", "support")],
        ])
    # Not subscribed - show free option
    free_ready = can_use_free(uid)
    free_txt = "🎯 تحلیل رایگان" if free_ready else "🎯 رایگان (بزودی)"
    return InlineKeyboardMarkup([
        [btn_main("📦 مشاهده پلن‌ها", "show_plans")],
        [btn_main(free_txt, "free_analysis")],
        [btn_action("💬 پشتیبانی", "support")],
    ])

def admin_kb():
    return InlineKeyboardMarkup([
        [btn_main("📊 آمار کلی", "ad_stats")],
        [btn_main("💰 رسیدهای در انتظار", "ad_pending")],
        [btn_main("👥 لیست کاربران", "ad_users")],
        [btn_main("🔍 جستجوی کاربر", "ad_search")],
        [btn_main("🎁 تخفیف", "ad_discount")],
        [btn_main("🎯 کاربران رایگان", "ad_free_users")],
        [btn_action("📢 ارسال همگانی", "ad_broadcast")],
        [btn_action("💾 بکاپ دیتابیس", "ad_backup")],
        [btn_action("⚙️ تنظیمات", "ad_settings")],
    ])

def admin_user_kb(target_uid):
    return InlineKeyboardMarkup([
        [btn("📦 فعال‌سازی اشتراک", "ad_act_" + str(target_uid))],
        [btn("⏰ تمدید ۷ روز", "ad_ext7_" + str(target_uid))],
        [btn("⏰ تمدید ۳۰ روز", "ad_ext30_" + str(target_uid))],
        [btn("🚫 لغو اشتراک", "ad_revoke_" + str(target_uid))],
        [btn("🎁 تخفیف ۲۰٪", "ad_disc20_" + str(target_uid))],
        [btn("🎁 تخفیف ۵۰٪", "ad_disc50_" + str(target_uid))],
        [btn("🚫 حذف تخفیف", "ad_disc0_" + str(target_uid))],
        [btn_back("ad_users")],
    ])

# ═══════════════════════ START / PANEL ═══════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"

    if is_admin(uid):
        stats = get_stats()
        return await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 👥 کل: **" + str(stats['total']) + "**\n"
            "│ ✅ فعال: **" + str(stats['active']) + "**\n"
            "│ ❌ منقضی: **" + str(stats['expired']) + "**\n"
            "│ 🚫 لغو: **" + str(stats['revoked']) + "**\n"
            "│ ⏳ در انتظار: **" + str(stats['pending']) + "**\n"
            "│ 📊 تحلیل: **" + str(stats['total_analyses']) + "**\n"
            "│ 🎯 رایگان: **" + str(stats['free_users']) + "**\n"
            "└─────────────────────┘",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    sub = get_sub(uid)
    if sub and sub["status"] == "active":
        p = PLANS.get(sub["plan"], {})
        expires = sub["expires_at"][:10] if sub["expires_at"] else "—"
        usage = sub["usage_count"]
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        remaining = "∞"
        if sub["expires_at"]:
            exp = datetime.fromisoformat(sub["expires_at"])
            remaining = str((exp - datetime.now()).days) + " روز"
        discount = sub.get("discount_percent", 0)
        disc_text = "\n│ 🏷 تخفیف: **" + str(discount) + "٪**" if discount > 0 else ""
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ⚔ **" + BOT_NAME + "** ⚔\n"
            "╚══════════════════════╝\n\n"
            "سلام **" + name + "** 👋\n\n"
            "┌─────────────────────┐\n"
            "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "│ 📊 تحلیل‌ها: **" + str(usage) + "/" + str(max_u) + "**\n"
            "│ 📅 باقی‌مانده: " + remaining + disc_text + "\n"
            "└─────────────────────┘\n\n"
            "📸 عکس اسکرین‌شات بفرستید:",
            parse_mode="Markdown", reply_markup=main_menu_kb(uid)
        )
    else:
        free_time = get_free_remaining(uid)
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ⚔ **" + BOT_NAME + "** ⚔\n"
            "╚══════════════════════╝\n\n"
            "🎮 **تحلیلگر هوشمند شرط‌بندی MK**\n"
            "مخصوص سیستم مارتینگل\n\n"
            "🎯 **تحلیل رایگان:** هر ۳ روز یکبار\n"
            "⏰ باقی‌مانده: **" + free_time + "**\n\n"
            "یا اشتراک بخرید برای تحلیل نامحدود:",
            parse_mode="Markdown", reply_markup=main_menu_kb(uid)
        )

async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    stats = get_stats()
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
        "╚══════════════════════╝\n\n"
        "┌─────────────────────┐\n"
        "│ 👥 کل: **" + str(stats['total']) + "**\n"
        "│ ✅ فعال: **" + str(stats['active']) + "**\n"
        "│ ❌ منقضی: **" + str(stats['expired']) + "**\n"
        "│ 🚫 لغو: **" + str(stats['revoked']) + "**\n"
        "│ ⏳ در انتظار: **" + str(stats['pending']) + "**\n"
        "│ 📊 تحلیل: **" + str(stats['total_analyses']) + "**\n"
        "│ 🎯 رایگان: **" + str(stats['free_users']) + "**\n"
        "└─────────────────────┘",
        parse_mode="Markdown", reply_markup=admin_kb()
    )

# ═══════════════════════ CALLBACKS ═══════════════════════

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    uid = q.from_user.id

    # ─── MAIN MENU ───
    if d == "main_menu":
        sub = get_sub(uid)
        if is_admin(uid):
            stats = get_stats()
            return await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
                "╚══════════════════════╝\n\n"
                "👥 کل: **" + str(stats['total']) + "** | ✅ فعال: **" + str(stats['active']) + "**\n"
                "❌ منقضی: **" + str(stats['expired']) + "** | 🚫 لغو: **" + str(stats['revoked']) + "**\n"
                "⏳ انتظار: **" + str(stats['pending']) + "** | 📊 تحلیل: **" + str(stats['total_analyses']) + "**\n"
                "🎯 رایگان: **" + str(stats['free_users']) + "**",
                parse_mode="Markdown", reply_markup=admin_kb()
            )
        if sub and sub["status"] == "active":
            p = PLANS.get(sub["plan"], {})
            expires = sub["expires_at"][:10] if sub["expires_at"] else "—"
            usage = sub["usage_count"]
            max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
            remaining = "∞"
            if sub["expires_at"]:
                exp = datetime.fromisoformat(sub["expires_at"])
                remaining = str((exp - datetime.now()).days) + " روز"
            discount = sub.get("discount_percent", 0)
            disc_text = "\n│ 🏷 تخفیف: **" + str(discount) + "٪**" if discount > 0 else ""
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⚔ **" + BOT_NAME + "** ⚔\n"
                "╚══════════════════════╝\n\n"
                "سلام **" + q.from_user.first_name + "** 👋\n\n"
                "┌─────────────────────┐\n"
                "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
                "│ 📊 تحلیل‌ها: **" + str(usage) + "/" + str(max_u) + "**\n"
                "│ 📅 باقی‌مانده: " + remaining + disc_text + "\n"
                "└─────────────────────┘\n\n"
                "📸 عکس اسکرین‌شات بفرستید:",
                parse_mode="Markdown", reply_markup=main_menu_kb(uid)
            )
        else:
            free_time = get_free_remaining(uid)
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⚔ **" + BOT_NAME + "** ⚔\n"
                "╚══════════════════════╝\n\n"
                "🎮 **تحلیلگر هوشمند شرط‌بندی MK**\n\n"
                "🎯 **تحلیل رایگان:** هر ۳ روز یکبار\n"
                "⏰ باقی‌مانده: **" + free_time + "**",
                parse_mode="Markdown", reply_markup=main_menu_kb(uid)
            )

    # ─── FREE ANALYSIS ───
    elif d == "free_analysis":
        if is_admin(uid):
            return await q.edit_message_text(
                "📸 عکس اسکرین‌شات رو بفرستید.",
                reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
            )
        if can_use_free(uid):
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  🎯 **تحلیل رایگان**\n"
                "╚══════════════════════╝\n\n"
                "اسکرین‌شات مسابقه رو بفرستید.\n"
                "بعد از هر تحلیل، ۳ روز صبر کنید.\n\n"
                "یا اشتراک بخرید برای تحلیل نامحدود:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [btn_main("📦 خرید اشتراک", "show_plans")],
                    [btn_back("main_menu")]
                ])
            )
        else:
            free_time = get_free_remaining(uid)
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⏳ **تحلیل رایگان در دسترس نیست**\n"
                "╚══════════════════════╝\n\n"
                "⏰ باقی‌مانده: **" + free_time + "**\n\n"
                "یا اشتراک بخرید:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [btn_main("📦 خرید اشتراک", "show_plans")],
                    [btn_back("main_menu")]
                ])
            )

    # ─── PLANS ───
    elif d == "show_plans":
        sub = get_sub(uid)
        discount = sub.get("discount_percent", 0) if sub else 0
        def dp(price):
            if discount > 0: return str(int(price * (100 - discount) / 100))
            return str(price)
        disc_text = "\n🏷 **تخفیف " + str(discount) + "٪ فعال!**" if discount > 0 else ""
        kb = InlineKeyboardMarkup([
            [btn("⚡ ۱ روزه — $" + dp(5), "plan_day")],
            [btn("📦 ۱ ماهه پایه — $" + dp(30), "plan_month_basic")],
            [btn("👑 ۱ ماهه نامحدود — $" + dp(60), "plan_month_unlimited")],
            [btn("📅 ۱ ساله پایه — $" + dp(250), "plan_year_basic")],
            [btn("💎 ۱ ساله نامحدود — $" + dp(350), "plan_year_unlimited")],
            [btn_back("main_menu")],
        ])
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "     📦 **پلن‌های اشتراک**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────────┐\n"
            "│ ⚡ **۱ روزه — $" + dp(5) + "** — هر ۳۰ دقیقه\n"
            "├─────────────────────────┤\n"
            "│ 📦 **۱ ماهه پایه — $" + dp(30) + "** — ۵۰۰ تحلیل\n"
            "├─────────────────────────┤\n"
            "│ 👑 **۱ ماهه نامحدود — $" + dp(60) + "**\n"
            "├─────────────────────────┤\n"
            "│ 📅 **۱ ساله پایه — $" + dp(250) + "** — ۵۰۰۰ تحلیل\n"
            "├─────────────────────────┤\n"
            "│ 💎 **۱ ساله نامحدود — $" + dp(350) + "**\n"
            "└─────────────────────────┘\n\n"
            "💰 پرداخت: **USDT (TRC20)**" + disc_text,
            parse_mode="Markdown", reply_markup=kb
        )

    elif d.startswith("plan_"):
        plan = d.replace("plan_", "")
        if plan not in PLANS:
            return await q.edit_message_text("پلن نامعتبر.")
        p = PLANS[plan]
        conn = sqlite3.connect(str(DB_PATH))
        existing = conn.execute("SELECT id FROM pending_payments WHERE user_id=? AND status='pending'", (uid,)).fetchone()
        if existing:
            conn.execute("UPDATE pending_payments SET plan=? WHERE id=?", (plan, existing[0]))
        else:
            conn.execute("INSERT INTO pending_payments (user_id, username, plan, tx_hash) VALUES (?, ?, ?, ?)",
                         (uid, q.from_user.username or "", plan, "awaiting_payment"))
        conn.commit(); conn.close()
        kb = InlineKeyboardMarkup([
            [btn_action("📋 کپی آدرس", "copy_addr")],
            [btn_action("📝 ارسال رسید", "how_receipt")],
            [btn_back("show_plans")],
        ])
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  💰 **پرداخت — " + p['name'] + "**\n"
            "╚══════════════════════╝\n\n"
            "💵 مبلغ: **$" + str(p['price']) + "**\n"
            "📝 " + p['desc'] + "\n\n"
            "🔹 **آدرس USDT (TRC20):**\n"
            "`" + PAYMENT_ADDRESS + "`\n\n"
            "⚠️ فقط **USDT TRC20**\n"
            "بعد TX Hash رو اینجا بفرستید.",
            parse_mode="Markdown", reply_markup=kb
        )

    elif d == "how_receipt":
        await q.edit_message_text(
            "📝 **ارسال رسید**\n\n۱. پرداخت کنید\n۲. TX Hash رو کپی کنید\n۳. اینجا بفرستید\n\nفرمت: `TX_HASH`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )

    elif d == "send_photo_hint":
        await q.edit_message_text(
            "📸 **اسکرین‌شات مسابقه رو بفرستید**\n\nتحلیل خودکار و فوری انجام میشه.",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )

    elif d == "my_stats":
        sub = get_sub(uid)
        if not sub or sub["status"] != "active":
            return await q.edit_message_text("اشتراک فعال ندارید.", reply_markup=InlineKeyboardMarkup([[btn_main("📦 پلن‌ها", "show_plans")]]))
        p = PLANS.get(sub["plan"], {})
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        remaining = "∞"
        if sub["expires_at"]:
            exp = datetime.fromisoformat(sub["expires_at"])
            remaining = str((exp - datetime.now()).days) + " روز"
        discount = sub.get("discount_percent", 0)
        disc_text = " | 🏷 " + str(discount) + "٪" if discount > 0 else ""
        await q.edit_message_text(
            "📊 **آمار شما**\n\n"
            "📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "📊 تحلیل‌ها: **" + str(sub['usage_count']) + "/" + str(max_u) + "**\n"
            "📅 باقی‌مانده: " + remaining + disc_text,
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_action("📸 تحلیل", "send_photo_hint")],
                [btn_main("📦 تمدید", "show_plans")],
                [btn_back("main_menu")],
            ])
        )

    elif d == "support":
        await q.edit_message_text("💬 پیام مستقیم بفرستید:", reply_markup=InlineKeyboardMarkup([
            [btn_action("💬 ارسال پیام", "https://t.me/admin")],
            [btn_back("main_menu")],
        ]))

    elif d == "copy_addr":
        await q.answer("📋 کپی شد:\n" + PAYMENT_ADDRESS, show_alert=True)

    # ─── ADMIN: APPROVE/REJECT ───
    elif d.startswith("approve_") or d.startswith("reject_"):
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        try: target = int(d.split("_")[1])
        except: return await q.answer("❌", show_alert=True)
        approve = d.startswith("approve_")
        conn = sqlite3.connect(str(DB_PATH))
        if approve:
            row = conn.execute("SELECT plan FROM pending_payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
            plan = row[0] if row and row[0] in PLANS else "month_basic"
            conn.execute("UPDATE pending_payments SET status='approved' WHERE user_id=? AND status='pending'", (target,))
            conn.commit(); conn.close()
            activate_sub(target, "", plan)
            try:
                await context.bot.send_message(target, "✅ **اشتراک فعال شد!**\n\nپلن: " + PLANS[plan]['name'] + "\n📸 عکس بفرست!", parse_mode="Markdown", reply_markup=main_menu_kb(target))
            except: pass
            await q.edit_message_text("✅ تایید — " + plan + " برای " + str(target))
        else:
            conn.execute("UPDATE pending_payments SET status='rejected' WHERE user_id=? AND status='pending'", (target,))
            conn.commit(); conn.close()
            try: await context.bot.send_message(target, "❌ **رسید تایید نشد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("❌ رد شد.")

    # ─── ADMIN: STATS ───
    elif d == "ad_stats":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        stats = get_stats()
        await q.edit_message_text(
            "📊 **آمار کلی**\n\n"
            "👥 کل: **" + str(stats['total']) + "** | ✅ فعال: **" + str(stats['active']) + "**\n"
            "❌ منقضی: **" + str(stats['expired']) + "** | 🚫 لغو: **" + str(stats['revoked']) + "**\n"
            "⏳ انتظار: **" + str(stats['pending']) + "** | 📊 تحلیل: **" + str(stats['total_analyses']) + "**\n"
            "🎯 رایگان: **" + str(stats['free_users']) + "**",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: PENDING ───
    elif d == "ad_pending":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, username, plan, tx_hash, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text("📭 خالیه.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        msg = "💰 **رسیدهای در انتظار**\n\n"
        for r in rows:
            msg += "🆔 `" + str(r[0]) + "` — @" + (r[1] or "—") + "\n📦 " + PLANS.get(r[2],{}).get('name',r[2]) + " — `" + r[3][:15] + "...`\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: USERS ───
    elif d == "ad_users":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, username, plan, status, usage_count, expires_at, discount_percent FROM subscriptions ORDER BY status='active' DESC, expires_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text("📭 خالیه.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        msg = "👥 **کاربران**\n\n"
        for r in rows:
            se = "✅" if r[3] == "active" else ("🚫" if r[3] == "revoked" else "❌")
            disc = " 🏷" + str(r[6]) + "٪" if r[6] > 0 else ""
            msg += se + " `" + str(r[0]) + "` — @" + (r[1] or "—") + disc + "\n  📦 " + PLANS.get(r[2],{}).get('name',r[2]) + " | 📊 " + str(r[4]) + "\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: SEARCH ───
    elif d == "ad_search":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text("🔍 آیدی عددی کاربر رو بفرستید:\n`/srch USER_ID`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: DISCOUNT ───
    elif d == "ad_discount":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text("🎁 **تخفیف**\n\n`/disc USER_ID PERCENT`\nمثال: `/disc 123 20`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: FREE USERS ───
    elif d == "ad_free_users":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, last_free_at, free_count FROM free_users ORDER BY last_free_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text("📭 کاربر رایگانی نیست.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        msg = "🎯 **کاربران رایگان**\n\n"
        for r in rows:
            last = r[1][:16] if r[1] else "هرگز"
            msg += "🆔 `" + str(r[0]) + "` — 📊 " + str(r[2]) + " تحلیل — 🕐 " + last + "\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: BROADCAST ───
    elif d == "ad_broadcast":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text("📢 **ارسال همگانی**\n\n`/bc متن پیام`", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: BACKUP ───
    elif d == "ad_backup":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        data = export_subs_json()
        if data:
            await context.bot.send_document(uid, io.BytesIO(data.encode()), filename="backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json", caption="💾 **بکاپ دیتابیس**")
            await q.edit_message_text("✅ بکاپ ارسال شد.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        else:
            await q.edit_message_text("❌ خطا در بکاپ.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: SETTINGS ───
    elif d == "ad_settings":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text(
            "⚙️ **تنظیمات**\n\n"
            "🤖 نام: " + BOT_NAME + "\n"
            "🔗 API: `" + API_URL + "`\n"
            "🧠 مدل: `" + MODEL_NAME + "`\n"
            "💰 آدرس: `" + PAYMENT_ADDRESS + "`\n"
            "👤 ادمین: `" + str(ADMIN_ID) + "`\n"
            "🎯 رایگان: هر " + str(FREE_COOLDOWN_HOURS // 24) + " روز",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: BACK TO MENU ───
    elif d == "admin_menu":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        stats = get_stats()
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
            "╚══════════════════════╝\n\n"
            "👥 کل: **" + str(stats['total']) + "** | ✅ فعال: **" + str(stats['active']) + "**\n"
            "❌ منقضی: **" + str(stats['expired']) + "** | 🚫 لغو: **" + str(stats['revoked']) + "**\n"
            "⏳ انتظار: **" + str(stats['pending']) + "** | 📊 تحلیل: **" + str(stats['total_analyses']) + "**\n"
            "🎯 رایگان: **" + str(stats['free_users']) + "**",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

    # ─── ADMIN: USER ACTIONS ───
    elif d.startswith("ad_act_") or d.startswith("ad_ext") or d.startswith("ad_revoke_") or d.startswith("ad_disc"):
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        try: target = int(d.split("_")[-1])
        except: return await q.answer("❌", show_alert=True)

        if d.startswith("ad_act_"):
            await q.edit_message_text("📦 **انتخاب پلن**", reply_markup=InlineKeyboardMarkup([
                [btn("⚡ ۱ روزه", "ad_activate_" + str(target) + "_day")],
                [btn("📦 ماهانه پایه", "ad_activate_" + str(target) + "_month_basic")],
                [btn("👑 ماهانه نامحدود", "ad_activate_" + str(target) + "_month_unlimited")],
                [btn("📅 سالانه پایه", "ad_activate_" + str(target) + "_year_basic")],
                [btn("💎 سالانه نامحدود", "ad_activate_" + str(target) + "_year_unlimited")],
                [btn_back("ad_users")],
            ]))
        elif d.startswith("ad_activate_"):
            parts = d.replace("ad_activate_", "").split("_")
            target = int(parts[0])
            plan = "_".join(parts[1:])
            if plan not in PLANS: return await q.answer("نامعتبر", show_alert=True)
            activate_sub(target, "", plan)
            try: await context.bot.send_message(target, "✅ **اشتراک فعال شد!**\nپلن: " + PLANS[plan]['name'], parse_mode="Markdown", reply_markup=main_menu_kb(target))
            except: pass
            await q.edit_message_text("✅ " + plan + " برای " + str(target))
        elif d.startswith("ad_ext7_"):
            extend_sub(target, 7)
            try: await context.bot.send_message(target, "⏰ **۷ روز تمدید شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("✅ " + str(target) + " → ۷ روز")
        elif d.startswith("ad_ext30_"):
            extend_sub(target, 30)
            try: await context.bot.send_message(target, "⏰ **۳۰ روز تمدید شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("✅ " + str(target) + " → ۳۰ روز")
        elif d.startswith("ad_revoke_"):
            revoke_sub(target)
            try: await context.bot.send_message(target, "🚫 **اشتراک لغو شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("🚫 " + str(target) + " لغو شد.")
        elif d.startswith("ad_disc20_"):
            set_discount(target, 20); await q.edit_message_text("🎁 تخفیف ۲۰٪ → " + str(target))
        elif d.startswith("ad_disc50_"):
            set_discount(target, 50); await q.edit_message_text("🎁 تخفیف ۵۰٪ → " + str(target))
        elif d.startswith("ad_disc0_"):
            set_discount(target, 0); await q.edit_message_text("🚫 تخفیف حذف → " + str(target))

# ═══════════════════════ TEXT HANDLER ═══════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # ─── ADMIN: SEARCH ───
    if is_admin(uid) and text.startswith("/srch ") and len(context.args) == 1:
        try: target = int(context.args[0])
        except: return await update.message.reply_text("آیدی نامعتبر.")
        sub = get_sub(target)
        if not sub:
            return await update.message.reply_text("یافت نشد.", reply_markup=InlineKeyboardMarkup([[btn("📦 فعال‌سازی", "ad_act_" + str(target))], [btn_back("admin_menu")]]))
        p = PLANS.get(sub["plan"], {})
        mx = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        disc = sub.get("discount_percent", 0)
        disc_text = "\n🏷 تخفیف: **" + str(disc) + "٪**" if disc > 0 else ""
        await update.message.reply_text(
            "🔍 **" + str(target) + "**\n\n"
            "👤 @" + (sub["username"] or "—") + "\n"
            "📦 " + p.get('name', sub['plan']) + "\n"
            "📊 " + str(sub['usage_count']) + "/" + str(mx) + "\n"
            "📅 " + (sub['expires_at'][:10] if sub['expires_at'] else "—") + disc_text,
            parse_mode="Markdown", reply_markup=admin_user_kb(target)
        )
        return

    # ─── ADMIN: DISCOUNT ───
    if is_admin(uid) and text.startswith("/disc ") and len(context.args) == 2:
        try: target = int(context.args[0]); percent = int(context.args[1])
        except: return await update.message.reply_text("فرمت: `/disc USER_ID %`", parse_mode="Markdown")
        set_discount(target, percent)
        try:
            if percent > 0: await context.bot.send_message(target, "🎁 **تخفیف " + str(percent) + "٪ فعال شد!**", parse_mode="Markdown")
            else: await context.bot.send_message(target, "🚫 **تخفیف حذف شد.**", parse_mode="Markdown")
        except: pass
        await update.message.reply_text("✅ تخفیف " + str(percent) + "٪ → " + str(target))
        return

    # ─── ADMIN: BROADCAST ───
    if is_admin(uid) and text.startswith("/bc ") and len(context.args) >= 1:
        msg_text = " ".join(context.args)
        user_ids = get_all_user_ids()
        sent = failed = 0
        for user_id in user_ids:
            try: await context.bot.send_message(user_id, "📢 **" + BOT_NAME + "**\n\n" + msg_text, parse_mode="Markdown"); sent += 1
            except: failed += 1
        await update.message.reply_text("✅ " + str(sent) + " | ❌ " + str(failed))
        return

    # ─── CHECK FOR TX HASH ───
    if len(text) >= 10 and not text.startswith("/"):
        conn = sqlite3.connect(str(DB_PATH))
        existing = conn.execute("SELECT id, plan FROM pending_payments WHERE user_id=? AND status='pending'", (uid,)).fetchone()
        if existing and existing[1] not in ("unknown", "awaiting_payment", ""):
            conn.execute("UPDATE pending_payments SET tx_hash=?, created_at=datetime('now') WHERE id=?", (text, existing[0]))
            conn.commit(); conn.close()
        else:
            conn.close()
            return await update.message.reply_text("❌ ابتدا پلن انتخاب کنید.", reply_markup=InlineKeyboardMarkup([[btn_main("📦 پلن‌ها", "show_plans")]]))

        conn2 = sqlite3.connect(str(DB_PATH))
        row = conn2.execute("SELECT plan FROM pending_payments WHERE id=?", (existing[0],)).fetchone()
        pn = PLANS.get(row[0], {}).get('name', row[0]) if row else "نامشخص"
        conn2.close()

        if ADMIN_ID:
            try:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تایید", callback_data="approve_" + str(uid)),
                     InlineKeyboardButton("❌ رد", callback_data="reject_" + str(uid))]
                ])
                await context.bot.send_message(ADMIN_ID,
                    "💰 **رسید جدید**\n\n👤 " + (update.effective_user.first_name or "") + " (@" + (update.effective_user.username or "") + ")\n📦 " + pn + "\n📝 `" + text[:30] + "...`",
                    parse_mode="Markdown", reply_markup=kb
                )
            except: pass

        await update.message.reply_text("✅ **رسید ثبت شد.** در انتظار تایید...", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]]))
        return

# ═══════════════════════ PHOTO HANDLER ═══════════════════════

@gate
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_free = False
    if not is_admin(uid):
        sub = get_sub(uid)
        if not sub or sub["status"] != "active":
            if can_use_free(uid):
                is_free = True
            else:
                return await update.message.reply_text("❌ اشتراک ندارید و تحلیل رایگان در دسترس نیست.")

    msg = await update.message.reply_text("⏳ **در حال تحلیل...** ⚔️", parse_mode="Markdown")
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
        if is_free:
            record_free_analysis(uid)
        else:
            record_analysis(uid)
        await msg.delete()
        for p in split_msg(analysis):
            await update.message.reply_text(p, parse_mode="Markdown")
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━\n📸 تحلیل بعدی؟",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )
    except Exception as e:
        err = str(e)
        if "unavailable" in err.lower(): user_msg = "❌ سرویس AI موقتاً در دسترس نیست."
        elif "rate" in err.lower() or "429" in err: user_msg = "⏳ درخواست زیاده. صبر کنید."
        else: user_msg = "❌ خطا: `" + err[:200] + "`"
        await msg.edit_text(user_msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]]))

# ═══════════════════════ ADMIN COMMANDS ═══════════════════════

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT user_id, username, plan, tx_hash FROM pending_payments WHERE status='pending'").fetchall()
    conn.close()
    if not rows: return await update.message.reply_text("📭 خالی.")
    msg = "💰 **در انتظار**\n\n"
    for r in rows: msg += "🆔 `" + str(r[0]) + "` — " + PLANS.get(r[2],{}).get('name',r[2]) + "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def activate_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(context.args) < 2: return await update.message.reply_text("`/act USER_ID PLAN`", parse_mode="Markdown")
    try: target = int(context.args[0]); plan = context.args[1]
    except: return await update.message.reply_text("فرمت نادرست.")
    if plan not in PLANS: return await update.message.reply_text("پلن نامعتبر.")
    activate_sub(target, "", plan)
    try: await context.bot.send_message(target, "✅ **اشتراک فعال شد!**\nپلن: " + PLANS[plan]['name'], parse_mode="Markdown", reply_markup=main_menu_kb(target))
    except: pass
    await update.message.reply_text("✅ فعال شد.")

# ═══════════════════════ MAIN ═══════════════════════

def main():
    if not TOKEN: log.error("TOKEN empty"); return
    if not API_KEY: log.error("API_KEY empty"); return
    if ADMIN_ID == 0: log.warning("ADMIN_ID not set")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("act", activate_manual))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("⚔ " + BOT_NAME + " running")
    app.run_polling()

if __name__ == "__main__":
    main()
