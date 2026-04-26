import os, json, logging, re
from collections import defaultdict
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY      = os.environ["ANTHROPIC_API_KEY"].strip()
MANAGEMENT_CHAT_ID = int(os.environ["MANAGEMENT_CHAT_ID"])
SG_TOPIC_ID        = int(os.environ.get("SG_TOPIC_ID", "0"))
GOOGLE_SHEET_ID    = os.environ.get("GOOGLE_SHEET_ID", "")
SGT                = ZoneInfo("Asia/Singapore")

# ── Google Sheets ──────────────────────────────────────────────────────────────
def get_sheet():
    try:
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS", ""))
        scopes     = ["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
        creds      = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client     = gspread.authorize(creds)
        return client.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        logger.error(f"Google Sheets connection error: {e}")
        return None

def ensure_sheet_headers(worksheet):
    headers = worksheet.row_values(1)
    if not headers:
        worksheet.append_row([
            "Date", "Reporter", "Store", "Retailer",
            "Sentiment", "Priority", "Units Sold", "Summary",
            "✅ Wins / Good News", "👀 Competitors",
            "💬 Customers", "📦 Stock & Display",
            "🙋 Staff", "⚡ Actions"
        ], value_input_option="RAW")
        worksheet.format("A1:N1", {
            "backgroundColor": {"red": 0.1, "green": 0.1, "blue": 0.18},
            "textFormat": {"bold": True, "fontSize": 11,
                           "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE"
        })
        col_widths = [160, 130, 160, 130, 100, 90, 90, 280, 250, 250, 250, 220, 200, 260]
        worksheet.spreadsheet.batch_update({"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": worksheet.id, "dimension": "COLUMNS",
                          "startIndex": i, "endIndex": i+1},
                "properties": {"pixelSize": w}, "fields": "pixelSize"
            }} for i, w in enumerate(col_widths)
        ]})
        worksheet.spreadsheet.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": worksheet.id,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"
            }
        }]})

def write_to_sheet(data: dict, reporter: str, store: str, retailer: str,
                   date_str: str, notes: str):
    try:
        sheet_obj = get_sheet()
        if not sheet_obj: return
        try:
            ws = sheet_obj.worksheet("Store Visits")
        except gspread.WorksheetNotFound:
            ws = sheet_obj.add_worksheet(title="Store Visits", rows=2000, cols=14)
        ensure_sheet_headers(ws)

        def join_list(key):
            items = data.get(key, [])
            return "\n".join(f"• {item}" for item in items) if items else ""

        def format_actions(actions):
            if not actions: return ""
            icons = {"Urgent": "🔴 ", "Soon": "🟡 ", "Monitor": "🟢 "}
            return "\n".join(f"{icons.get(a.get('urgency','Soon'),'• ')}{a.get('action','')}" for a in actions)

        def format_competitors(bench):
            if not bench: return ""
            threat_icon = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}
            lines = []
            for c in bench:
                icon = threat_icon.get(c.get("threat_level","Low"), "•")
                lines.append(f"{icon} {c.get('brand','?')}\n"
                    f"   Shelf: {c.get('shelf_space','?')} | Price: {c.get('price_position','?')} | "
                    f"Promo: {c.get('promo_activity','None')}")
            return "\n".join(lines)

        comp_text  = join_list("competitor")
        bench_text = format_competitors(data.get("competitor_bench", []))
        competitors = "\n".join(filter(None, [comp_text, bench_text]))

        row = [
            date_str, reporter, store, retailer,
            data.get("sentiment", ""), data.get("priority", ""),
            data.get("units_sold", "") or "", data.get("summary", ""),
            join_list("sales"), competitors, join_list("customer"),
            join_list("stock"), join_list("staff"),
            format_actions(data.get("actions", [])),
        ]
        ws.append_row(row, value_input_option="RAW")
        last_row = len(ws.get_all_values())

        row_bg = {"red": 0.97, "green": 0.97, "blue": 1.0} if last_row % 2 == 0 \
                 else {"red": 1.0, "green": 1.0, "blue": 1.0}
        ws.format(f"A{last_row}:N{last_row}", {
            "backgroundColor": row_bg, "verticalAlignment": "TOP",
            "wrapStrategy": "WRAP", "textFormat": {"fontSize": 10}
        })
        sentiment_colors = {
            "Positive": {"red": 0.82, "green": 0.95, "blue": 0.82},
            "Mixed":    {"red": 1.0,  "green": 0.95, "blue": 0.75},
            "Negative": {"red": 0.98, "green": 0.82, "blue": 0.82},
        }
        ws.format(f"E{last_row}", {
            "backgroundColor": sentiment_colors.get(data.get("sentiment",""), row_bg),
            "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "fontSize": 10}
        })
        priority_colors = {
            "High":   {"red": 0.98, "green": 0.82, "blue": 0.82},
            "Medium": {"red": 1.0,  "green": 0.95, "blue": 0.75},
            "Low":    {"red": 0.82, "green": 0.95, "blue": 0.82},
        }
        ws.format(f"F{last_row}", {
            "backgroundColor": priority_colors.get(data.get("priority",""), row_bg),
            "horizontalAlignment": "CENTER", "textFormat": {"bold": True, "fontSize": 10}
        })
        ws.format(f"G{last_row}", {"horizontalAlignment": "CENTER"})
        ws.spreadsheet.batch_update({"requests": [{
            "updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "ROWS",
                          "startIndex": last_row - 1, "endIndex": last_row},
                "properties": {"pixelSize": 90}, "fields": "pixelSize"
            }
        }]})
        logger.info(f"Written to Google Sheets row {last_row}: {store} by {reporter}")
    except Exception as e:
        logger.error(f"Failed to write to Google Sheets: {e}")


