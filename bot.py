import os, json, logging, re
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY      = os.environ["ANTHROPIC_API_KEY"].strip()
MANAGEMENT_CHAT_ID = int(os.environ["MANAGEMENT_CHAT_ID"])
SG_TOPIC_ID        = int(os.environ.get("SG_TOPIC_ID", "0"))
SGT                = ZoneInfo("Asia/Singapore")

insights_store = []
MIN_MESSAGE_LENGTH = 80

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
    "our_shelf_score": 5,
    "competitor_pressure": "High/Medium/Low",
    "customer_preference_signal": "Positive/Neutral/Negative",
    "staff_advocacy_score": 5,
    "display_quality_score": 5,
    "overall_share_index": 5,
    "share_trend": "Gaining/Holding/Losing",
    "notes": "brief explanation"
  }
}

Scoring: our_shelf_score 1=hidden 10=dominant, staff_advocacy_score 1=ignoring us 10=actively recommending,
display_quality_score 1=damaged/missing 10=perfect placement. Use 5 as neutral default if insufficient info.
Only populate competitor_bench with brands actually mentioned. Empty arrays [] are fine. Never truncate."""

DAILY_PROMPT = """You are a retail field insights analyst. Analyse today's store visit reports and produce a daily digest.

Return ONLY a valid JSON object:
{
  "date": "today's date",
  "daily_summary": "2-3 sentence overview of today",
  "total_units_sold": "total number or null",
  "overall_sentiment": "Positive/Mixed/Negative",
  "stores_visited": ["store 1", "store 2"],
  "top_wins": ["win 1", "win 2"],
  "issues_flagged": ["issue 1", "issue 2"],
  "competitor_activity": ["observation 1", "observation 2"],
  "urgent_actions": [{"action": "what to do", "store": "which store", "urgency": "Urgent/Soon/Monitor"}],
  "share_trend_today": "Gaining/Holding/Losing",
  "avg_share_index": "average 1-10"
}"""

WEEKLY_PROMPT = """You are a retail field insights analyst. Analyse this week's store visit reports and produce a weekly rollup.

