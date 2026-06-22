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
    """)
    conn.execute("DELETE FROM pending_payments WHERE tx_hash IN ('awaiting_payment', '') AND status='pending'")
    conn.commit(); conn.close()

def get_sub(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(r) if r else None

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
            return False, "صبر کن " + str(int(sub["cooldown_minutes"] - diff)) + " دقیقه"
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
    conn.close()
    return {"total": total, "active": active, "expired": expired, "revoked": revoked,
            "pending": pending, "total_analyses": total_analyses}

# ═══════════════════════ SYSTEM PROMPT ═══════════════════════

SYSTEM_PROMPT = (
    "تو 'تحلیلگر ارشد Mortal Kombat' هستی — یک متخصص ۲۰ ساله MK.\n"
    "فقط فارسی خروجی بده. هیچوقت انگلیسی ننویس.\n\n"

    "دانش تو از MK:\n"
    "- تمام کاراکترها و move set های آنها (بازی‌های MK11, MK1, MK1 Mobile)\n"
    "- frame data: startup, recovery, plus-on-block, minus-on-block\n"
    "- combo damage: کومبوهای بهینه و optimal punish\n"
    "- matchup knowledge: چه کاراکتری در برابر چه کاراکتری برتری داره\n"
    "- tournament meta: ترندهای فعلی بازی و tier list\n"
    "- betting markets: انواع شرط‌بندی (moneyline, props, round duration, total rounds)\n\n"

    "قوانین تحلیل:\n"
    "1. ابتدا عکس رو دقیق بررسی کن — کاراکترها، ضرایب، نوع شرط\n"
    "2. تحلیل matchup: چه کسی در چه رنجی (close/mid/far) برتره\n"
    "3. frame advantage: کی turn داره، کی باید صبر کنه\n"
    "4. combo potential: هر کاراکتر چقدر damage میزنه\n"
    "5. اولویت با props (زیر/بالای ثانیه) بیشتر از moneyline\n"
    "6. فقط پیشنهاد با ضریب >= ۱.۸ بده\n"
    "7. احتمال واقع‌بینانه — حداکثر ۶۵٪ (MK همیشه unpredictable هست)\n"
    "8. اگر هیچ گزینه مناسبی نیست: 'وضعیت: خطرناک. وارد نشو.'\n"
    "9. هیچوقت ۱۰۰٪ اطمینان نده\n\n"

    "فرمت خروجی:\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "⚔ [کاراکتر۱] vs [کاراکتر۲]\n\n"

    "📊 تحلیل:\n"
    "▸ [کاراکتر۱]: [سبک بازی + نقطه قوت + نقطه ضعف — ۲ خط]\n"
    "▸ [کاراکتر۲]: [سبک بازی + نقطه قوت + نقطه ضعف — ۲ خط]\n"
    "▸ مقایسه: [چه کسی در چه رنجی برتره + frame advantage — ۱ خط]\n"
    "▸ کومبو: [هر کاراکتر چقدر damage میزنه — ۱ خط]\n\n"

    "🎯 پیشنهاد مارتینگل:\n"
    "▸ [شرط دقیق] — ضریب [X.XX]\n"
    "▸ احتمال برد: [XX–XX٪]\n"
    "▸ دلیل: [۲-۳ خط منطقی با اشاره به frame data یا matchup]\n\n"

    "⚠️ هشدارها:\n"
    "▸ [نکات ریسک و شرایط خاص]\n"

    "━━━━━━━━━━━━━━━━━━\n\n"

    "نکات استراتژیک:\n"
    "- راند اول معمولاً محافظه‌کارانه → props زمان بهترین گزینه\n"
    "- اگر ضریب برنده < ۱.۵ → ارزش ریسک نداره\n"
    "- اگر دو کاراکتر tier نزدیک → props بهتر از moneyline\n"
    "- rushdown vs zoner → معمولاً round 1 کشیده میشه\n"
    "- mirror match → همیشه unpredictable → وارد نشو\n"
    "- اگر یک کاراکتر明显ly بهتره → ضریبش پایینه و ارزش نداره\n"
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
    content = [{"type": "text", "text": prompt or "اسکرین‌شات مسابقه Mortal Kombat رو تحلیل کن. کاراکترها، ضرایب و نوع شرط رو شناسایی کن و تحلیل کامل بده."}]
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
                [btn_action("پشتیبانی", "support")]
            ])
            await update.message.reply_text(
                "╔══════════════════════╗\n"
                "  🔐 **" + BOT_NAME + "**\n"
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
            [btn_main("📊 آمار من", "my_stats")],
            [btn_main("📦 تمدید اشتراک", "show_plans")],
            [btn_action("💬 پشتیبانی", "support")],
        ])
    return InlineKeyboardMarkup([
        [btn_main("📦 مشاهده پلن‌ها", "show_plans")],
        [btn_action("💬 پشتیبانی", "support")],
    ])

def admin_kb():
    return InlineKeyboardMarkup([
        [btn_main("📊 آمار کلی", "ad_stats")],
        [btn_main("💰 رسیدهای در انتظار", "ad_pending")],
        [btn_main("👥 لیست کاربران", "ad_users")],
        [btn_main("🔍 جستجوی کاربر", "ad_search")],
        [btn_main("🎁 تخفیف", "ad_discount")],
        [btn_action("📢 ارسال همگانی", "ad_broadcast")],
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
        [btn("🎁 حذف تخفیف", "ad_disc0_" + str(target_uid))],
        [btn_back("ad_users")],
    ])

# ═══════════════════════ START ═══════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"
    sub = get_sub(uid)

    if is_admin(uid):
        stats = get_stats()
        return await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 👥 کل کاربران: **" + str(stats['total']) + "**\n"
            "│ ✅ فعال: **" + str(stats['active']) + "**\n"
            "│ ❌ منقضی: **" + str(stats['expired']) + "**\n"
            "│ 🚫 لغو شده: **" + str(stats['revoked']) + "**\n"
            "│ ⏳ در انتظار: **" + str(stats['pending']) + "**\n"
            "│ 📊 کل تحلیل‌ها: **" + str(stats['total_analyses']) + "**\n"
            "└─────────────────────┘",
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
        await update.message.reply_text(
            "╔══════════════════════╗\n"
            "  ⚔ **" + BOT_NAME + "** ⚔\n"
            "╚══════════════════════╝\n\n"
            "🎮 **تحلیلگر هوشمند شرط‌بندی MK**\n"
            "مخصوص سیستم مارتینگل\n\n"
            "🔐 **نیاز به اشتراک**\n\n"
            "برای شروع دکمه زیر را بزنید:",
            parse_mode="Markdown", reply_markup=main_menu_kb(uid)
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
            await q.edit_message_text(
                "╔══════════════════════╗\n"
                "  ⚔ **" + BOT_NAME + "** ⚔\n"
                "╚══════════════════════╝\n\n"
                "🎮 **تحلیلگر هوشمند شرط‌بندی MK**\n"
                "مخصوص سیستم مارتینگل\n\n"
                "🔐 **نیاز به اشتراک**",
                parse_mode="Markdown", reply_markup=main_menu_kb(uid)
            )

    # ─── PLANS ───
    elif d == "show_plans":
        sub = get_sub(uid)
        discount = sub.get("discount_percent", 0) if sub else 0
        def disc_price(price):
            if discount > 0:
                return str(int(price * (100 - discount) / 100))
            return str(price)
        kb = InlineKeyboardMarkup([
            [btn("⚡ ۱ روزه — $" + disc_price(5), "plan_day")],
            [btn("📦 ۱ ماهه پایه — $" + disc_price(30), "plan_month_basic")],
            [btn("👑 ۱ ماهه نامحدود — $" + disc_price(60), "plan_month_unlimited")],
            [btn("📅 ۱ ساله پایه — $" + disc_price(250), "plan_year_basic")],
            [btn("💎 ۱ ساله نامحدود — $" + disc_price(350), "plan_year_unlimited")],
            [btn_back("main_menu")],
        ])
        disc_text = "\n🏷 **تخفیف " + str(discount) + "٪ فعال** — قیمت‌ها اعمال شده!" if discount > 0 else ""
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "     📦 **پلن‌های اشتراک**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────────┐\n"
            "│ ⚡ **۱ روزه — $" + disc_price(5) + "**\n"
            "│ هر ۳۰ دقیقه یک تحلیل\n"
            "├─────────────────────────┤\n"
            "│ 📦 **۱ ماهه پایه — $" + disc_price(30) + "**\n"
            "│ ۵۰۰ تحلیل — هر ۱۵ دقیقه\n"
            "├─────────────────────────┤\n"
            "│ 👑 **۱ ماهه نامحدود — $" + disc_price(60) + "**\n"
            "│ بدون محدودیت\n"
            "├─────────────────────────┤\n"
            "│ 📅 **۱ ساله پایه — $" + disc_price(250) + "**\n"
            "│ ۵۰۰۰ تحلیل — هر ۲۵ دقیقه\n"
            "├─────────────────────────┤\n"
            "│ 💎 **۱ ساله نامحدود — $" + disc_price(350) + "**\n"
            "│ بدون محدودیت\n"
            "└─────────────────────────┘\n\n"
            "💰 پرداخت: **USDT (TRC20)**" + disc_text + "\n\n"
            "پلن مورد نظر را انتخاب کنید:",
            parse_mode="Markdown", reply_markup=kb
        )

    # ─── PLAN SELECTION ───
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
            "┌─────────────────────┐\n"
            "│ 💵 مبلغ: **$" + str(p['price']) + "**\n"
            "│ 📦 پلن: **" + p['name'] + "**\n"
            "│ 📝 " + p['desc'] + "\n"
            "└─────────────────────┘\n\n"
            "🔹 **آدرس USDT (TRC20):**\n"
            "`" + PAYMENT_ADDRESS + "`\n\n"
            "⚠️ فقط **USDT TRC20** بفرستید.\n"
            "بعد از پرداخت، TX Hash رو اینجا بفرستید.",
            parse_mode="Markdown", reply_markup=kb
        )

    elif d == "how_receipt":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📝 **ارسال رسید پرداخت**\n"
            "╚══════════════════════╝\n\n"
            "۱. پرداخت رو انجام بدید\n"
            "۲. TX Hash رو کپی کنید\n"
            "۳. اینجا بفرستید\n\n"
            "فرمت: `TX_HASH_HERE`\n"
            "(معمولاً ۶۴ کاراکتر)",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )

    elif d == "send_photo_hint":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📸 **تحلیل مسابقه**\n"
            "╚══════════════════════╝\n\n"
            "اسکرین‌شات مسابقه رو بفرستید.\n"
            "تحلیل خودکار و فوری انجام میشه.\n\n"
            "🎯 مخصوص سیستم مارتینگل",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )

    elif d == "my_stats":
        sub = get_sub(uid)
        if not sub or sub["status"] != "active":
            return await q.edit_message_text(
                "اشتراک فعال ندارید.",
                reply_markup=InlineKeyboardMarkup([[btn_main("📦 مشاهده پلن‌ها", "show_plans")]])
            )
        p = PLANS.get(sub["plan"], {})
        max_u = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        remaining = "∞"
        if sub["expires_at"]:
            exp = datetime.fromisoformat(sub["expires_at"])
            remaining = str((exp - datetime.now()).days) + " روز"
        discount = sub.get("discount_percent", 0)
        disc_text = " | 🏷 " + str(discount) + "٪ تخفیف" if discount > 0 else ""
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📊 **آمار شما**\n"
            "╚══════════════════════╝\n\n"
            "┌─────────────────────┐\n"
            "│ 👤 **" + q.from_user.first_name + "**\n"
            "│ 📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "│ 📊 تحلیل‌ها: **" + str(sub['usage_count']) + "/" + str(max_u) + "**\n"
            "│ 📅 باقی‌مانده: " + remaining + disc_text + "\n"
            "│ 💎 وضعیت: ✅ فعال\n"
            "└─────────────────────┘",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_action("📸 تحلیل مسابقه", "send_photo_hint")],
                [btn_main("📦 تمدید", "show_plans")],
                [btn_back("main_menu")],
            ])
        )

    elif d == "support":
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  💬 **پشتیبانی**\n"
            "╚══════════════════════╝\n\n"
            "پیام مستقیم بفرستید:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                [btn_action("💬 ارسال پیام", "https://t.me/admin")],
                [btn_back("main_menu")],
            ])
        )

    elif d == "copy_addr":
        await q.answer("📋 آدرس کپی شد:\n" + PAYMENT_ADDRESS, show_alert=True)

    # ─── ADMIN: APPROVE/REJECT ───
    elif d.startswith("approve_") or d.startswith("reject_"):
        if not is_admin(uid):
            return await q.answer("⛔ غیرمجاز", show_alert=True)
        try:
            target = int(d.split("_")[1])
        except:
            return await q.answer("❌ خطا", show_alert=True)
        approve = d.startswith("approve_")
        conn = sqlite3.connect(str(DB_PATH))
        if approve:
            row = conn.execute("SELECT plan FROM pending_payments WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1", (target,)).fetchone()
            plan = row[0] if row and row[0] in PLANS else "month_basic"
            conn.execute("UPDATE pending_payments SET status='approved' WHERE user_id=? AND status='pending'", (target,))
            conn.commit(); conn.close()
            activate_sub(target, "", plan)
            p = PLANS[plan]
            try:
                cd = "بدون محدودیت" if p['cooldown'] == 0 else "هر " + str(p['cooldown']) + " دقیقه"
                mx = "∞" if p['max'] <= 0 else str(p['max'])
                await context.bot.send_message(target,
                    "╔══════════════════════╗\n"
                    "  ✅ **اشتراک فعال شد!**\n"
                    "╚══════════════════════╝\n\n"
                    "📦 پلن: " + p['name'] + "\n"
                    "📊 محدودیت: " + mx + " تحلیل\n"
                    "⏰ کول‌داون: " + cd + "\n"
                    "📅 مدت: " + str(p['days']) + " روز\n\n"
                    "📸 عکس بفرست برای تحلیل!",
                    parse_mode="Markdown", reply_markup=main_menu_kb(target)
                )
            except: pass
            await q.edit_message_text("✅ تایید شد — اشتراک " + plan + " برای " + str(target))
        else:
            conn.execute("UPDATE pending_payments SET status='rejected' WHERE user_id=? AND status='pending'", (target,))
            conn.commit(); conn.close()
            try:
                await context.bot.send_message(target,
                    "❌ **رسید شما تایید نشد.**\nبا ادمین تماس بگیرید.",
                    parse_mode="Markdown"
                )
            except: pass
            await q.edit_message_text("❌ رد شد.")

    # ─── ADMIN: STATS ───
    elif d == "ad_stats":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        stats = get_stats()
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  📊 **آمار کلی سیستم**\n"
            "╚══════════════════════╝\n\n"
            "👥 کل کاربران: **" + str(stats['total']) + "**\n"
            "✅ فعال: **" + str(stats['active']) + "**\n"
            "❌ منقضی: **" + str(stats['expired']) + "**\n"
            "🚫 لغو شده: **" + str(stats['revoked']) + "**\n"
            "⏳ در انتظار: **" + str(stats['pending']) + "**\n"
            "📊 کل تحلیل‌ها: **" + str(stats['total_analyses']) + "**",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: PENDING ───
    elif d == "ad_pending":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, username, plan, tx_hash, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text("📭 رسید در انتظار نیست.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        msg = "💰 **رسیدهای در انتظار**\n\n"
        for r in rows:
            pn = PLANS.get(r[2], {}).get('name', r[2])
            msg += "🆔 `" + str(r[0]) + "` — @" + (r[1] or "—") + "\n📦 " + pn + " — `" + r[3][:15] + "...`\n🕐 " + r[4][:16] + "\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: USERS ───
    elif d == "ad_users":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT user_id, username, plan, status, usage_count, expires_at, discount_percent FROM subscriptions ORDER BY status='active' DESC, expires_at DESC").fetchall()
        conn.close()
        if not rows:
            return await q.edit_message_text("📭 کاربری نیست.", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))
        msg = "👥 **لیست کاربران**\n\n"
        for r in rows:
            se = "✅" if r[3] == "active" else ("🚫" if r[3] == "revoked" else "❌")
            pn = PLANS.get(r[2], {}).get('name', r[2])
            exp = r[5][:10] if r[5] else "—"
            disc = " 🏷" + str(r[6]) + "٪" if r[6] > 0 else ""
            msg += se + " `" + str(r[0]) + "` — @" + (r[1] or "—") + disc + "\n  📦 " + pn + " | 📊 " + str(r[4]) + " | 📅 " + exp + "\n\n"
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]]))

    # ─── ADMIN: SEARCH ───
    elif d == "ad_search":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text(
            "🔍 **جستجوی کاربر**\n\nآیدی عددی کاربر رو بفرستید:\n`/srch USER_ID`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: DISCOUNT ───
    elif d == "ad_discount":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text(
            "🎁 **تنظیم تخفیف**\n\n"
            "آیدی کاربر و درصد تخفیف رو بفرستید:\n"
            "`/disc USER_ID PERCENT`\n\n"
            "مثال: `/disc 123456 20` → تخفیف ۲۰٪",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: BROADCAST ───
    elif d == "ad_broadcast":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text(
            "📢 **ارسال همگانی**\n\nمتن پیام رو بفرستید:\n`/bc متن پیام`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: SETTINGS ───
    elif d == "ad_settings":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        await q.edit_message_text(
            "⚙️ **تنظیمات سیستم**\n\n"
            "🤖 نام ربات: " + BOT_NAME + "\n"
            "🔗 API: " + API_URL + "\n"
            "🧠 مدل: " + MODEL_NAME + "\n"
            "💰 آدرس پرداخت: `" + PAYMENT_ADDRESS + "`\n"
            "👤 ادمین: `" + str(ADMIN_ID) + "`",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("admin_menu")]])
        )

    # ─── ADMIN: USER ACTION SUB-MENUS ───
    elif d.startswith("ad_act_") or d.startswith("ad_ext7_") or d.startswith("ad_ext30_") or d.startswith("ad_revoke_") or d.startswith("ad_disc"):
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        try:
            target = int(d.split("_")[-1])
        except:
            return await q.answer("❌ خطا", show_alert=True)

        if d.startswith("ad_act_"):
            await q.edit_message_text(
                "📦 **انتخاب پلن برای " + str(target) + "**\n\nپلن رو انتخاب کنید:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([
                    [btn("⚡ ۱ روزه", "ad_activate_" + str(target) + "_day")],
                    [btn("📦 ماهانه پایه", "ad_activate_" + str(target) + "_month_basic")],
                    [btn("👑 ماهانه نامحدود", "ad_activate_" + str(target) + "_month_unlimited")],
                    [btn("📅 سالانه پایه", "ad_activate_" + str(target) + "_year_basic")],
                    [btn("💎 سالانه نامحدود", "ad_activate_" + str(target) + "_year_unlimited")],
                    [btn_back("ad_users")],
                ])
            )
        elif d.startswith("ad_activate_"):
            parts = d.replace("ad_activate_", "").split("_")
            target = int(parts[0])
            plan = "_".join(parts[1:])
            if plan not in PLANS:
                return await q.answer("پلن نامعتبر", show_alert=True)
            activate_sub(target, "", plan)
            p = PLANS[plan]
            try:
                await context.bot.send_message(target,
                    "✅ **اشتراک فعال شد!**\n\nپلن: " + p['name'] + "\n\n📸 عکس بفرست!",
                    parse_mode="Markdown", reply_markup=main_menu_kb(target)
                )
            except: pass
            await q.edit_message_text("✅ اشتراک " + plan + " برای " + str(target) + " فعال شد.")

        elif d.startswith("ad_ext7_"):
            extend_sub(target, 7)
            try:
                await context.bot.send_message(target, "⏰ **اشتراک ۷ روز تمدید شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("✅ " + str(target) + " → ۷ روز تمدید.")

        elif d.startswith("ad_ext30_"):
            extend_sub(target, 30)
            try:
                await context.bot.send_message(target, "⏰ **اشتراک ۳۰ روز تمدید شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("✅ " + str(target) + " → ۳۰ روز تمدید.")

        elif d.startswith("ad_revoke_"):
            revoke_sub(target)
            try:
                await context.bot.send_message(target, "🚫 **اشتراک شما لغو شد.**", parse_mode="Markdown")
            except: pass
            await q.edit_message_text("🚫 اشتراک " + str(target) + " لغو شد.")

        elif d.startswith("ad_disc20_"):
            set_discount(target, 20)
            await q.edit_message_text("🎁 تخفیف ۲۰٪ برای " + str(target) + " فعال شد.")

        elif d.startswith("ad_disc50_"):
            set_discount(target, 50)
            await q.edit_message_text("🎁 تخفیف ۵۰٪ برای " + str(target) + " فعال شد.")

        elif d.startswith("ad_disc0_"):
            set_discount(target, 0)
            await q.edit_message_text("🚫 تخفیف " + str(target) + " حذف شد.")

    # ─── ADMIN: BACK TO ADMIN MENU ───
    elif d == "admin_menu":
        if not is_admin(uid): return await q.answer("⛔", show_alert=True)
        stats = get_stats()
        await q.edit_message_text(
            "╔══════════════════════╗\n"
            "  🛡 **پنل مدیریت " + BOT_NAME + "**\n"
            "╚══════════════════════╝\n\n"
            "👥 کل: **" + str(stats['total']) + "** | ✅ فعال: **" + str(stats['active']) + "**\n"
            "❌ منقضی: **" + str(stats['expired']) + "** | 🚫 لغو: **" + str(stats['revoked']) + "**\n"
            "⏳ در انتظار: **" + str(stats['pending']) + "** | 📊 تحلیل: **" + str(stats['total_analyses']) + "**",
            parse_mode="Markdown", reply_markup=admin_kb()
        )

# ═══════════════════════ TEXT HANDLER ═══════════════════════

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()

    # ─── ADMIN: SEARCH USER ───
    if is_admin(uid) and text.startswith("/srch ") and len(context.args) == 1:
        try:
            target = int(context.args[0])
        except:
            return await update.message.reply_text("آیدی نامعتبر.")
        sub = get_sub(target)
        if not sub:
            return await update.message.reply_text(
                "❌ کاربر یافت نشد.\n\nآیا میخواهید اشتراک فعال کنید؟",
                reply_markup=InlineKeyboardMarkup([
                    [btn("📦 فعال‌سازی", "ad_act_" + str(target))],
                    [btn_back("admin_menu")]
                ])
            )
        p = PLANS.get(sub["plan"], {})
        se = "✅" if sub["status"] == "active" else "❌"
        mx = sub["max_usage"] if sub["max_usage"] > 0 else "∞"
        disc = sub.get("discount_percent", 0)
        disc_text = "\n🏷 تخفیف: **" + str(disc) + "٪**" if disc > 0 else ""
        await update.message.reply_text(
            "🔍 **اطلاعات کاربر**\n\n"
            "🆔 آیدی: `" + str(target) + "`\n"
            "👤 یوزرنیم: @" + (sub["username"] or "—") + "\n"
            + se + " وضعیت: " + sub["status"] + "\n"
            "📦 پلن: " + p.get('name', sub['plan']) + "\n"
            "📊 تحلیل‌ها: " + str(sub['usage_count']) + "/" + str(mx) + "\n"
            "📅 انقضا: " + (sub['expires_at'][:10] if sub['expires_at'] else "—") + disc_text,
            parse_mode="Markdown",
            reply_markup=admin_user_kb(target)
        )
        return

    # ─── ADMIN: DISCOUNT ───
    if is_admin(uid) and text.startswith("/disc ") and len(context.args) == 2:
        try:
            target = int(context.args[0])
            percent = int(context.args[1])
        except:
            return await update.message.reply_text("فرمت: `/disc USER_ID PERCENT`", parse_mode="Markdown")
        if percent < 0 or percent > 100:
            return await update.message.reply_text("درصد باید ۰-۱۰۰ باشد.")
        set_discount(target, percent)
        try:
            if percent > 0:
                await context.bot.send_message(target,
                    "🎁 **تخفیف " + str(percent) + "٪ فعال شد!**\n\n"
                    "قیمت‌های پلن‌ها با تخفیف اعمال شده.",
                    parse_mode="Markdown"
                )
            else:
                await context.bot.send_message(target, "🚫 **تخفیف شما حذف شد.**", parse_mode="Markdown")
        except: pass
        await update.message.reply_text("✅ تخفیف " + str(percent) + "٪ برای " + str(target) + " تنظیم شد.")
        return

    # ─── ADMIN: BROADCAST ───
    if is_admin(uid) and text.startswith("/bc ") and len(context.args) >= 1:
        msg_text = " ".join(context.args)
        user_ids = get_all_user_ids()
        sent = 0
        failed = 0
        for user_id in user_ids:
            try:
                await context.bot.send_message(user_id,
                    "📢 **" + BOT_NAME + "**\n\n" + msg_text,
                    parse_mode="Markdown"
                )
                sent += 1
            except:
                failed += 1
        await update.message.reply_text("✅ ارسال شد: " + str(sent) + "\n❌ ناموفق: " + str(failed))
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
            return await update.message.reply_text(
                "❌ ابتدا یک پلن انتخاب کنید.",
                reply_markup=InlineKeyboardMarkup([[btn_main("📦 پلن‌ها", "show_plans")]])
            )

        conn2 = sqlite3.connect(str(DB_PATH))
        row = conn2.execute("SELECT plan FROM pending_payments WHERE id=?", (existing[0],)).fetchone()
        pn = PLANS.get(row[0], {}).get('name', row[0]) if row else "نامشخص"
        conn2.close()

        if ADMIN_ID:
            try:
                name = update.effective_user.first_name or "کاربر"
                uname = update.effective_user.username or "—"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تایید", callback_data="approve_" + str(uid)),
                     InlineKeyboardButton("❌ رد", callback_data="reject_" + str(uid))]
                ])
                await context.bot.send_message(ADMIN_ID,
                    "💰 **رسید جدید**\n\n👤 " + name + " (@" + uname + ")\n🆔 `" + str(uid) + "`\n📦 " + pn + "\n📝 `" + text[:30] + "...`\n\nتایید یا رد کن:",
                    parse_mode="Markdown", reply_markup=kb
                )
            except: pass

        await update.message.reply_text(
            "✅ **رسید ثبت شد.**\nادمین در حال بررسی...",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )
        return

# ═══════════════════════ PHOTO HANDLER ═══════════════════════

@gate
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ **" + BOT_NAME + " در حال تحلیل...**\nلطفاً صبر کنید ⚔️",
        parse_mode="Markdown"
    )
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        raw = io.BytesIO()
        await file.download_to_memory(raw)
        raw = raw.getvalue()
        if len(raw) > MAX_IMAGE_SIZE:
            return await msg.edit_text("❌ حجم تصویر زیاد است. حداکثر ۸MB.")
        compressed = compress_image(raw)
        b64 = base64.b64encode(compressed).decode()
        analysis = await call_ai(b64)
        record_analysis(update.effective_user.id)
        await msg.delete()
        for p in split_msg(analysis):
            await update.message.reply_text(p, parse_mode="Markdown")
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━\n📸 تحلیل بعدی؟ عکس بفرستید 👇",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[btn_back("main_menu")]])
        )
    except Exception as e:
        err = str(e)
        if "unavailable" in err.lower() or "credits" in err.lower():
            user_msg = "❌ سرویس AI موقتاً در دسترس نیست."
        elif "rate" in err.lower() or "429" in err:
            user_msg = "⏳ درخواست‌ها زیاد شده. چند دقیقه صبر کنید."
        else:
            user_msg = "❌ خطا: `" + err[:200] + "`"
        await msg.edit_text(user_msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[btn_action("💬 پشتیبانی", "support")]]))

# ═══════════════════════ MAIN ═══════════════════════

async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT user_id, username, plan, tx_hash, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC").fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("📭 رسید در انتظار نیست.")
    msg = "💰 **رسیدهای در انتظار**\n\n"
    for r in rows:
        pn = PLANS.get(r[2], {}).get('name', r[2])
        msg += "🆔 `" + str(r[0]) + "` — @" + (r[1] or "—") + " — " + pn + " — `" + r[3][:15] + "...`\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def activate_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    if len(context.args) < 2:
        return await update.message.reply_text("فرمت: `/act USER_ID PLAN`", parse_mode="Markdown")
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
        await context.bot.send_message(target_uid,
            "✅ **اشتراک فعال شد!**\n\nپلن: " + p['name'] + "\n\n📸 عکس بفرست!",
            parse_mode="Markdown", reply_markup=main_menu_kb(target_uid)
        )
    except: pass
    await update.message.reply_text("✅ اشتراک " + plan + " برای " + str(target_uid) + " فعال شد.")

def main():
    if not TOKEN:   log.error("TOKEN empty"); return
    if not API_KEY: log.error("API_KEY empty"); return
    if ADMIN_ID == 0: log.warning("ADMIN_ID not set")

    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pending", pending))
    app.add_handler(CommandHandler("act", activate_manual))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("⚔ " + BOT_NAME + " running")
    app.run_polling()

if __name__ == "__main__":
    main()
