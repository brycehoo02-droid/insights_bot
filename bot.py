import os, json, logging, re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime, timedelta

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"].strip()

# In-memory store for weekly rollup
insights_store = []

SYSTEM_PROMPT = """You are a retail field insights analyst. Extract structured insights from store visit notes.

You MUST return ONLY a valid JSON object. No explanation, no markdown, no backticks, no text before or after.
Start your response with { and end with }.

Use this exact structure:
{
  "summary": "1-2 sentence executive summary",
  "sentiment": "Positive or Mixed or Negative",
  "priority": "High or Medium or Low",
  "units_sold": "number as string or null",
  "sales": ["insight"],
  "competitor": ["insight"],
  "customer": ["insight"],
  "stock": ["insight"],
  "staff": ["insight"],
  "promo": ["insight"],
  "actions": [{"action": "what to do", "urgency": "Urgent or Soon or Monitor"}],
  "competitor_bench": [
    {
      "brand": "brand name",
      "shelf_space": "More/Same/Less than us",
      "promo_activity": "description or None",
      "price_position": "Higher/Same/Lower than us",
      "staff_engagement": "High/Medium/Low/None",
      "threat_level": "High/Medium/Low"
    }
  ],
  "market_share_proxy": {
    "our_shelf_score": 1-10 integer,
    "competitor_pressure": "High/Medium/Low",
    "customer_preference_signal": "Positive/Neutral/Negative",
    "staff_advocacy_score": 1-10 integer,
    "display_quality_score": 1-10 integer,
    "overall_share_index": 1-10 integer,
    "share_trend": "Gaining/Holding/Losing",
    "notes": "brief explanation"
  }
}

Scoring guide for market_share_proxy:
- our_shelf_score: 1=tiny/hidden, 10=dominant/prime position
- staff_advocacy_score: 1=staff ignoring/misdirecting, 10=staff actively recommending us
- display_quality_score: 1=damaged/missing, 10=perfect/premium placement
- overall_share_index: weighted average of above signals
- If insufficient info to score, use 5 as neutral default
- Only populate competitor_bench with brands actually mentioned in the notes

Rules:
- Only include array items that have real content
- Empty arrays are fine
- Keep insights concise
- Always return valid JSON, never truncate"""

WEEKLY_PROMPT = """You are a retail field insights analyst. Analyse these store visit reports from the past 7 days and produce a weekly rollup.

Return ONLY a valid JSON object with this structure:
{
  "week_summary": "2-3 sentence overview of the week",
  "total_units_sold": "total number or null",
  "overall_sentiment": "Positive/Mixed/Negative",
  "top_wins": ["win 1", "win 2", "win 3"],
  "recurring_issues": ["issue 1", "issue 2"],
  "competitor_threats": ["threat 1", "threat 2"],
  "share_trend": "Gaining/Holding/Losing",
  "avg_share_index": "average score 1-10",
  "top_actions": [{"action": "what to do", "urgency": "Urgent/Soon/Monitor", "stores_affected": "which stores"}],
  "store_performance": [{"store": "store name", "sentiment": "Positive/Mixed/Negative", "units": "number or null", "highlight": "one key point"}]
}"""

CM_TEMPLATE = """📝 <b>Store Visit Update</b>
━━━━━━━━━━━━━━━━━━━━━━━━

<b>Outlet:</b> [Store name]
<b>Date:</b> [DD/MM/YY]

1️⃣ <b>Good News</b>
[What went well today — sales wins, positive customer reactions, staff support, strong display, etc.]

2️⃣ <b>Competitors' Insights</b>
[Brand] — shelf: [More/Same/Less] | price: [Higher/Same/Lower] | promo: [Describe or None]
[Add one line per competitor observed]

3️⃣ <b>Display &amp; Stock</b>
Stock: [Full/Low/Out]  |  Display: [Good/Issue — describe]

4️⃣ <b>What to Follow Up</b>
[Action needed and by when — or None]

━━━━━━━━━━━━━━━━━━━━━━━━
💡 <i>Takes ~2 mins to fill. Add any extra observations freely below!</i>"""

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
TREND_ICONS     = {"Gaining": "📈", "Holding": "➡️", "Losing": "📉"}
THREAT_COLORS   = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def call_claude(prompt: str, user_msg: str, model: str) -> dict:
    logger.info(f"Calling model: {model} | key prefix: {ANTHROPIC_KEY[:12]}...")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 2048, "system": prompt,
                  "messages": [{"role": "user", "content": user_msg}]}
        )
    logger.info(f"Anthropic status: {r.status_code} | response: {r.text[:400]}")
    if r.status_code != 200:
        raise ValueError(f"ANTHROPIC_ERROR_{r.status_code}: {r.text[:500]}")
    raw   = "".join(b.get("text", "") for b in r.json().get("content", []))
    clean = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
    start = clean.find("{")
    end   = clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found in response")
    return json.loads(clean[start:end])


