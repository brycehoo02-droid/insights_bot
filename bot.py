import os, json, logging, re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

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


async def call_claude(notes: str, reporter: str) -> dict:
    user_msg = f"Reporter: {reporter or 'Unknown'}\n\nStore visit notes:\n{notes}"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 2048,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_msg}]
            }
        )
        data = r.json()

    logger.info(f"API status: {r.status_code}")
    if "error" in data:
        raise ValueError(f"API error: {data['error']}")

    raw = "".join(b.get("text", "") for b in data.get("content", []))
    logger.info(f"Raw response: {raw[:600]}")

    clean = raw.strip()
    clean = re.sub(r"```[a-zA-Z]*", "", clean).replace("```", "").strip()
    start = clean.find("{")
    end   = clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in response")
    return json.loads(clean[start:end])


def escape_md(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def format_response(d: dict, reporter: str, date_str: str) -> str:
    sent_icon = SENTIMENT_ICONS.get(d.get("sentiment", "Mixed"), "🟡")
    pri_icon  = PRIORITY_ICONS.get(d.get("priority", "Medium"), "🟡")

    lines = [
        "*📋 Field Insights Report*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 *Reporter:* {escape_md(reporter or '—')}",
        f"📅 *Date:* {escape_md(date_str)}",
        f"📊 *Sentiment:* {sent_icon} {escape_md(d.get('sentiment', '—'))}   |   *Priority:* {pri_icon} {escape_md(d.get('priority', '—'))}",
    ]
    if d.get("units_sold") not in (None, "null", "", "None"):
        lines.append(f"🛒 *Units sold:* {escape_md(str(d['units_sold']))}")

    lines += ["", "*📝 Summary*", f"_{escape_md(d.get('summary', '—'))}_"]

    for key, (icon, label) in CATEGORY_ICONS.items():
        items = d.get(key, [])
        if items:
            lines += ["", f"{icon} *{escape_md(label)}*"]
            for item in items:
                lines.append(f"• {escape_md(item)}")

    actions = d.get("actions", [])
    if actions:
        lines += ["", "⚡ *Actions Required*"]
        for a in actions:
            urg = a.get("urgency", "Soon")
            lines.append(f"{URGENCY_ICONS.get(urg, '🟡')} _{escape_md(urg)}_ — {escape_md(a.get('action', ''))}")

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

    # Telegram sends photo+text as a media group — the caption holds the text
    notes = message.text or message.caption or ""

    # If this is part of a media group (multiple photos) with no text, skip silently
    # Only process the message that actually carries the caption/text
    if not notes.strip():
        if getattr(message, "media_group_id", None):
            return  # Other photos in the same group — ignore
        await message.reply_text(
            "⚠️ No text found in this forwarded message\\. Make sure the original message contains text or a caption\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    reporter   = get_reporter_name(message)
    date_str   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    processing = await message.reply_text("⏳ Extracting insights\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    try:
        data     = await call_claude(notes, reporter)
        response = format_response(data, reporter, date_str)
        await processing.edit_text(response, parse_mode=ParseMode.MARKDOWN_V2)
    except json.JSONDecodeError as e:
        logger.error(f"JSON error: {e}")
        await processing.edit_text("⚠️ Could not parse AI response\\. Please try forwarding again\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.error(f"Error: {e}")
        await processing.edit_text(f"⚠️ Error: {escape_md(str(e)[:200])}", parse_mode=ParseMode.MARKDOWN_V2)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Field Insights Bot is active\\!*\n\n"
        "Simply *forward* any store visit update into this chat\\.\n"
        "Works with text messages and photo messages with captions\\.\n\n"
        "*What I extract:*\n"
        "📈 Sales • 👀 Competitors • 💬 Customer feedback\n"
        "📦 Stock & display • 🙋 Staff • 🎯 Promo effectiveness\n"
        "⚡ Actions required \\(Urgent / Soon / Monitor\\)",
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*📖 How to use*\n\n"
        "Forward any store visit update into this group\\.\n"
        "Works with plain text or photos with captions\\.\n\n"
        "The bot auto\\-detects forwarded messages and extracts insights immediately\\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.CAPTION),
        handle_forwarded
    ))
    logger.info("Bot is running — listening for forwarded messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
