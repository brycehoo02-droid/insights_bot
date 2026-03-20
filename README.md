# Field Insights Bot — Setup Guide

Automatically extracts structured insights from store visit updates
whenever a message is **forwarded** into the group chat. No commands needed.

---

## Step 1 — Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name: e.g. `Field Insights Bot`
4. Choose a username: e.g. `@YourBrandInsightsBot`
5. BotFather will give you a **bot token** — copy it

---

## Step 2 — Get your Anthropic API key

1. Go to https://console.anthropic.com
2. Navigate to **API Keys → Create Key**
3. Copy the key

---

## Step 3 — Deploy to Railway (free, recommended)

1. Go to https://railway.app and sign up (free)
2. Click **New Project → Deploy from GitHub**
   (or New Project → Empty Project and upload files manually)
3. Add your files: `bot.py`, `requirements.txt`, `Procfile`
4. Go to **Variables** and add:
   - `TELEGRAM_TOKEN` = your token from BotFather
   - `ANTHROPIC_API_KEY` = your Anthropic key
5. Deploy — your bot will be live in ~1 minute

---

## Step 4 — Add the bot to your Channel Manager group

1. Open your Channel Manager Telegram group
2. Tap the group name → **Add Members**
3. Search for your bot username (e.g. `@YourBrandInsightsBot`) and add it
4. Make the bot an **Admin**:
   Group Settings → Administrators → Add Admin → select your bot
   Enable the **"Read Messages"** permission

---

## How to use

A Channel Manager sends their store visit update anywhere
(WhatsApp, SMS, another Telegram chat, typed message, etc.)

**Simply forward that message into this group.**

The bot automatically detects it's a forwarded message and replies
with a full structured insights report — no commands needed.

The reporter name is pulled automatically from the original sender
(or from the person who forwarded if the original sender has privacy mode on).

---

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick guide |
| `/help` | Full usage instructions |

---

## Running locally (for testing)

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=your_token
export ANTHROPIC_API_KEY=your_key
python bot.py
```

---

## What the bot extracts

- Executive summary + sentiment (Positive / Mixed / Negative)
- Priority level (High / Medium / Low)
- Units sold
- Sales performance insights
- Competitor activity
- Customer feedback
- Stock & display issues
- Staff feedback
- Promo effectiveness
- Actions required with urgency tags (Urgent / Soon / Monitor)