def format_share_bar(score: int) -> str:
    score = max(1, min(10, int(score)))
    filled = round(score / 10 * 8)
    return "█" * filled + "░" * (8 - filled) + f" {score}/10"


def format_response(d: dict, reporter: str, store: str, date_str: str) -> str:
    sent_icon = SENTIMENT_ICONS.get(d.get("sentiment", "Mixed"), "🟡")
    pri_icon  = PRIORITY_ICONS.get(d.get("priority",  "Medium"), "🟡")

    lines = [
        "📋 <b>Field Insights Report</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Reporter:</b> {esc(reporter or '—')}",
        f"🏪 <b>Store:</b> {esc(store or '—')}",
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

    # Competitor benchmarking
    bench = d.get("competitor_bench", [])
    if bench:
        lines += ["", "⚔️ <b>Competitor Benchmarking</b>"]
        for c in bench:
            threat = c.get("threat_level", "Low")
            lines.append(
                f"{THREAT_COLORS.get(threat,'🟢')} <b>{esc(c.get('brand','?'))}</b> — "
                f"Shelf: {esc(c.get('shelf_space','?'))} | "
                f"Price: {esc(c.get('price_position','?'))} | "
                f"Staff: {esc(c.get('staff_engagement','?'))} | "
                f"Promo: {esc(c.get('promo_activity','None'))}"
            )

    # Market share proxy
    msp = d.get("market_share_proxy", {})
    if msp:
        trend = msp.get("share_trend", "Holding")
        lines += ["", "📊 <b>Market Share Proxy</b>"]
        lines.append(f"{TREND_ICONS.get(trend,'➡️')} <b>Trend:</b> {esc(trend)}  |  <b>Competitor Pressure:</b> {esc(msp.get('competitor_pressure','—'))}")
        lines.append(f"🏪 Shelf score:      <code>{format_share_bar(msp.get('our_shelf_score', 5))}</code>")
        lines.append(f"🙋 Staff advocacy:   <code>{format_share_bar(msp.get('staff_advocacy_score', 5))}</code>")
        lines.append(f"🎯 Display quality:  <code>{format_share_bar(msp.get('display_quality_score', 5))}</code>")
        lines.append(f"⭐ Overall index:    <code>{format_share_bar(msp.get('overall_share_index', 5))}</code>")
        if msp.get("notes"):
            lines.append(f"💡 <i>{esc(msp['notes'])}</i>")

    # Actions
    actions = d.get("actions", [])
    if actions:
        lines += ["", "⚡ <b>Actions Required</b>"]
        for a in actions:
            urg = a.get("urgency", "Soon")
            lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i> — {esc(a.get('action',''))}")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_weekly(d: dict, report_count: int, date_str: str) -> str:
    sent_icon  = SENTIMENT_ICONS.get(d.get("overall_sentiment", "Mixed"), "🟡")
    trend_icon = TREND_ICONS.get(d.get("share_trend", "Holding"), "➡️")

    lines = [
        "📊 <b>Weekly Field Insights Rollup</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Generated:</b> {esc(date_str)}",
        f"📋 <b>Reports analysed:</b> {report_count}",
        f"📊 <b>Overall sentiment:</b> {sent_icon} {esc(d.get('overall_sentiment','—'))}",
    ]
    if d.get("total_units_sold") not in (None, "null", "", "None"):
        lines.append(f"🛒 <b>Total units sold:</b> {esc(str(d['total_units_sold']))}")

    lines += ["", f"📝 <b>Week Summary</b>", f"<i>{esc(d.get('week_summary','—'))}</i>"]

    if d.get("top_wins"):
        lines += ["", "🏆 <b>Top Wins</b>"]
        for w in d["top_wins"]:
            lines.append(f"✅ {esc(w)}")

    if d.get("recurring_issues"):
        lines += ["", "⚠️ <b>Recurring Issues</b>"]
        for i in d["recurring_issues"]:
            lines.append(f"• {esc(i)}")

    if d.get("competitor_threats"):
        lines += ["", "⚔️ <b>Competitor Threats</b>"]
        for t in d["competitor_threats"]:
            lines.append(f"🔴 {esc(t)}")

    lines += ["", f"📈 <b>Market Share Trend:</b> {trend_icon} {esc(d.get('share_trend','—'))}  |  <b>Avg Index:</b> {esc(str(d.get('avg_share_index','—')))}/10"]

    if d.get("store_performance"):
        lines += ["", "🏪 <b>Store Performance</b>"]
        for s in d["store_performance"]:
            si = SENTIMENT_ICONS.get(s.get("sentiment","Mixed"),"🟡")
            units = f" • {esc(str(s['units']))} units" if s.get("units") not in (None,"null","","None") else ""
            lines.append(f"{si} <b>{esc(s.get('store','?'))}</b>{units}")
            lines.append(f"   <i>{esc(s.get('highlight',''))}</i>")

    if d.get("top_actions"):
        lines += ["", "⚡ <b>Priority Actions This Week</b>"]
        for a in d["top_actions"]:
            urg = a.get("urgency", "Soon")
            stores = f" ({esc(a.get('stores_affected',''))})" if a.get("stores_affected") else ""
            lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i> — {esc(a.get('action',''))}{stores}")

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


def extract_store_name(notes: str) -> str:
    for line in notes.splitlines():
        low = line.lower()
        if "outlet" in low or "store" in low:
            parts = line.split(":", 1)
            if len(parts) > 1:
                return parts[1].strip()
    return "Unknown Store"


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    notes   = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None):
            return
        await message.reply_text("⚠️ No text found in this forwarded message.")
        return

    reporter   = get_reporter_name(message)
    store      = extract_store_name(notes)
    date_str   = datetime.now().strftime("%d %b %Y, %I:%M %p")
    model      = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    processing = await message.reply_text("⏳ Extracting insights...")

    try:
        user_msg = f"Reporter: {reporter}\nStore: {store}\n\nStore visit notes:\n{notes}"
        data     = await call_claude(SYSTEM_PROMPT, user_msg, model)
        response = format_response(data, reporter, store, date_str)

        # Store for weekly rollup
        insights_store.append({
            "reporter": reporter, "store": store, "date": date_str,
            "timestamp": datetime.now().isoformat(), "data": data, "notes": notes
        })

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


