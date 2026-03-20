import os, json, logging, re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"].strip()

SYSTEM_PROMPT = """You are a retail field insights analyst. Extract structured insights from store visit notes.

You MUST return ONLY a valid JSON object. No explanation, no markdown, no backticks, no text before or after.
Start your response with { and end with }.

Use this exact structure:
{
  "summary": "1-2 sentence executive summary",
  "sentiment": "Positive or Mixed or Negative",
  "priority": "High or Medium or Low",
  "units_sold": "number as string or null",
  "sales": ["insight 1", "insight 2"],
  "competitor": ["insight"],
  "customer": ["insight"],
  "stock": ["insight"],
  "staff": ["insight"],
  "promo": ["insight"],
  "actions": [{"action": "what to do", "urgency": "Urgent or Soon or Monitor"}]
}

Rules:
- Only include array items that have real content from the notes
- Empty arrays [] are fine if nothing relevant was mentioned
- Keep each insight concise, one sentence
- Always return valid JSON, never truncate"""

CATEGORY_ICONS = {
    "sales":      ("📈", "Sales Performance"),
    "competitor": ("👀", "Competitor Activity"),
    "customer":   ("💬", "Customer Feedback"),
    "stock":      ("📦", "Stock & Display"),
    "staff":      ("🙋", "Staff Feedback"),
    "promo":      ("🎯", "Promo Effectiveness"),
}

URGENCY_ICONS   = {"Urgent": "🔴", "Soon": "🟡", "Monitor": "🟢"}
SENTIMENT_ICONS = {"Positive": "🟢", "Mixed": "🟡", "Negative": "🔴"}
PRIORITY_ICONS  = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}


def esc(text: str) -> str:
    """Escape text for Telegram HTML mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def call_claude(notes: str, reporter: str, model: str) -> dict:
    user_msg = f"Reporter: {reporter or 'Unknown'}\n\nStore visit notes:\n{notes}"
    payload  = {
        "model": model,
        "max_tokens": 2048,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}]
    }
    logger.info(f"Calling model: {model} | key prefix: {ANTHROPIC_KEY[:12]}...")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload
        )
    logger.info(f"Anthropic status: {r.status_code} | response: {r.text[:600]}")
    if r.status_code != 200:
        raise ValueError(f"ANTHROPIC_ERROR_{r.status_code}: {r.text[:500]}")
    data  = r.json()
    raw   = "".join(b.get("text", "") for b in data.get("content", []))
    clean = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
    start = clean.find("{")
    end   = clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in response")
    return json.loads(clean[start:end])


def format_response(d: dict, reporter: str, date_str: str) -> str:
    sent_icon = SENTIMENT_ICONS.get(d.get("sentiment", "Mixed"), "🟡")
    pri_icon  = PRIORITY_ICONS.get(d.get("priority",  "Medium"), "🟡")

    lines = [
        "📋 <b>Field Insights Report</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Reporter:</b> {esc(reporter or '—')}",
        f"📅 <b>Date:</b> {esc(date_str)}",
        f"📊 <b>Sentiment:</b> {sent_icon} {esc(d.get('sentiment','—'))}   |   <b>Priority:</b> {pri_icon} {esc(d.get('priority','—'))}",
    ]
    if d.get("units_sold") not in (None, "null", "", "None"):
        lines.append(f"🛒 <b>Units sold:</b> {esc(str(d['units_sold']))}")

    lines += ["", "📝 <b>Summary</b>", f"<i>{esc(d.get('summary','—'))}</i>"]

    for key, (icon, label) in CATEGORY_ICONS.items():
        items = d.get(key, [])
        if items:
            lines += ["", f"{icon} <b>{label}</b>"]
            for item in items:
                lines.append(f"• {esc(item)}")

    actions = d.get("actions", [])
    if actions:
        lines += ["", "⚡ <b>Actions Required</b>"]
        for a in actions:
            urg = a.get("urgency", "Soon")
            lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i> — {esc(a.get('action',''))}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def get_reporter_name(message) -> str:
    if message.forward_origin:
        origin = message.forward_origin
        if hasattr(origin, "sender_user") and origin.sender_user:
            return origin.sender_user.full_name
        if hasattr(origin, "sender_user_name") and origin.sender_user_name:
            return origin.sender_user_name
        if hasattr(origin, "sender_chat") and origin.sender_chat:
            return origin.sender_chat.title or ""
    if message.from_user:
        return message.from_user.full_name
    return "Unknown"


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    notes   = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None):
            return
        await message.reply_text("⚠️ No text found in this forwarded message.")
        return

    reporter   = get_reporter_name(message)
    date_str   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    model      = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    processing = await message.reply_text("⏳ Extracting insights...")

    try:
        data     = await call_claude(notes, reporter, model)
        response = format_response(data, reporter, date_str)
        await processing.edit_text(response, parse_mode=ParseMode.HTML)
    except ValueError as e:
        logger.error(f"ValueError: {e}")
        await processing.edit_text(f"⚠️ Error: {str(e)[:300]}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON error: {e}")
        await processing.edit_text("⚠️ Could not parse AI response. Please try again.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await processing.edit_text(f"⚠️ Error: {str(e)[:200]}")


async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking API key and available models...")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}
            )
        if r.status_code == 200:
            models = [m["id"] for m in r.json().get("data", [])]
            msg = "✅ <b>API key is valid! Available models:</b>\n" + "\n".join(f"• <code>{m}</code>" for m in models)
        else:
            msg = f"❌ API error {r.status_code}:\n<code>{esc(r.text[:300])}</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Field Insights Bot is active!</b>\n\n"
        "Simply <b>forward</b> any store visit update into this chat.\n"
        "Works with text messages and photo messages with captions.\n\n"
        "<b>What I extract:</b>\n"
        "📈 Sales • 👀 Competitors • 💬 Customer feedback\n"
        "📦 Stock &amp; display • 🙋 Staff • 🎯 Promo effectiveness\n"
        "⚡ Actions required (Urgent / Soon / Monitor)",
        parse_mode=ParseMode.HTML
    )


def main():
    logger.info(f"Loaded API key prefix: {ANTHROPIC_KEY[:20]}")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("testapi", test_api))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.CAPTION),
        handle_forwarded
    ))
    logger.info("Bot is running — listening for forwarded messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