# ── Persistent local store ─────────────────────────────────────────────────────
STORE_FILE = "/tmp/insights_store.json"

def load_store():
    try:
        with open(STORE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_store():
    try:
        with open(STORE_FILE, "w") as f:
            json.dump(insights_store, f)
    except Exception as e:
        logger.error(f"Failed to save store: {e}")

insights_store = load_store()
logger.info(f"Loaded {len(insights_store)} reports from disk")

MIN_MESSAGE_LENGTH = 80

# ── Store name normaliser ──────────────────────────────────────────────────────
RETAILER_MAP = {
    "cms": "Courts Megastore", "courts megastore": "Courts Megastore", "courts": "Courts",
    "hn": "Harvey Norman", "harvey norman": "Harvey Norman",
    "hnnp": "Harvey Norman North Point", "hn np": "Harvey Norman North Point", "hn north point": "Harvey Norman North Point",
    "hnmw": "Harvey Norman Millenia Walk", "hn mw": "Harvey Norman Millenia Walk", "hn millenia walk": "Harvey Norman Millenia Walk",
    "hnbv": "Harvey Norman Buona Vista", "hn bv": "Harvey Norman Buona Vista",
    "hntm": "Harvey Norman Tampines Mall", "hn tm": "Harvey Norman Tampines Mall", "hn tampines": "Harvey Norman Tampines Mall",
    "hnjw": "Harvey Norman Jurong West", "hn jw": "Harvey Norman Jurong West",
    "challenger": "Challenger", "chall": "Challenger",
    "sprint-cass": "Sprint-Cass", "sprintcass": "Sprint-Cass", "sprint cass": "Sprint-Cass",
    "sprint-cass e1": "Sprint-Cass E1", "sprint-cass e2": "Sprint-Cass E2", "sprint-cass h1": "Sprint-Cass H1",
    "sprintcass e1": "Sprint-Cass E1", "sprintcass e2": "Sprint-Cass E2", "sprintcass h1": "Sprint-Cass H1",
    "best denki": "Best Denki", "best": "Best Denki", "bd": "Best Denki",
    "taka": "Takashimaya", "takashimaya": "Takashimaya", "tks": "Takashimaya",
    "audio house": "Audio House", "ah": "Audio House",
    "changi airport": "Changi Airport", "airport": "Changi Airport",
    "t1": "Changi Airport T1", "t2": "Changi Airport T2", "t3": "Changi Airport T3",
}

def get_retailer_group(store_name: str) -> str:
    s = store_name.lower()
    if "harvey norman" in s or s.startswith("hn"): return "Harvey Norman"
    if "courts" in s: return "Courts"
    if "challenger" in s: return "Challenger"
    if "sprint" in s or "sprintcass" in s: return "Sprint-Cass"
    if "best denki" in s or s == "bd": return "Best Denki"
    if "takashimaya" in s or "taka" in s: return "Takashimaya"
    if "audio house" in s: return "Audio House"
    if "airport" in s or "changi" in s: return "Changi Airport"
    return store_name

def normalise_store(raw: str) -> str:
    cleaned = raw.strip().rstrip(".,;:")
    lower   = cleaned.lower()
    if lower in RETAILER_MAP: return RETAILER_MAP[lower]
    for key, val in sorted(RETAILER_MAP.items(), key=lambda x: -len(x[0])):
        if key in lower: return val
    return cleaned

def extract_store_name(notes: str) -> str:
    for line in notes.splitlines():
        low = line.lower()
        if "outlet" in low or "store" in low or "outlet name" in low:
            parts = line.split(":", 1)
            if len(parts) > 1 and len(parts[1].strip()) > 1:
                return normalise_store(parts[1].strip())
    return "Unknown Store"

# ── Local aggregation — no API call needed ─────────────────────────────────────
def aggregate_reports(reports: list) -> dict:
    retailer_data = defaultdict(lambda: {
        "stores": set(), "sentiments": [], "units": 0, "units_known": False,
        "wins": [], "issues": [], "competitor_activity": [], "actions": [],
        "share_indices": []
    })
    all_sentiments = []
    total_units    = 0
    units_known    = False

    for r in reports:
        d        = r.get("data", {})
        retailer = r.get("retailer", get_retailer_group(r.get("store","?")))
        rd       = retailer_data[retailer]

        rd["stores"].add(r.get("store","?"))
        sentiment = d.get("sentiment","Mixed")
        rd["sentiments"].append(sentiment)
        all_sentiments.append(sentiment)

        units = d.get("units_sold")
        if units and str(units) not in ("", "null", "None"):
            try:
                u = int(str(units).replace(",",""))
                rd["units"] += u
                rd["units_known"] = True
                total_units += u
                units_known = True
            except ValueError:
                pass

        rd["wins"]               += d.get("sales", [])[:2]
        rd["issues"]             += d.get("stock", [])[:1] + d.get("staff", [])[:1]
        rd["competitor_activity"]+= d.get("competitor", [])[:2]
        rd["actions"]            += d.get("actions", [])[:2]

        msp = d.get("market_share_proxy", {})
        if msp and msp.get("overall_share_index"):
            try: rd["share_indices"].append(int(msp["overall_share_index"]))
            except (ValueError, TypeError): pass

    def majority_sentiment(sentiments):
        if not sentiments: return "Mixed"
        counts = {s: sentiments.count(s) for s in ["Positive","Mixed","Negative"]}
        return max(counts, key=counts.get)

    def avg_share(indices):
        return round(sum(indices)/len(indices)) if indices else 5

    by_retailer = []
    for retailer, rd in retailer_data.items():
        sentiment   = majority_sentiment(rd["sentiments"])
        share_idx   = avg_share(rd["share_indices"])
        share_trend = "Gaining" if share_idx >= 7 else "Losing" if share_idx <= 4 else "Holding"
        urgent      = [a for a in rd["actions"] if a.get("urgency") == "Urgent"]
        other       = [a for a in rd["actions"] if a.get("urgency") != "Urgent"]
        by_retailer.append({
            "retailer":            retailer,
            "stores_visited":      sorted(rd["stores"]),
            "units_sold":          str(rd["units"]) if rd["units_known"] else None,
            "sentiment":           sentiment,
            "wins":                list(dict.fromkeys(rd["wins"]))[:3],
            "issues":              list(dict.fromkeys(rd["issues"]))[:3],
            "competitor_activity": list(dict.fromkeys(rd["competitor_activity"]))[:3],
            "share_index":         share_idx,
            "share_trend":         share_trend,
            "actions":             (urgent + other)[:3],
        })

    sentiment_order = {"Negative": 0, "Mixed": 1, "Positive": 2}
    by_retailer.sort(key=lambda x: sentiment_order.get(x["sentiment"], 1))

    overall_sentiment = majority_sentiment(all_sentiments)
    flat_indices = [i for rd in retailer_data.values() for i in rd["share_indices"]]
    avg_idx      = avg_share(flat_indices)
    overall_trend = "Gaining" if avg_idx >= 7 else "Losing" if avg_idx <= 4 else "Holding"

    seen = set()
    top_urgent = []
    for r in reports:
        for a in r.get("data", {}).get("actions", []):
            if a.get("urgency") == "Urgent":
                key = a.get("action","")[:50]
                if key not in seen:
                    seen.add(key)
                    top_urgent.append({"action": a["action"], "urgency": "Urgent",
                                       "retailer": r.get("retailer","")})

    return {
        "overall_sentiment":   overall_sentiment,
        "overall_share_trend": overall_trend,
        "avg_share_index":     str(avg_idx),
        "total_units_sold":    str(total_units) if units_known else None,
        "by_retailer":         by_retailer,
        "top_urgent_actions":  top_urgent[:5],
        "report_count":        len(reports),
        "retailer_count":      len(retailer_data),
        "stores_visited":      sum(len(rd["stores"]) for rd in retailer_data.values()),
    }


async def get_narrative(bullet_summary: str, model: str, period: str = "week") -> str:
    """Ask Claude for just a short narrative — fast, 30s timeout, 200 tokens max."""
    prompt = (f"You are a retail field insights analyst. Based on these aggregated {period} stats, "
              "write a concise 2-3 sentence executive narrative summary. Be specific and actionable. "
              "Return ONLY the narrative text, no JSON, no bullet points.")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": model, "max_tokens": 200, "system": prompt,
                      "messages": [{"role": "user", "content": bullet_summary}]}
            )
        if r.status_code == 200:
            return "".join(b.get("text","") for b in r.json().get("content",[])).strip()
    except Exception as e:
        logger.error(f"Narrative error: {e}")
    return ""


