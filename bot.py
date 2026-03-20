import os, json, logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

SYSTEM_PROMPT = """You are a retail field insights analyst. Extract structured insights from store visit notes written by channel managers or promoters.

Return ONLY valid JSON (no markdown, no backticks, no preamble) in this exact structure:
{
  "summary": "1-2 sentence executive summary",
  "sentiment": "Positive|Mixed|Negative",
  "priority": "High|Medium|Low",
  "units_sold": "number as string, or null",
  "sales": ["insight"],
  "competitor": ["insight"],
  "customer": ["insight"],
  "stock": ["insight"],
  "staff": ["insight"],
  "promo": ["insight"],
  "actions": [{"action": "what to do", "urgency": "Urgent|Soon|Monitor"}]
}
Only populate arrays that have actual content. Empty arrays are fine."""

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
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000,
                  "system": SYSTEM_PROMPT,
                  "messages": [{"role": "user", "content": user_msg}]}
        )
        data = r.json()
    raw   = "".join(b.get("text", "") for b in data.get("content", []))
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)


def format_response(d: dict, reporter: str, date_str: str) -> str:
    sent_icon = SENTIMENT_ICONS.get(d.get("sentiment", "Mixed"), "🟡")
    pri_icon  = PRIORITY_ICONS.get(d.get("priority", "Medium"), "🟡")

    lines = [
        "*📋 Field Insights Report*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 *Reporter:* {reporter or '—'}",
        f"📅 *Date:* {date_str}",
        f"📊 *Sentiment:* {sent_icon} {d.get('sentiment', '—')}   |   *Priority:* {pri_icon} {d.get('priority', '—')}",
    ]
    if d.get("units_sold") not in (None, "null", ""):
        lines.append(f"🛒 *Units sold:* {d['units_sold']}")

    lines += ["", "*📝 Summary*", f"_{d.get('summary', '—')}_"]

    for key, (icon, label) in CATEGORY_ICONS.items():
        items = d.get(key, [])
        if items:
            lines += ["", f"{icon} *{label}*"]
            for item in items:
                lines.append(f"• {item}")

    actions = d.get("actions", [])
    if actions:
        lines += ["", "⚡ *Actions Required*"]
        for a in actions:
            urg = a.get("urgency", "Soon")
            lines.append(f"{URGENCY_ICONS.get(urg, '🟡')} _{urg}_ — {a.get('action', '')}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def get_forwarder_name(message) -> str:
    """Name of the person who forwarded the message into the group."""
    if message.from_user:
        return message.from_user.full_name
    return "Unknown"


def get_original_sender_name(message) -> str:
    """Best-effort name of who originally wrote the update."""
    # Forward from a Telegram user
    if message.forward_origin:
        origin = message.forward_origin
        # MessageOriginUser
        if hasattr(origin, "sender_user") and origin.sender_user:
            return origin.sender_user.full_name
        # MessageOriginHiddenUser (privacy mode)
        if hasattr(origin, "sender_user_name") and origin.sender_user_name:
            return origin.sender_user_name
        # MessageOriginChat / Channel
        if hasattr(origin, "sender_chat") and origin.sender_chat:
            return origin.sender_chat.title or origin.sender_chat.username or ""
    return ""


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    notes   = message.text or message.caption or ""
    if not notes.strip():
        return

    original  = get_original_sender_name(message)
    forwarder = get_forwarder_name(message)
    # Show original author if available, otherwise the person who forwarded
    reporter  = original if original else forwarder
    date_str  = datetime.now().strftime("%d %b %Y, %I:%M %p")

    processing = await message.reply_text("⏳ Extracting insights\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

    try:
        data     = await call_claude(notes, reporter)
        response = format_response(data, reporter, date_str)
        await processing.edit_text(response, parse_mode=ParseMode.MARKDOWN)
    except json.JSONDecodeError:
        await processing.edit_text("⚠️ Could not parse insights. Please try again.")
    except Exception as e:
        logger.error(f"Error: {e}")
        await processing.edit_text("⚠️ Something went wrong. Please try again in a moment.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Field Insights Bot is active\\!*\n\n"
        "Simply *forward* any store visit update into this chat and I will automatically extract the key insights\\.\n\n"
        "*What I extract:*\n"
        "📈 Sales • 👀 Competitors • 💬 Customer feedback\n"
        "📦 Stock & display • 🙋 Staff • 🎯 Promo effectiveness\n"
        "⚡ Actions required \\(Urgent / Soon / Monitor\\)\n\n"
        "No commands needed — just forward and I'll handle the rest\\!"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*📖 How to use the Insights Bot*\n\n"
        "1\\. A Channel Manager sends their store visit update \\(WhatsApp, SMS, typed message, etc\\.\\)\n"
        "2\\. *Forward that message* into this Telegram group\n"
        "3\\. The bot automatically replies with structured insights\n\n"
        "*That's it — no commands needed\\!*\n\n"
        "*What gets extracted:*\n"
        "• Executive summary \\+ sentiment \\+ priority\n"
        "• Units sold\n"
        "• Sales performance\n"
        "• Competitor activity\n"
        "• Customer feedback\n"
        "• Stock \\& display issues\n"
        "• Staff feedback\n"
        "• Promo effectiveness\n"
        "• Actions required with urgency tags"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN_V2)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    # Trigger on any forwarded message that contains text
    app.add_handler(MessageHandler(filters.FORWARDED & (filters.TEXT | filters.CAPTION), handle_forwarded))
    logger.info("Bot is running — listening for forwarded messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