async def weekly_rollup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cutoff  = datetime.now() - timedelta(days=7)
    recent  = [i for i in insights_store
               if datetime.fromisoformat(i["timestamp"]) >= cutoff]

    if not recent:
        await update.message.reply_text(
            "📭 <b>No reports this week yet.</b>\n\nForward store visit updates into this chat and they'll be included in the weekly rollup.",
            parse_mode=ParseMode.HTML
        )
        return

    processing = await update.message.reply_text(
        f"⏳ Generating weekly rollup from {len(recent)} report(s)..."
    )
    model    = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    reports_text = ""
    for i, r in enumerate(recent, 1):
        reports_text += f"\n--- Report {i}: {r['store']} by {r['reporter']} on {r['date']} ---\n{r['notes']}\n"

    try:
        data     = await call_claude(WEEKLY_PROMPT, reports_text, model)
        response = format_weekly(data, len(recent), date_str)
        await processing.edit_text(response, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Weekly rollup error: {e}")
        await processing.edit_text(f"⚠️ Error generating rollup: {str(e)[:200]}")


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CM_TEMPLATE, parse_mode=ParseMode.HTML)


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
            msg = "✅ <b>API key valid! Available models:</b>\n" + "\n".join(f"• <code>{m}</code>" for m in models)
        else:
            msg = f"❌ API error {r.status_code}:\n<code>{esc(r.text[:300])}</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:200]}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Field Insights Bot is active!</b>\n\n"
        "<b>How to use:</b>\n"
        "• <b>Forward</b> any store visit update → auto extracts insights\n"
        "• /template — get the CM update template\n"
        "• /weekly — generate this week's rollup report\n"
        "• /testapi — check API connection\n\n"
        "<b>What I extract:</b>\n"
        "📈 Sales • 👀 Competitors • 💬 Customer feedback\n"
        "📦 Stock &amp; display • 🙋 Staff • 🎯 Promo effectiveness\n"
        "⚔️ Competitor benchmarking • 📊 Market share proxy\n"
        "⚡ Prioritised actions",
        parse_mode=ParseMode.HTML
    )


def main():
    logger.info(f"Loaded API key prefix: {ANTHROPIC_KEY[:20]}")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("testapi",  test_api))
    app.add_handler(CommandHandler("weekly",   weekly_rollup))
    app.add_handler(CommandHandler("template", template_command))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.CAPTION),
        handle_forwarded
    ))
    logger.info("Bot is running — listening for forwarded messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