Return ONLY a valid JSON object:
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
💡 <i>Add any extra observations freely below!</i>"""

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


def is_correct_topic(message) -> bool:
    if SG_TOPIC_ID == 0:
        return True
    return getattr(message, "message_thread_id", None) == SG_TOPIC_ID


def is_store_update(text: str) -> bool:
    if len(text) < MIN_MESSAGE_LENGTH:
        return False
    keywords = [
        "outlet", "store", "units", "sold", "stock", "display",
        "competitor", "bose", "jbl", "samsung", "follow up", "good news",
        "shelf", "promo", "customer", "staff", "harvey norman", "challenger",
        "courts", "takashimaya", "airport", "millenia", "clocked in", "sva",
        "brand execution", "engagement", "buzz plan", "train", "insights",
        "sprintcass", "bowers", "marshall", "sonos", "b&o", "sennheiser"
    ]
    return sum(1 for kw in keywords if kw in text.lower()) >= 2


async def call_claude(prompt: str, user_msg: str, model: str) -> dict:
    logger.info(f"Calling model: {model}")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 2048, "system": prompt,
                  "messages": [{"role": "user", "content": user_msg}]}
        )
    logger.info(f"Anthropic status: {r.status_code}")
    if r.status_code != 200:
        raise ValueError(f"ANTHROPIC_ERROR_{r.status_code}: {r.text[:500]}")
    raw   = "".join(b.get("text", "") for b in r.json().get("content", []))
    clean = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
    start, end = clean.find("{"), clean.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON in response")
    return json.loads(clean[start:end])


def format_share_bar(score) -> str:
    score = max(1, min(10, int(score)))
    filled = round(score / 10 * 8)
    return "█" * filled + "░" * (8 - filled) + f" {score}/10"


def extract_store_name(notes: str) -> str:
    for line in notes.splitlines():
        low = line.lower()
        if "outlet" in low or "store" in low:
            parts = line.split(":", 1)
            if len(parts) > 1 and len(parts[1].strip()) > 1:
                return parts[1].strip()
    return "Unknown Store"


def format_daily(d: dict, count: int, date_str: str) -> str:
    sent_icon  = SENTIMENT_ICONS.get(d.get("overall_sentiment", "Mixed"), "🟡")
    trend_icon = TREND_ICONS.get(d.get("share_trend_today", "Holding"), "➡️")
    lines = [
        "📋 <b>Daily Field Insights Digest</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Date:</b> {esc(date_str)}",
        f"📊 <b>Reports received:</b> {count}",
        f"📊 <b>Overall sentiment:</b> {sent_icon} {esc(d.get('overall_sentiment','—'))}",
    ]
    if d.get("total_units_sold") not in (None, "null", "", "None"):
        lines.append(f"🛒 <b>Total units sold:</b> {esc(str(d['total_units_sold']))}")
    if d.get("stores_visited"):
        lines.append(f"🏪 <b>Stores visited:</b> {esc(', '.join(d['stores_visited']))}")
    lines += ["", "📝 <b>Today's Summary</b>", f"<i>{esc(d.get('daily_summary','—'))}</i>"]
    if d.get("top_wins"):
        lines += ["", "🏆 <b>Today's Wins</b>"]
        for w in d["top_wins"]: lines.append(f"✅ {esc(w)}")
    if d.get("issues_flagged"):
        lines += ["", "⚠️ <b>Issues Flagged</b>"]
        for i in d["issues_flagged"]: lines.append(f"• {esc(i)}")
    if d.get("competitor_activity"):
        lines += ["", "⚔️ <b>Competitor Activity</b>"]
        for c in d["competitor_activity"]: lines.append(f"👀 {esc(c)}")
    lines += ["", f"📈 <b>Share Trend Today:</b> {trend_icon} {esc(d.get('share_trend_today','—'))}  |  <b>Avg Index:</b> {esc(str(d.get('avg_share_index','—')))}/10"]
    if d.get("urgent_actions"):
        lines += ["", "⚡ <b>Actions Required</b>"]
        for a in d["urgent_actions"]:
            urg = a.get("urgency", "Soon")
            store = f" [{esc(a.get('store',''))}]" if a.get("store") else ""
            lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i>{store} — {esc(a.get('action',''))}")
    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def format_weekly(d: dict, count: int, date_str: str) -> str:
    sent_icon  = SENTIMENT_ICONS.get(d.get("overall_sentiment", "Mixed"), "🟡")
    trend_icon = TREND_ICONS.get(d.get("share_trend", "Holding"), "➡️")
    lines = [
        "📊 <b>Weekly Field Insights Rollup</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Generated:</b> {esc(date_str)}",
        f"📋 <b>Reports analysed:</b> {count}",
        f"📊 <b>Overall sentiment:</b> {sent_icon} {esc(d.get('overall_sentiment','—'))}",
    ]
    if d.get("total_units_sold") not in (None, "null", "", "None"):
        lines.append(f"🛒 <b>Total units sold:</b> {esc(str(d['total_units_sold']))}")
    lines += ["", "📝 <b>Week Summary</b>", f"<i>{esc(d.get('week_summary','—'))}</i>"]
    if d.get("top_wins"):
        lines += ["", "🏆 <b>Top Wins</b>"]
        for w in d["top_wins"]: lines.append(f"✅ {esc(w)}")
    if d.get("recurring_issues"):
        lines += ["", "⚠️ <b>Recurring Issues</b>"]
        for i in d["recurring_issues"]: lines.append(f"• {esc(i)}")
    if d.get("competitor_threats"):
        lines += ["", "⚔️ <b>Competitor Threats</b>"]
        for t in d["competitor_threats"]: lines.append(f"🔴 {esc(t)}")
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
            urg = a.get("urgency","Soon")
            stores = f" ({esc(a.get('stores_affected',''))})" if a.get("stores_affected") else ""
            lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i> — {esc(a.get('action',''))}{stores}")
    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


async def send_daily_digest(context) -> None:
    """Scheduled job — sends daily digest to management group at 9pm SGT."""
    now_sgt  = datetime.now(SGT)
    today    = now_sgt.strftime("%d %b %Y")
    cutoff   = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_reports = [
        i for i in insights_store
        if datetime.fromisoformat(i["timestamp"]).astimezone(SGT) >= cutoff
    ]
    if not today_reports:
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text=f"📋 <b>Daily Digest — {esc(today)}</b>\n\nNo store visit updates received today.",
            parse_mode=ParseMode.HTML
        )
        return

    model        = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    reports_text = "\n".join(
        f"--- {r['store']} by {r['reporter']} ---\n{r['notes']}"
        for r in today_reports
    )
    try:
        data     = await call_claude(DAILY_PROMPT, reports_text, model)
        response = format_daily(data, len(today_reports), today)
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text=response,
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Daily digest sent — {len(today_reports)} reports")
    except Exception as e:
        logger.error(f"Daily digest error: {e}")
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text=f"⚠️ Could not generate daily digest: {str(e)[:200]}",
            parse_mode=ParseMode.HTML
        )


async def send_weekly_rollup(context) -> None:
    """Scheduled job — sends weekly rollup to management group every Saturday 10am SGT."""
    now_sgt  = datetime.now(SGT)
    cutoff   = now_sgt - timedelta(days=7)
    week_reports = [
        i for i in insights_store
        if datetime.fromisoformat(i["timestamp"]).astimezone(SGT) >= cutoff
    ]
    date_str = now_sgt.strftime("%d %b %Y, %I:%M %p")
    if not week_reports:
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text="📊 <b>Weekly Rollup</b>\n\nNo store visit updates received this week.",
            parse_mode=ParseMode.HTML
        )
        return

    model        = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    reports_text = "\n".join(
        f"--- {r['store']} by {r['reporter']} on {r['date']} ---\n{r['notes']}"
        for r in week_reports
    )
    try:
        data     = await call_claude(WEEKLY_PROMPT, reports_text, model)
        response = format_weekly(data, len(week_reports), date_str)
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text=response,
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Weekly rollup sent — {len(week_reports)} reports")
    except Exception as e:
        logger.error(f"Weekly rollup error: {e}")
        await context.bot.send_message(
            chat_id=MANAGEMENT_CHAT_ID,
            text=f"⚠️ Could not generate weekly rollup: {str(e)[:200]}",
            parse_mode=ParseMode.HTML
        )


async def handle_cm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    if not is_correct_topic(message):
        return

    notes = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None):
            return
        return
    if not is_store_update(notes):
        return

    reporter = message.from_user.full_name if message.from_user else "Unknown"
    store    = extract_store_name(notes)
    date_str = datetime.now(SGT).strftime("%d %b %Y, %I:%M %p")
    model    = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()

    ack = await message.reply_text("⏳ Logging update...")

    try:
        user_msg = f"Reporter: {reporter}\nStore: {store}\n\nStore visit notes:\n{notes}"
        data     = await call_claude(SYSTEM_PROMPT, user_msg, model)

        insights_store.append({
            "reporter": reporter, "store": store, "date": date_str,
            "timestamp": datetime.now(SGT).isoformat(), "data": data, "notes": notes
        })

        await ack.edit_text(
            f"✅ Update logged from <b>{esc(reporter)}</b> — <i>{esc(store)}</i>\n"
            f"Daily digest will be sent at 9pm SGT.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Error: {e}")
        await ack.edit_text("⚠️ Could not log this update. Please try again.")


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual forwarding in management group — analyses immediately."""
    message = update.message
    notes   = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None):
            return
        return

    reporter   = message.from_user.full_name if message.from_user else "Unknown"
    store      = extract_store_name(notes)
    date_str   = datetime.now(SGT).strftime("%d %b %Y, %I:%M %p")
    model      = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    processing = await message.reply_text("⏳ Extracting insights...")

    try:
        user_msg = f"Reporter: {reporter}\nStore: {store}\n\nStore visit notes:\n{notes}"
        data     = await call_claude(SYSTEM_PROMPT, user_msg, model)

        # Also save to store for daily/weekly digest
        insights_store.append({
            "reporter": reporter, "store": store, "date": date_str,
            "timestamp": datetime.now(SGT).isoformat(), "data": data, "notes": notes
        })

        # Format and send full report for manual forwards
        from telegram.constants import ParseMode as PM
        sent_icon = SENTIMENT_ICONS.get(data.get("sentiment", "Mixed"), "🟡")
        pri_icon  = PRIORITY_ICONS.get(data.get("priority",  "Medium"), "🟡")
        lines = [
            "📋 <b>Field Insights Report</b>",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 <b>Reporter:</b> {esc(reporter)}",
            f"🏪 <b>Store:</b> {esc(store)}",
            f"📅 <b>Date:</b> {esc(date_str)}",
            f"📊 <b>Sentiment:</b> {sent_icon} {esc(data.get('sentiment','—'))}   |   <b>Priority:</b> {pri_icon} {esc(data.get('priority','—'))}",
        ]
        if data.get("units_sold") not in (None, "null", "", "None"):
            lines.append(f"🛒 <b>Units sold:</b> {esc(str(data['units_sold']))}")
        lines += ["", "📝 <b>Summary</b>", f"<i>{esc(data.get('summary','—'))}</i>"]
        for key, (icon, label) in CATEGORY_ICONS.items():
            items = data.get(key, [])
            if items:
                lines += ["", f"{icon} <b>{label}</b>"]
                for item in items: lines.append(f"• {esc(item)}")
        bench = data.get("competitor_bench", [])
        if bench:
            lines += ["", "⚔️ <b>Competitor Benchmarking</b>"]
            for c in bench:
                threat = c.get("threat_level", "Low")
                lines.append(
                    f"{THREAT_COLORS.get(threat,'🟢')} <b>{esc(c.get('brand','?'))}</b> — "
                    f"Shelf: {esc(c.get('shelf_space','?'))} | "
                    f"Price: {esc(c.get('price_position','?'))} | "
                    f"Promo: {esc(c.get('promo_activity','None'))}"
                )
        msp = data.get("market_share_proxy", {})
        if msp:
            trend = msp.get("share_trend", "Holding")
            lines += ["", "📊 <b>Market Share Proxy</b>"]
            lines.append(f"{TREND_ICONS.get(trend,'➡️')} <b>Trend:</b> {esc(trend)}")
            lines.append(f"⭐ Overall index: <code>{format_share_bar(msp.get('overall_share_index',5))}</code>")
        actions = data.get("actions", [])
        if actions:
            lines += ["", "⚡ <b>Actions Required</b>"]
            for a in actions:
                urg = a.get("urgency","Soon")
                lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} <i>{esc(urg)}</i> — {esc(a.get('action',''))}")
        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
        await processing.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error: {e}")
        await processing.edit_text(f"⚠️ Error: {str(e)[:200]}")


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """On-demand daily digest."""
    await update.message.reply_text("⏳ Generating today's digest...")
    await send_daily_digest(context)


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """On-demand weekly rollup."""
    await update.message.reply_text("⏳ Generating weekly rollup...")
    await send_weekly_rollup(context)


