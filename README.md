# BlockVeil Support Bot

A Telegram support bot for the BlockVeil ecosystem. Handles support tickets, bug reports, and feature requests with attachment support, star ratings, and automatic forwarding to admin groups.

---

## Features

- /start command with a full inline keyboard main menu
- Need Support flow: App selection, issue description, optional attachments (photo/video/voice/file), star rating (1-5), and submission
- Report Bug and Request Feature flows with description and attachment support
- View My Tickets, FAQ, and Profile menu options
- Auto-forwards tickets to 2 separate admin groups:
  - Support Group: receives "Need Support" tickets
  - Bug/Feature Group: receives "Report Bug" and "Request Feature" tickets
- Unique ticket IDs per submission (format: BV-YYMMDDHHMI-XXXX)
- Sends username + user ID with every forwarded ticket

---

## Bot Flow

```
/start
  Main Menu
    Need Support -> Select App (BlockVeil App) -> Describe Issue -> [Next]
      -> Add Attachments (photo/video/voice/file) -> [Done/Skip]
      -> Star Rating (1-5) or Skip -> [Submit]
      -> Forwarded to SUPPORT_GROUP with username and ticket ID

    Report Bug -> Describe Bug -> [Next]
      -> Add Attachments -> [Done/Skip]
      -> [Submit]
      -> Forwarded to BUG_FEATURE_GROUP

    Request Feature -> Describe Feature -> [Next]
      -> Add Attachments -> [Done/Skip]
      -> [Submit]
      -> Forwarded to BUG_FEATURE_GROUP

    View My Tickets -> Coming Soon notice
    FAQ -> Static FAQ answers
    Profile -> Shows Telegram user info
```

---

## Setup

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get Admin Group IDs

1. Create 2 Telegram groups: one for Support, one for Bug/Feature reports
2. Add your bot to both groups and make it an **admin**
3. To get the group ID: add [@userinfobot](https://t.me/userinfobot) temporarily, or use the Telegram API. Group IDs start with `-100`.

### 3. Local Development

```bash
# Clone the repo
git clone https://github.com/BlockVeilBuild/blockveil-support-bot.git
cd blockveil-support-bot

# Create a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create your .env file
cp .env.example .env
# Edit .env and fill in your BOT_TOKEN, SUPPORT_GROUP_ID, BUG_FEATURE_GROUP_ID

# Run the bot
python bot.py
```

### 4. Deploy on Railway

1. Push this repo to GitHub
2. Go to [Railway](https://railway.app) and create a new project
3. Click "Deploy from GitHub repo" and select this repository
4. Go to the **Variables** tab and add:
   - `BOT_TOKEN` = your bot token
   - `SUPPORT_GROUP_ID` = your support group ID (e.g. `-1001234567890`)
   - `BUG_FEATURE_GROUP_ID` = your bug/feature group ID (e.g. `-1009876543210`)
5. Railway will automatically detect the `Procfile` and start the worker

---

## Environment Variables

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from BotFather |
| `SUPPORT_GROUP_ID` | Group ID where support tickets are forwarded |
| `BUG_FEATURE_GROUP_ID` | Group ID where bug and feature tickets are forwarded |

---

## Project Structure

```
blockveil-support-bot/
  bot.py              # Main bot logic (all handlers, conversation flow)
  requirements.txt    # Python dependencies
  Procfile            # Railway worker process definition
  .env.example        # Environment variable template
  .gitignore
  README.md
```

---

## Commands

| Command | Description |
|---|---|
| `/start` | Show main menu |
| `/cancel` | Cancel current flow and return to start |

---

## Tech Stack

- Python 3.11+
- python-telegram-bot 21.6 (PTB v21, async)
- Railway (deployment)

---

## License

MIT License. Part of the BlockVeil open-source ecosystem.
Built by [@antonysrm](https://twitter.com/antonysrm)