# ── System prompt (per-message analysis only) ──────────────────────────────────
SYSTEM_PROMPT = """You are a retail field insights analyst. Extract structured insights from store visit notes.

Return ONLY a valid JSON object. Start with { and end with }.

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
    "overall_share_index": 5,
    "share_trend": "Gaining/Holding/Losing"
  }
}

All numeric scores must be integers 1-10. Use 5 as neutral default. Empty arrays [] are fine. Never truncate."""

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
    "sales": ("📈", "Sales Performance"), "competitor": ("👀", "Competitor Activity"),
    "customer": ("💬", "Customer Feedback"), "stock": ("📦", "Stock & Display"),
    "staff": ("🙋", "Staff Feedback"), "promo": ("🎯", "Promo Effectiveness"),
}
URGENCY_ICONS   = {"Urgent": "🔴", "Soon": "🟡", "Monitor": "🟢"}
SENTIMENT_ICONS = {"Positive": "🟢", "Mixed": "🟡", "Negative": "🔴"}
PRIORITY_ICONS  = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}
TREND_ICONS     = {"Gaining": "📈", "Holding": "➡️", "Losing": "📉"}
THREAT_COLORS   = {"High": "🔴", "Medium": "🟡", "Low": "🟢"}


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def format_share_bar(score) -> str:
    try: score = max(1, min(10, int(score)))
    except (TypeError, ValueError): score = 5
    filled = round(score / 10 * 8)
    return "█" * filled + "░" * (8 - filled) + f" {score}/10"