async def topic_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message   = update.message
    thread_id = getattr(message, "message_thread_id", None)
    chat_id   = message.chat_id
    if thread_id:
        await message.reply_text(
            f"📌 <b>Topic ID found!</b>\n\n"
            f"<b>Chat ID:</b> <code>{chat_id}</code>\n"
            f"<b>Topic thread ID:</b> <code>{thread_id}</code>\n\n"
            f"Add to Render as: <code>SG_TOPIC_ID = {thread_id}</code>",
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply_text(
            f"ℹ️ Not inside a topic.\n<b>Chat ID:</b> <code>{chat_id}</code>",
            parse_mode=ParseMode.HTML
        )


async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"<b>Chat ID:</b> <code>{update.message.chat_id}</code>",
        parse_mode=ParseMode.HTML
    )


async def template_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(CM_TEMPLATE, parse_mode=ParseMode.HTML)


async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Checking API...")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://api.anthropic.com/v1/models",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"})
        if r.status_code == 200:
            models = [m["id"] for m in r.json().get("data", [])]
            msg = "✅ <b>API key valid! Models:</b>\n" + "\n".join(f"• <code>{m}</code>" for m in models)
        else:
            msg = f"❌ {r.status_code}: <code>{esc(r.text[:200])}</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic_status = f"Monitoring topic ID: <code>{SG_TOPIC_ID}</code>" if SG_TOPIC_ID else "⚠️ SG_TOPIC_ID not set"
    await update.message.reply_text(
        "👋 <b>Field Insights Bot is active!</b>\n\n"
        f"📌 {topic_status}\n"
        "⏰ <b>Daily digest:</b> 9pm SGT\n"
        "📅 <b>Weekly rollup:</b> Saturday 10am SGT\n\n"
        "<b>Commands:</b>\n"
        "• /daily — generate today's digest now\n"
        "• /weekly — generate this week's rollup now\n"
        "• /template — get the CM update template\n"
        "• /topicid — get the current topic ID\n"
        "• /chatid — get this group's chat ID\n"
        "• /testapi — check API connection",
        parse_mode=ParseMode.HTML
    )


