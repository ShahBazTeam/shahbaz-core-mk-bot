import os, io, base64, asyncio, logging, sqlite3, string, random, json
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
import httpx
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TOKEN = os.getenv("TOKEN", "")
API_KEY = os.getenv("API_KEY", "")
API_URL = os.getenv("API_URL", "https://api.freemodel.dev/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-5.5")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DB_PATH = Path("data.db")

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            key TEXT PRIMARY KEY,
            created_by INTEGER,
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            expires_at TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            license_key TEXT,
            activated_at TEXT DEFAULT (datetime('now')),
            usage_count INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()

def is_licensed(user_id: int) -> bool:
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT is_premium FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None and row[0] == 1

def require_license(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_licensed(uid):
            await update.message.reply_text(
                "👑 **شهباز کور — دسترسی محدود** 👑\n\n"
                "شما لایسنس فعال ندارید.\n"
                "برای فعال‌سازی، کد لایسنس خود را وارد کنید:\n\n"
                "`/activate XXXX-XXXX-XXXX`\n\n"
                "🔐 برای دریافت لایسنس با ادمین تماس بگیرید.",
                parse_mode="Markdown"
            )
            return
        return await func(update, context)
    return wrapper

SYSTEM_PROMPT = (
    "شما 'شهباز کور' هستید — متخصص حرفه‌ای Mortal Kombat.\n"
    "خروجی فقط فارسی.\n\n"
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

def compress_image(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA": img = img.convert("RGB")
    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        ratio = min(MAX_DIMENSION / w, MAX_DIMENSION / h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()

def image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")

async def call_ai_api(image_b64: str, user_prompt: str = "") -> str:
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    content = [{"type": "text", "text": user_prompt or "اسکرین‌شات مسابقه Mortal Kombat را تحلیل کن."}]
    content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]
    payload = {"model": MODEL_NAME, "messages": messages, "temperature": 0.15, "max_tokens": 1500}
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(API_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            raise Exception(f"AI API error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"]

def generate_key() -> str:
    chars = string.ascii_uppercase + string.digits
    return "-".join("".join(random.choices(chars, k=4)) for _ in range(3))

def split_message(text: str, max_len: int = 3800) -> list:
    if len(text) <= max_len: return [text]
    parts = []
    while text:
        if len(text) <= max_len: parts.append(text); break
        s = text.rfind("\n", 0, max_len)
        if s < max_len // 2: s = max_len
        parts.append(text[:s]); text = text[s:].strip()
    return parts

# ─── GATE: START ───
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name or "کاربر"

    if is_licensed(uid):
        conn = sqlite3.connect(str(DB_PATH))
        c = conn.cursor()
        c.execute("UPDATE users SET usage_count = usage_count + 0 WHERE user_id = ?", (uid,))
        c.execute("SELECT license_key, usage_count FROM users WHERE user_id = ?", (uid,))
        row = c.fetchone()
        conn.close()
        key = row[0] if row else "—"
        usage = row[1] if row else 0
        await update.message.reply_text(
            f"⚔ **شهباز کور** ⚔\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 {name} — کاربر ویژه\n"
            f"🔑 لایسنس: `{key}`\n"
            f"📊 تحلیل‌ها: {usage}\n\n"
            "📸 عکس مسابقه بفرست تا تحلیل کنم.\n"
            "یا `/analyze Smoke vs Sub-Zero`\n\n"
            f"🔐 `{key}` — معتبر",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "⚔ **شهباز کور** ⚔\n"
            "ترمینال تحلیلی شرط‌بندی Mortal Kombat\n"
            "نسخه VIP — مخصوص سیستم مارتینگل\n\n"
            "🔐 برای استفاده نیاز به لایسنس داری.\n"
            "کد لایسنست رو بفرست:\n\n"
            "`/activate XXXX-XXXX-XXXX`\n\n"
            "📩 برای خرید لایسنس با ادمین تماس بگیر.",
            parse_mode="Markdown"
        )

# ─── ACTIVATE ───
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
        await update.message.reply_text("❌ کد لایسنس نامعتبر است.")
        return
    if row[0] == 0:
        conn.close()
        await update.message.reply_text("❌ این لایسنس قبلاً غیرفعال شده.")
        return

    c.execute("UPDATE licenses SET is_active = 0 WHERE key = ?", (key,))
    c.execute("""
        INSERT INTO users (user_id, username, license_key, usage_count, is_premium)
        VALUES (?, ?, ?, 0, 1)
        ON CONFLICT(user_id) DO UPDATE SET license_key = excluded.license_key, is_premium = 1, username = excluded.username
    """, (uid, uname, key))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ **لایسنس فعال شد.**\n"
        f"🔑 کد: `{key}`\n\n"
        "حالا می‌تونی عکس بفرستی یا `/analyze` بزنی.\n"
        "به خانواده شهباز خوش اومدی 🎯",
        parse_mode="Markdown"
    )

# ─── ADMIN: GENKEY ───
async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return

    note = " ".join(context.args) if context.args else ""
    key = generate_key()
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("INSERT INTO licenses (key, created_by, note) VALUES (?, ?, ?)", (key, uid, note))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🔑 **لایسنس جدید ساخته شد**\n\n"
        f"`{key}`\n\n"
        f"یادداشت: {note or '—'}\n"
        "برای ارسال به کاربر از دکمه زیر استفاده کن.",
        parse_mode="Markdown"
    )

# ─── ADMIN: LIST ───
async def listkeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("SELECT key, note, is_active, created_at FROM licenses ORDER BY created_at DESC LIMIT 20")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📭 هیچ لایسنس یافت نشد.")
        return

    msg = "📋 **لایسنس‌ها (۲۰ تا آخر)**\n\n"
    for k, n, a, t in rows:
        status = "✅" if a else "❌"
        msg += f"{status} `{k}` — {n or '—'} — {t[:10]}\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

# ─── ADMIN: REVOKE ───
async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ دسترسی غیرمجاز.")
        return

    key = " ".join(context.args).strip().upper()
    if not key:
        await update.message.reply_text("فرمت: `/revoke XXXX-XXXX-XXXX`", parse_mode="Markdown")
        return

    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("UPDATE licenses SET is_active = 0 WHERE key = ?", (key,))
    c.execute("UPDATE users SET is_premium = 0 WHERE license_key = ?", (key,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"❌ لایسنس `{key}` غیرفعال شد.", parse_mode="Markdown")

# ─── ANALYZE TEXT ───
@require_license
async def analyze_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or "vs" not in text.lower():
        await update.message.reply_text("فرمت: `/analyze Smoke vs Sub-Zero`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("⏳ شهباز در حال تحلیل...")
    try:
        result = await call_ai_api("", f"تحلیل مچ‌آپ: {text}")
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (update.effective_user.id,))
        conn.commit()
        conn.close()
        await msg.delete()
        for p in split_message(result):
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ─── ANALYZE PHOTO ───
@require_license
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ شهباز در حال تحلیل تصویر...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        raw = io.BytesIO()
        await file.download_to_memory(raw)
        raw = raw.getvalue()
        if len(raw) > MAX_IMAGE_SIZE:
            await msg.edit_text("❌ حجم تصویر زیاد است. حداکثر ۸ مگابایت.")
            return
        compressed = compress_image(raw)
        b64 = image_to_base64(compressed)
        analysis = await call_ai_api(b64)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("UPDATE users SET usage_count = usage_count + 1 WHERE user_id = ?", (update.effective_user.id,))
        conn.commit()
        conn.close()
        await msg.delete()
        for p in split_message(analysis):
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:300]}")

# ─── MAIN ───
def main():
    if not TOKEN:    log.error("TOKEN is empty"); return
    if not API_KEY:  log.error("API_KEY is empty"); return
    if ADMIN_ID == 0: log.warning("ADMIN_ID not set. Admin commands disabled.")

    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("analyze", analyze_text))
    app.add_handler(CommandHandler("genkey", genkey))
    app.add_handler(CommandHandler("listkeys", listkeys))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    log.info("👑 Shahbaz Core VIP Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