def is_correct_topic(message) -> bool:
    if SG_TOPIC_ID == 0: return True
    return getattr(message, "message_thread_id", None) == SG_TOPIC_ID

def is_store_update(text: str) -> bool:
    if len(text) < MIN_MESSAGE_LENGTH: return False
    keywords = [
        "outlet", "store", "units", "sold", "stock", "display", "competitor",
        "bose", "jbl", "samsung", "follow up", "good news", "shelf", "promo",
        "customer", "staff", "harvey norman", "challenger", "courts", "takashimaya",
        "airport", "millenia", "clocked in", "sva", "brand execution", "engagement",
        "buzz plan", "insights", "sprintcass", "sprint-cass", "bowers", "marshall",
        "sonos", "b&o", "sennheiser", "cms", "hn ", "hnnp", "hnmw", "best denki", "audio house"
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
    if start == -1 or end == 0: raise ValueError("No JSON in response")
    json_str = clean[start:end]
    json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', json_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)
        return json.loads(json_str)


def format_retailer_block(r: dict, is_weekly: bool = False) -> list:
    si        = SENTIMENT_ICONS.get(r.get("sentiment","Mixed"),"🟡")
    trend     = r.get("share_trend", "Holding")
    ti        = TREND_ICONS.get(trend, "➡️")
    share_idx = r.get("share_index") or 5
    stores    = r.get("stores_visited", [])
    units_key = "total_units" if is_weekly else "units_sold"
    units     = r.get(units_key)
    units_str = f" • {esc(str(units))} units" if units not in (None,"null","","None") else ""
    lines = [
        f"\n🏪 <b>{esc(r.get('retailer','?'))}</b>{units_str}  {si}",
        f"   📍 {', '.join(esc(s) for s in stores) if stores else '—'}",
        f"   {ti} {esc(trend)}  |  Index: <code>{format_share_bar(share_idx)}</code>",
    ]
    wins_key   = "top_wins" if is_weekly else "wins"
    issues_key = "recurring_issues" if is_weekly else "issues"
    comp_key   = "competitor_threats" if is_weekly else "competitor_activity"
    for win in r.get(wins_key, []):     lines.append(f"   ✅ {esc(win)}")
    for issue in r.get(issues_key, []): lines.append(f"   ⚠️ {esc(issue)}")
    for comp in r.get(comp_key, []):    lines.append(f"   👀 {esc(comp)}")
    actions_key = "priority_actions" if is_weekly else "actions"
    for a in r.get(actions_key, []):
        urg = a.get("urgency","Soon")
        lines.append(f"   {URGENCY_ICONS.get(urg,'🟡')} {esc(a.get('action',''))}")
    return lines


async def send_daily_digest(context) -> None:
    now_sgt       = datetime.now(SGT)
    today         = now_sgt.strftime("%d %b %Y")
    cutoff        = now_sgt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_reports = [i for i in insights_store
                     if datetime.fromisoformat(i["timestamp"]).astimezone(SGT) >= cutoff]
    if not today_reports:
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID,
            text=f"📋 <b>Daily Digest — {esc(today)}</b>\n\nNo store visit updates received today.",
            parse_mode=ParseMode.HTML)
        return

    model = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    agg   = aggregate_reports(today_reports)

    bullet = (
        f"Date: {today} | Reports: {agg['report_count']} | "
        f"Stores: {agg['stores_visited']} | Units: {agg.get('total_units_sold','unknown')}\n"
        f"Sentiment: {agg['overall_sentiment']} | Share trend: {agg['overall_share_trend']}\n"
        + "\n".join(
            f"{r['retailer']}: {r['sentiment']}, wins: {', '.join(r['wins'][:2]) or 'none'}, "
            f"issues: {', '.join(r['issues'][:2]) or 'none'}"
            for r in agg["by_retailer"]
        )
    )
    narrative  = await get_narrative(bullet, model, period="day")
    sent_icon  = SENTIMENT_ICONS.get(agg["overall_sentiment"],"🟡")
    trend_icon = TREND_ICONS.get(agg["overall_share_trend"],"➡️")

    header = [
        "📋 <b>Daily Field Insights Digest</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Date:</b> {esc(today)}",
        f"📊 <b>Reports:</b> {agg['report_count']}  |  {sent_icon} {esc(agg['overall_sentiment'])}  |  {trend_icon} {esc(agg['overall_share_trend'])}",
    ]
    if agg.get("total_units_sold"):
        header.append(f"🛒 <b>Total units sold:</b> {esc(agg['total_units_sold'])}")
    if narrative:
        header += ["", f"📝 <i>{esc(narrative)}</i>"]
    await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(header), parse_mode=ParseMode.HTML)

    for r in agg["by_retailer"]:
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━"] + format_retailer_block(r, is_weekly=False)
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML)

    urgent = agg.get("top_urgent_actions", [])
    if urgent:
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "⚡ <b>Priority Actions Today</b>"]
        for a in urgent:
            retailer = f" [{esc(a.get('retailer',''))}]" if a.get("retailer") else ""
            lines.append(f"🔴 <i>Urgent</i>{retailer} — {esc(a.get('action',''))}")
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML)

    logger.info(f"Daily digest sent — {len(today_reports)} reports, aggregated locally")


