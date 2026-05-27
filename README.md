# BlockVeil Support Bot v2

A Telegram support bot for the BlockVeil ecosystem. Handles support tickets, bug reports, and feature requests with SQLite-backed user profiles, ticket stats, timezone support, and automatic forwarding to admin groups.

---

## What's New in v2

- SQLite database: user profiles, ticket counts, timezone, joined date all persisted
- Profile page: full stats (total/support/bug/feature ticket counts) + joined date
- Change Timezone: 10 popular quick-pick buttons + manual IANA input
- Need Support: now has "BlockVeil App" and "Others" buttons
- Report Bug: now has "BlockVeil App" and "Others" buttons

---

## Bot Flow

```
/start
  Main Menu
    Need Support
      -> BlockVeil App / Others
      -> Describe Issue -> [Next]
      -> Attachments (photo/video/voice/file) -> [Done/Skip]
      -> Star Rating 1-5 or Skip -> [Submit]
      -> Forwarded to SUPPORT_GROUP

    Report Bug
      -> BlockVeil App / Others
      -> Describe Bug -> [Next]
      -> Attachments -> [Done/Skip]
      -> [Submit]
      -> Forwarded to BUG_FEATURE_GROUP

    Request Feature
      -> Describe Feature -> [Next]
      -> Attachments -> [Done/Skip]
      -> [Submit]
      -> Forwarded to BUG_FEATURE_GROUP

    View My Tickets  -> Live stats from DB
    FAQ              -> Static FAQ
    Profile
      -> Name, Username, User ID, Joined date, Timezone
      -> Ticket stats: Total / Support / Bug / Feature
      -> [Change Timezone] button
          -> 10 quick-pick buttons OR type custom IANA name
```

---

## Setup

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get Admin Group IDs

1. Create 2 Telegram groups: one for Support, one for Bug/Feature
2. Add your bot to both groups and make it an **admin** (so it can send messages)
3. To get a group ID: temporarily add [@userinfobot](https://t.me/userinfobot) to the group, or use the Telegram API. Group IDs begin with `-100`.

### 3. Local Development

```bash
# Clone the repo
git clone https://github.com/BlockVeilBuild/blockveil-support-bot.git
cd blockveil-support-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
# Edit .env: fill in BOT_TOKEN, SUPPORT_GROUP_ID, BUG_FEATURE_GROUP_ID

# Run
python bot.py
```

### 4. Deploy on Railway

1. Push this repo to GitHub
2. Go to [Railway](https://railway.app) and create a new project
3. Click "Deploy from GitHub repo" and select this repository
4. Go to the **Variables** tab and add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your BotFather token |
| `SUPPORT_GROUP_ID` | e.g. `-1001234567890` |
| `BUG_FEATURE_GROUP_ID` | e.g. `-1009876543210` |

5. Railway detects the `Procfile` and runs `worker: python bot.py` automatically.

> **Note on database persistence:** Railway's filesystem resets on redeploy. For production, add a Railway Volume and set `DB_PATH=/data/blockveil_support.db` in your Variables. Or use Railway's Postgres addon and swap the SQLite layer.

---

## Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `SUPPORT_GROUP_ID` | Group ID for Need Support tickets |
| `BUG_FEATURE_GROUP_ID` | Group ID for Bug and Feature tickets |
| `DB_PATH` | (Optional) SQLite file path. Default: `blockveil_support.db` |

---

## Project Structure

```
blockveil-support-bot/
  bot.py              # Full bot logic (handlers, DB, conversation flow)
  requirements.txt    # Python dependencies
  Procfile            # Railway worker definition
  .env.example        # Env variable template
  .gitignore
  README.md
```

---

## Commands

| Command | Description |
|---|---|
| `/start` | Show main menu (re-registers user in DB) |
| `/cancel` | Cancel current flow and exit |

---

## Admin Group Message Format

```
🛟 Support Request
━━━━━━━━━━━━━━━━━━━━
🎫 Ticket ID: BV-2605143022-0042
👤 User: @antonysrm (123456789)
📱 App: BlockVeil App
🕐 Time: 2026-05-26 14:30 UTC
⭐ Rating: ★★★★☆
━━━━━━━━━━━━━━━━━━━━

📝 Description:
(user's message here)

(followed by any attachments as separate messages)
```

---

## Tech Stack

- Python 3.11+
- python-telegram-bot 21.6 (PTB v21, async/await)
- SQLite (WAL mode for safe concurrent writes)
- Railway (deployment)

---

## License

MIT License. Part of the BlockVeil open-source ecosystem.
Built by [@antonysrm](https://twitter.com/antonysrm)
