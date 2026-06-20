import os, io, base64, asyncio, logging
from pathlib import Path
from dotenv import load_dotenv
import httpx
from PIL import Image
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TOKEN = os.getenv("TOKEN", "")
API_KEY = os.getenv("API_KEY", "")
API_URL = os.getenv("API_URL", "https://api.freemodel.dev/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "شما 'شهباز کور' هستید — یک متخصص حرفه‌ای Mortal Kombat با ۱۰+ سال تجربه در آنالیز مچ‌آپ‌های حرفه‌ای و شرط‌بندی.\n"
    "خروجی فقط فارسی، لحن کاملاً تخصصی.\n\n"
    "قوانین:\n"
    "1. کاراکترها را عمیق تحلیل کن: سبک بازی، فریم‌دیتا، ریسک/ریوارد ابزارها.\n"
    "2. بگو کدام کاراکتر در کدام رنج برتری دارد.\n"
    "3. بگو مچ‌آپ به نفع کیست و چرا — با استناد به متای فعلی ۲۰۲۶.\n"
    "4. ضرایب را استخراج کن و بگو بازار با متا همخوانی دارد یا نه.\n"
    "5. اولویت با گزینه‌های با ضریب >= ۱.۸ برای مارتینگل.\n"
    "6. اگر گزینه >= ۱.۸ وجود ندارد: 'مناسب مارتینگل نیست' + بهترین گزینه موجود را بده.\n"
    "7. احتمال موفقیت واقع‌بینانه.\n"
    "8. اگر تصویر نامشخص است: وضعیت: خطرناک. وارد نشو.\n\n"
    "فرمت خروجی (دقیقاً):\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "تحلیل مچ‌آپ: [A] vs [B]\n"
    "متای ۲۰۲۶: ...\n\n"
    "تحلیل کاراکترها:\n"
    "▸ [A]: [آرکتایپ] — [ابزارها، نقاط قوت، نقاط ضعف، رنج]\n"
    "▸ [B]: [آرکتایپ] — [ابزارها، نقاط قوت، نقاط ضعف، رنج]\n"
    "▸ برتری مچ‌آپ: ...\n\n"
    "بازار برد:\n"
    "▸ [A]: ضریب X.XXX\n"
    "▸ [B]: ضریب X.XXX\n"
    "▸ ارزیابی: ...\n\n"
    "تحلیل بازار زمان راند اول:\n"
    "▸ بازه محتمل: XX تا XX ثانیه\n"
    "▸ نقطه تعادل: حدود XX ثانیه\n"
    "▸ سناریوی بازی: ...\n\n"
    "گزینه مخصوص مارتینگل:\n"
    "▸ زیر/بالای X.X ثانیه — ضریب X.XXX\n"
    "▸ احتمال موفقیت: XX–XX٪\n"
    "▸ مناسب مارتینگل: بله/خیر\n"
    "▸ دلیل: ...\n\n"
    "سیگنال نهایی:\n"
    "▸ ...\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━"
)

MAX_IMAGE_SIZE = 8 * 1024 * 1024
MAX_DIMENSION = 2048

def compress_image(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        img = img.convert("RGB")
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
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    content = [{"type": "text", "text": user_prompt or "اسکرین‌شات مسابقه Mortal Kombat را تحلیل کن."}]
    content.append({
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}
    })
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content}
    ]
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.15,
        "max_tokens": 1500,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(API_URL, headers=headers, json=payload)
        if resp.status_code != 200:
            raise Exception(f"AI API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔ **شهباز کور — ترمینال شرط‌بندی MK** ⚔\n\n"
        "یک عکس از اسکرین‌شات مسابقه بفرست تا تحلیل کنم.\n"
        "من مخصوص سیستم **مارتینگل** طراحی شده‌ام.\n\n"
        "دستورات:\n"
        "/start — راهنما\n"
        "/analyze [A] vs [B] — تحلیل دستی\n"
        "یا просто عکس بفرست."
    )

async def analyze_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text or "vs" not in text.lower():
        await update.message.reply_text("فرمت: /analyze Sindel vs Sub-Zero")
        return
    msg = await update.message.reply_text("🔄 در حال تحلیل...")
    try:
        result = await call_ai_api("", f"تحلیل مچ‌آپ: {text}")
        parts = split_long_message(result)
        for p in parts:
            await update.message.reply_text(p)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔄 شهباز در حال تحلیل تصویر...")
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
        await msg.delete()
        parts = split_long_message(analysis)
        for p in parts:
            await update.message.reply_text(p, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:300]}")

def split_long_message(text: str, max_len: int = 4000) -> list:
    if len(text) <= max_len:
        return [text]
    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].strip()
    return parts

def main():
    if not TOKEN:
        log.error("TOKEN is empty. Set in .env")
        return
    if not API_KEY:
        log.error("API_KEY is empty. Set in .env")
        return
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    log.info("✅ Shahbaz Core Telegram Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