async def send_weekly_rollup(context) -> None:
    now_sgt      = datetime.now(SGT)
    cutoff       = now_sgt - timedelta(days=7)
    week_reports = [i for i in insights_store
                    if datetime.fromisoformat(i["timestamp"]).astimezone(SGT) >= cutoff]
    date_str = now_sgt.strftime("%d %b %Y")
    if not week_reports:
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID,
            text="📊 <b>Weekly Rollup</b>\n\nNo store visit updates received this week.",
            parse_mode=ParseMode.HTML)
        return

    model = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    agg   = aggregate_reports(week_reports)

    bullet = (
        f"Week ending: {date_str} | Reports: {agg['report_count']} | "
        f"Stores: {agg['stores_visited']} | Units: {agg.get('total_units_sold','unknown')}\n"
        f"Sentiment: {agg['overall_sentiment']} | Share trend: {agg['overall_share_trend']} | Avg index: {agg['avg_share_index']}/10\n"
        + "\n".join(
            f"{r['retailer']}: {r['sentiment']}, wins: {', '.join(r['wins'][:2]) or 'none'}, "
            f"issues: {', '.join(r['issues'][:2]) or 'none'}, "
            f"competitors: {', '.join(r['competitor_activity'][:2]) or 'none'}"
            for r in agg["by_retailer"]
        )
    )
    narrative  = await get_narrative(bullet, model, period="week")
    sent_icon  = SENTIMENT_ICONS.get(agg["overall_sentiment"],"🟡")
    trend_icon = TREND_ICONS.get(agg["overall_share_trend"],"➡️")

    header = [
        "📊 <b>Weekly Field Insights Rollup</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>Week ending:</b> {esc(date_str)}",
        f"📋 <b>Reports analysed:</b> {agg['report_count']}  |  {sent_icon} {esc(agg['overall_sentiment'])}  |  {trend_icon} {esc(agg['overall_share_trend'])}",
        f"⭐ <b>Avg share index:</b> {esc(agg['avg_share_index'])}/10",
    ]
    if agg.get("total_units_sold"):
        header.append(f"🛒 <b>Total units sold:</b> {esc(agg['total_units_sold'])}")
    if narrative:
        header += ["", f"📝 <i>{esc(narrative)}</i>"]
    await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(header), parse_mode=ParseMode.HTML)

    for r in agg["by_retailer"]:
        weekly_r = {**r, "top_wins": r["wins"], "recurring_issues": r["issues"],
                    "competitor_threats": r["competitor_activity"],
                    "priority_actions": r["actions"], "total_units": r.get("units_sold")}
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━"] + format_retailer_block(weekly_r, is_weekly=True)
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML)

    urgent = agg.get("top_urgent_actions", [])
    if urgent:
        lines = ["━━━━━━━━━━━━━━━━━━━━━━━━", "⚡ <b>Priority Actions This Week</b>"]
        for a in urgent:
            retailer = f" [{esc(a.get('retailer',''))}]" if a.get("retailer") else ""
            lines.append(f"🔴 <i>Urgent</i>{retailer} — {esc(a.get('action',''))}")
        await context.bot.send_message(chat_id=MANAGEMENT_CHAT_ID, text="\n".join(lines), parse_mode=ParseMode.HTML)

    logger.info(f"Weekly rollup sent — {len(week_reports)} reports, aggregated locally")