def main():
    logger.info(f"API key prefix: {ANTHROPIC_KEY[:20]}")
    logger.info(f"Management chat ID: {MANAGEMENT_CHAT_ID}")
    logger.info(f"SG Topic ID: {SG_TOPIC_ID}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # SGT = UTC+8, so 9pm SGT = 13:00 UTC, Saturday 10am SGT = Saturday 02:00 UTC
    app.job_queue.run_daily(
        send_daily_digest,
        time=datetime.strptime("13:00", "%H:%M").time().replace(tzinfo=ZoneInfo("UTC")),
        name="daily_digest"
    )
    app.job_queue.run_weekly(
        send_weekly_rollup,
        time=datetime.strptime("02:00", "%H:%M").time().replace(tzinfo=ZoneInfo("UTC")),
        day=5,  # 0=Monday, 5=Saturday
        name="weekly_rollup"
    )

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("testapi",  test_api))
    app.add_handler(CommandHandler("daily",    cmd_daily))
    app.add_handler(CommandHandler("weekly",   cmd_weekly))
    app.add_handler(CommandHandler("template", template_command))
    app.add_handler(CommandHandler("chatid",   get_chat_id))
    app.add_handler(CommandHandler("topicid",  topic_id_command))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.CAPTION),
        handle_forwarded
    ))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.CAPTION) & ~filters.COMMAND & ~filters.FORWARDED,
        handle_cm_message
    ))

    logger.info("Bot running — daily digest 9pm SGT, weekly rollup Saturday 10am SGT")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
