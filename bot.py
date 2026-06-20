import os, io, base64, logging, sqlite3, string, random
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path("data.db")

# ═══════════════════════ DATABASE ═══════════════════════

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY, created_by INTEGER, note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')), expires_at TEXT, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', license_key TEXT,
            activated_at TEXT DEFAULT (datetime('now')), usage_count INTEGER DEFAULT 0, is_premium INTEGER DEFAULT 0
        );
    """)
    conn.commit(); conn.close()

def is_licensed(user_id: int) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT is_premium FROM users WHERE user_id = ?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r is not None and r[0] == 1

def get_user_stats(user_id: int):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT license_key, usage_count FROM users WHERE user_id = ?", (user_id,))
    r = c.fetchone()
    conn.close()
    return r if r else (None, 0)

# ═══════════════════════ LICENSE GATE ═══════════════════════

def gate(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_licensed(update.effective_user.id):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔑 فعال‌سازی لایسنس", callback_data="show_activate")]])
            await update.message.reply_text(
                "╔══════════════════════╗\n"
                "     🔐 **شهباز کور**\n"
                "     دسترسی محدود\n"
                "╚══════════════════════╝\n\n"
                "شما لایسنس فعال ندارید.\n"
                "برای استفاده، یک کد لایسنس وارد کنید:\n\n"
                "`/activate XXXX-XXXX-XXXX`\n\n"
                "📩 دریافت لایسنس: @admin",
                parse_mode="Markdown", reply_markup=kb
            )
            return
        return await func(update, context)
    return wrapper

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

# ═══════════════════════ COMMANDS ═══════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"

    if is_licensed(uid):
        key, usage = get_user_stats(uid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 ارسال عکس برای تحلیل", callback_data="send_photo_hint")],
            [InlineKeyboardButton("📊 آمار من", callback_data="my_stats")]
        ])
        await update.message.reply_text(
            f"⚔ **شهباز کور** ⚔\n"
            f"═━━━━━━━━━━━━━━━╕\n"
            f"👤 **{name}**\n"
            f"🔑 لایسنس: `{key}`\n"
            f"📊 تحلیلها: **{usage}**\n\n"
            f"📸 عکس بفرست تا تحلیل کنم\n"
            f"یا `/analyze Smoke vs Sub-Zero`",
            parse_mode="Markdown", reply_markup=kb
        )
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 فعال‌سازی لایسنس", callback_data="show_activate")],
            [InlineKeyboardButton("📩 دریافت لایسنس", url="https://t.me/admin")]
        ])
        await update.message.reply_text(
            "⚔ **شهباز کور** ⚔\n"
            "ترمینال تحلیلی شرط‌بندی MK\n"
            "مخصوص سیستم مارتینگل\n\n"
            "🔐 **نیاز به لایسنس**\n\n"
            "کد لایسنس خود را وارد کنید:\n"
            "`/activate XXXX-XXXX-XXXX`",
            parse_mode="Markdown", reply_markup=kb
        )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    uname = update.effective_user.username or ""
    key = " ".join(context.args).strip().upper()

    if not key:
        await update.message.reply_text("فرمت: `/activate XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT is_active FROM licenses WHERE key = ?", (key,))
    row = c.fetchone()

    if not row:
        conn.close()
        return await update.message.reply_text("❌ کد نامعتبر.")
    if row[0] == 0:
        conn.close()
        return await update.message.reply_text("❌ این کد قبلاً استفاده شده.")

    c.execute("UPDATE licenses SET is_active = 0 WHERE key = ?", (key,))
    c.execute("""INSERT INTO users (user_id, username, license_key, usage_count, is_premium)
        VALUES (?, ?, ?, 0, 1) ON CONFLICT(user_id) DO UPDATE
        SET license_key=excluded.license_key, is_premium=1, username=excluded.username""", (uid, uname, key))
    conn.commit(); conn.close()

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📸 ارسال عکس", callback_data="send_photo_hint")]])
    await update.message.reply_text(
        "╔══════════════════════╗\n"
        "  ✅ **لایسنس فعال شد**\n"
        "╚══════════════════════╝\n\n"
        f"🔑 `{key}`\n\n"
        "حالا می‌تونی عکس بفرستی.\n"
        "**به شهباز خوش اومدی** 🎯",
        parse_mode="Markdown", reply_markup=kb
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_licensed(uid):
        return await update.message.reply_text("لایسنس فعال نیست. از `/activate` استفاده کن.", parse_mode="Markdown")
    key, usage = get_user_stats(uid)
    name = update.effective_user.first_name or "کاربر"
    await update.message.reply_text(
        f"👤 **{name}**\n"
        f"🔑 `{key}`\n"
        f"📊 تحلیل‌های انجام شده: **{usage}**\n"
        f"💎 وضعیت: **VIP**",
        parse_mode="Markdown"
    )

# ─── ADMIN ───

def is_admin(uid): return uid == ADMIN_ID

async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    note = " ".join(context.args) if context.args else ""
    key = generate_key()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT INTO licenses (key, created_by, note) VALUES (?, ?, ?)", (key, ADMIN_ID, note))
    conn.commit(); conn.close()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 کپی", callback_data=f"copy_{key}")]
    ])
    await update.message.reply_text(
        f"🔑 **لایسنس جدید**\n\n`{key}`\n\n📝 {note or '—'}\n\n"
        "کد رو برای کاربر بفرست.",
        parse_mode="Markdown", reply_markup=kb
    )

async def listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute("SELECT key, note, is_active, created_at FROM licenses ORDER BY created_at DESC LIMIT 20").fetchall()
    conn.close()
    if not rows:
        return await update.message.reply_text("📭 هیچ لایسنس یافت نشد.")
    msg = "📋 **لایسنس‌ها**\n\n"
    for k, n, a, t in rows:
        s = "✅" if a else "❌"
        msg += f"{s} `{k}` — {n or '—'} ({t[:10]})\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ دسترسی غیرمجاز.")
    key = " ".join(context.args).strip().upper()
    if not key:
        return await update.message.reply_text("فرمت: `/revoke XXXX-XXXX-XXXX`", parse_mode="Markdown")
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE licenses SET is_active = 0 WHERE key = ?", (key,))
    conn.execute("UPDATE users SET is_premium = 0 WHERE license_key = ?", (key,))
    conn.commit(); conn.close()
    await update.message.reply_text(f"❌ `{key}` غیرفعال شد.", parse_mode="Markdown")

# ─── CALLBACKS ───

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "show_activate":
        await query.edit_message_text(
            "🔑 **فعال‌سازی لایسنس**\n\n"
            "کد ۱۲ رقمی خود را بفرست:\n`/activate XXXX-XXXX-XXXX`\n\n"
            "❓ کد نداری؟ با ادمین تماس بگیر.",
            parse_mode="Markdown"
        )
    elif data == "my_stats":
        uid = query.from_user.id
        if not is_licensed(uid):
            return await query.edit_message_text("❌ لایسنس فعال نیست.")
        key, usage = get_user_stats(uid)
        await query.edit_message_text(
            f"📊 **آمار شما**\n\n🔑 `{key}`\n📊 تحلیلها: **{usage}**\n💎 **VIP**",
            parse_mode="Markdown"
        )
    elif data == "send_photo_hint":
        await query.edit_message_text(
            "📸 فقط عکس اسکرین‌شات رو بفرست.\n"
            "شهباز خودش تحلیل می‌کنه.\n\n"
            "یا دستی: `/analyze Smoke vs Sub-Zero`",
            parse_mode="Markdown"
        )

# ─── ANALYZE TEXT ───

@gate
async def analyze_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or "vs" not in text.lower():
        return await update.message.reply_text("فرمت: `/analyze Smoke vs Sub-Zero`", parse_mode="Markdown")

    msg = await update.message.reply_text(
        "⏳ **شهباز در حال تحلیل...**\n━━━━━━━━━",
        parse_mode="Markdown"
    )
    try:
        result = await call_ai("", f"تحلیل مچ‌آپ: {text}")
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (update.effective_user.id,))
        conn.commit(); conn.close()
        await msg.delete()
        for p in split_msg(result):
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ─── ANALYZE PHOTO ───

@gate
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "⏳ **شهباز در حال تحلیل تصویر...**\n━━━━━━━━━",
        parse_mode="Markdown"
    )
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
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (update.effective_user.id,))
        conn.commit(); conn.close()
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
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("analyze", analyze_text))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("listkeys", listkeys))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info("👑 Shahbaz Core VIP Bot running")
    app.run_polling()

if __name__ == "__main__":
    main()