async def handle_cm_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message: return
    if not is_correct_topic(message): return
    notes = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None): return
        return
    if not is_store_update(notes): return
    reporter = message.from_user.full_name if message.from_user else "Unknown"
    store    = extract_store_name(notes)
    retailer = get_retailer_group(store)
    date_str = datetime.now(SGT).strftime("%d %b %Y, %I:%M %p")
    model    = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    ack = await message.reply_text("⏳ Logging update...")
    try:
        user_msg = f"Reporter: {reporter}\nStore: {store}\nRetailer: {retailer}\n\nStore visit notes:\n{notes}"
        data     = await call_claude(SYSTEM_PROMPT, user_msg, model)
        insights_store.append({
            "reporter": reporter, "store": store, "retailer": retailer,
            "date": date_str, "timestamp": datetime.now(SGT).isoformat(),
            "data": data, "notes": notes
        })
        save_store()
        write_to_sheet(data, reporter, store, retailer, date_str, notes)
        await ack.edit_text(
            f"✅ Update logged — <b>{esc(store)}</b> ({esc(retailer)}) by {esc(reporter)}\n"
            f"<i>Saved to Google Sheets • Daily digest at 9pm SGT</i>",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error: {e}")
        await ack.edit_text("⚠️ Could not log this update. Please try again.")


async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    notes   = message.text or message.caption or ""
    if not notes.strip():
        if getattr(message, "media_group_id", None): return
        return
    reporter   = message.from_user.full_name if message.from_user else "Unknown"
    store      = extract_store_name(notes)
    retailer   = get_retailer_group(store)
    date_str   = datetime.now(SGT).strftime("%d %b %Y, %I:%M %p")
    model      = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001").strip()
    processing = await message.reply_text("⏳ Extracting insights...")
    try:
        user_msg = f"Reporter: {reporter}\nStore: {store}\nRetailer: {retailer}\n\nStore visit notes:\n{notes}"
        data     = await call_claude(SYSTEM_PROMPT, user_msg, model)
        insights_store.append({
            "reporter": reporter, "store": store, "retailer": retailer,
            "date": date_str, "timestamp": datetime.now(SGT).isoformat(),
            "data": data, "notes": notes
        })
        save_store()
        write_to_sheet(data, reporter, store, retailer, date_str, notes)
        sent_icon = SENTIMENT_ICONS.get(data.get("sentiment","Mixed"),"🟡")
        pri_icon  = PRIORITY_ICONS.get(data.get("priority","Medium"),"🟡")
        lines = [
            "📋 <b>Field Insights Report</b>", "━━━━━━━━━━━━━━━━━━━━━━━━",
            f"👤 <b>Reporter:</b> {esc(reporter)}",
            f"🏪 <b>Store:</b> {esc(store)}  |  <b>Retailer:</b> {esc(retailer)}",
            f"📅 <b>Date:</b> {esc(date_str)}",
            f"📊 {sent_icon} {esc(data.get('sentiment','—'))}   |   {pri_icon} {esc(data.get('priority','—'))} priority",
        ]
        if data.get("units_sold") not in (None,"null","","None"):
            lines.append(f"🛒 <b>Units sold:</b> {esc(str(data['units_sold']))}")
        lines += ["", f"<i>{esc(data.get('summary','—'))}</i>"]
        for key, (icon, label) in CATEGORY_ICONS.items():
            items = data.get(key, [])
            if items:
                lines += ["", f"{icon} <b>{label}</b>"]
                for item in items: lines.append(f"• {esc(item)}")
        bench = data.get("competitor_bench", [])
        if bench:
            lines += ["", "⚔️ <b>Competitor Benchmarking</b>"]
            for c in bench:
                threat = c.get("threat_level","Low")
                lines.append(f"{THREAT_COLORS.get(threat,'🟢')} <b>{esc(c.get('brand','?'))}</b> — "
                    f"Shelf: {esc(c.get('shelf_space','?'))} | Price: {esc(c.get('price_position','?'))} | "
                    f"Promo: {esc(c.get('promo_activity','None'))}")
        actions = data.get("actions", [])
        if actions:
            lines += ["", "⚡ <b>Actions Required</b>"]
            for a in actions:
                urg = a.get("urgency","Soon")
                lines.append(f"{URGENCY_ICONS.get(urg,'🟡')} {esc(a.get('action',''))}")
        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")
        await processing.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Error: {e}")
        await processing.edit_text(f"⚠️ Error: {str(e)[:200]}")


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generating today's digest...")
    await send_daily_digest(context)

async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generating weekly rollup...")
    await send_weekly_rollup(context)

async def topic_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message   = update.message
    thread_id = getattr(message, "message_thread_id", None)
    chat_id   = message.chat_id
    if thread_id:
        await message.reply_text(
            f"📌 <b>Topic ID:</b> <code>{thread_id}</code>\n<b>Chat ID:</b> <code>{chat_id}</code>\n\n"
            f"Add to Render: <code>SG_TOPIC_ID = {thread_id}</code>", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text(f"ℹ️ Not inside a topic.\n<b>Chat ID:</b> <code>{chat_id}</code>", parse_mode=ParseMode.HTML)

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"<b>Chat ID:</b> <code>{update.message.chat_id}</code>", parse_mode=ParseMode.HTML)

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
            msg = "✅ <b>Models:</b>\n" + "\n".join(f"• <code>{m}</code>" for m in models)
        else:
            msg = f"❌ {r.status_code}: <code>{esc(r.text[:200])}</code>"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ {str(e)[:200]}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic_status = f"Monitoring topic ID: <code>{SG_TOPIC_ID}</code>" if SG_TOPIC_ID else "⚠️ SG_TOPIC_ID not set"
    sheet_status = "✅ Connected" if GOOGLE_SHEET_ID else "⚠️ Not configured"
    await update.message.reply_text(
        "👋 <b>Field Insights Bot is active!</b>\n\n"
        f"📌 {topic_status}\n"
        f"📊 <b>Google Sheets:</b> {sheet_status}\n"
        f"💾 <b>Reports in memory:</b> {len(insights_store)}\n"
        "⏰ <b>Daily digest:</b> 9pm SGT\n"
        "📅 <b>Weekly rollup:</b> Saturday 10am SGT\n\n"
        "<b>Commands:</b>\n"
        "• /daily — generate today's digest now\n"
        "• /weekly — generate this week's rollup now\n"
        "• /template — get the CM update template\n"
        "• /topicid — get the current topic ID\n"
        "• /chatid — get this group's chat ID\n"
        "• /testapi — check API connection",
        parse_mode=ParseMode.HTML)


def main():
    logger.info(f"API key prefix: {ANTHROPIC_KEY[:20]}")
    logger.info(f"Management chat ID: {MANAGEMENT_CHAT_ID}")
    logger.info(f"SG Topic ID: {SG_TOPIC_ID}")
    logger.info(f"Google Sheet ID: {GOOGLE_SHEET_ID[:20] if GOOGLE_SHEET_ID else 'NOT SET'}")
    logger.info(f"Reports loaded from disk: {len(insights_store)}")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.job_queue.run_daily(
        send_daily_digest,
        time=datetime.strptime("13:00", "%H:%M").time().replace(tzinfo=ZoneInfo("UTC")),
        name="daily_digest"
    )
    app.job_queue.run_daily(
        send_weekly_rollup,
        time=datetime.strptime("02:00", "%H:%M").time().replace(tzinfo=ZoneInfo("UTC")),
        days=(6,), name="weekly_rollup"
    )
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("testapi",  test_api))
    app.add_handler(CommandHandler("daily",    cmd_daily))
    app.add_handler(CommandHandler("weekly",   cmd_weekly))
    app.add_handler(CommandHandler("template", template_command))
    app.add_handler(CommandHandler("chatid",   get_chat_id))
    app.add_handler(CommandHandler("topicid",  topic_id_command))
    app.add_handler(MessageHandler(
        filters.FORWARDED & (filters.TEXT | filters.PHOTO | filters.CAPTION), handle_forwarded))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.CAPTION) & ~filters.COMMAND & ~filters.FORWARDED,
        handle_cm_message))
    logger.info("Bot running — daily digest 9pm SGT, weekly rollup Saturday 10am SGT")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
