"""
BlockVeil Support Bot v2
========================
Changes in v2:
  - SQLite database for user profiles, ticket counts, timezone, joined date
  - Profile page shows: Name, Username, User ID, Joined date, ticket stats (total/support/bug/feature)
  - "Change Timezone" button on profile page
  - Need Support app select: BlockVeil App + Others
  - Report Bug app select: BlockVeil App + Others
  - Timezone-aware joined date display

Deploy: Railway (Procfile -> worker: python bot.py)
Env vars: BOT_TOKEN, SUPPORT_GROUP_ID, BUG_FEATURE_GROUP_ID
"""

import os
import sqlite3
import logging
import zoneinfo
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPPORT_GROUP_ID = int(os.environ["SUPPORT_GROUP_ID"])
BUG_FEATURE_GROUP_ID = int(os.environ["BUG_FEATURE_GROUP_ID"])

# Database path (Railway persistent volume or local)
DB_PATH = os.environ.get("DB_PATH", "blockveil_support.db")

# ---------------------------------------------------------------------------
# Conversation States
# ---------------------------------------------------------------------------
(
    MAIN_MENU,
    SELECT_APP,
    DESCRIBE_ISSUE,
    AWAIT_ATTACHMENT,
    COLLECT_ATTACHMENT,
    RATE_EXPERIENCE,
    CONFIRM_SUBMIT,

    BUG_FEATURE_DESCRIBE,
    BUG_FEATURE_ATTACHMENT,
    BUG_FEATURE_COLLECT,
    BUG_FEATURE_SUBMIT,

    TIMEZONE_INPUT,         # User types their timezone
) = range(12)

# Ticket type constants
TYPE_SUPPORT = "support"
TYPE_BUG = "bug"
TYPE_FEATURE = "feature"

# Popular timezones shown as quick-pick buttons
POPULAR_TIMEZONES = [
    ("🇧🇩 Dhaka (UTC+6)",       "Asia/Dhaka"),
    ("🇮🇳 Kolkata (UTC+5:30)",  "Asia/Kolkata"),
    ("🇵🇰 Karachi (UTC+5)",     "Asia/Karachi"),
    ("🇦🇪 Dubai (UTC+4)",       "Asia/Dubai"),
    ("🇹🇷 Istanbul (UTC+3)",    "Europe/Istanbul"),
    ("🇬🇧 London (UTC+0/1)",    "Europe/London"),
    ("🇺🇸 New York (UTC-5/-4)", "America/New_York"),
    ("🇺🇸 Los Angeles (UTC-8)", "America/Los_Angeles"),
    ("🇸🇬 Singapore (UTC+8)",   "Asia/Singapore"),
    ("🇯🇵 Tokyo (UTC+9)",       "Asia/Tokyo"),
]

# ---------------------------------------------------------------------------
# Database Layer
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Return a thread-local SQLite connection with WAL mode for concurrency."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                full_name   TEXT    NOT NULL,
                username    TEXT,
                joined_at   TEXT    NOT NULL,   -- ISO 8601 UTC
                timezone    TEXT    DEFAULT 'Asia/Dhaka'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT    NOT NULL UNIQUE,
                user_id     INTEGER NOT NULL,
                ticket_type TEXT    NOT NULL,   -- support / bug / feature
                app_name    TEXT,
                description TEXT,
                rating      INTEGER,
                created_at  TEXT    NOT NULL    -- ISO 8601 UTC
            )
        """)
        conn.commit()
    logger.info("Database initialized at %s", DB_PATH)


def upsert_user(user) -> None:
    """Insert user on first seen; update name/username on subsequent calls."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, full_name, username, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                username  = excluded.username
        """, (user.id, user.full_name, user.username, now_iso))
        conn.commit()


def get_user_row(user_id: int) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def set_user_timezone(user_id: int, tz: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?", (tz, user_id)
        )
        conn.commit()


def save_ticket(ticket_id_str: str, user_id: int, ticket_type: str,
                app_name: str, description: str, rating: int | None) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO tickets (ticket_id, user_id, ticket_type, app_name, description, rating, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticket_id_str, user_id, ticket_type, app_name, description, rating, now_iso))
        conn.commit()


def get_ticket_stats(user_id: int) -> dict:
    """Return dict with total, support, bug, feature counts for a user."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(ticket_type = 'support')                    AS support_count,
                SUM(ticket_type = 'bug')                        AS bug_count,
                SUM(ticket_type = 'feature')                    AS feature_count
            FROM tickets WHERE user_id = ?
        """, (user_id,)).fetchone()
    return {
        "total":   row["total"]         or 0,
        "support": row["support_count"] or 0,
        "bug":     row["bug_count"]     or 0,
        "feature": row["feature_count"] or 0,
    }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def username_display(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name


def gen_ticket_id(user_id: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
    return f"BV-{ts}-{user_id % 10000:04d}"


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def format_joined_date(iso_str: str, tz_name: str) -> str:
    """Format ISO UTC string -> 'DD Month YYYY' in user's timezone."""
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        tz = zoneinfo.ZoneInfo(tz_name)
        dt_local = dt_utc.astimezone(tz)
        return dt_local.strftime("%-d %B %Y")  # e.g. "26 May 2026"
    except Exception:
        return iso_str[:10]  # Fallback: YYYY-MM-DD

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def make_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛟  Need Support",      callback_data="menu_support")],
        [InlineKeyboardButton("🐛  Report Bug",        callback_data="menu_bug"),
         InlineKeyboardButton("💡  Request Feature",   callback_data="menu_feature")],
        [InlineKeyboardButton("🎫  View My Tickets",   callback_data="menu_tickets"),
         InlineKeyboardButton("❓  FAQ",               callback_data="menu_faq")],
        [InlineKeyboardButton("👤  Profile",           callback_data="menu_profile")],
    ])


def make_support_app_keyboard() -> InlineKeyboardMarkup:
    """App selection for Need Support flow."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐  BlockVeil App", callback_data="app_blockveil")],
        [InlineKeyboardButton("🔧  Others",        callback_data="app_others")],
        [InlineKeyboardButton("⬅️  Back",           callback_data="back_main")],
    ])


def make_bug_app_keyboard() -> InlineKeyboardMarkup:
    """App selection for Report Bug flow."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐  BlockVeil App", callback_data="bugapp_blockveil")],
        [InlineKeyboardButton("🔧  Others",        callback_data="bugapp_others")],
        [InlineKeyboardButton("⬅️  Back",           callback_data="back_main")],
    ])


def make_next_keyboard(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️  Next", callback_data=cb)],
    ])


def make_skip_done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️  Skip",        callback_data="skip_attachment"),
         InlineKeyboardButton("✅  Done (Next)", callback_data="done_attachment")],
    ])


def make_rating_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(stars(i), callback_data=f"rate_{i}")] for i in range(1, 6)]
    rows.append([InlineKeyboardButton("➡️  Next (No Rating)", callback_data="rate_skip")])
    return InlineKeyboardMarkup(rows)


def make_submit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨  Submit Ticket", callback_data="submit_confirm")],
        [InlineKeyboardButton("❌  Cancel",         callback_data="back_main")],
    ])


def make_timezone_keyboard() -> InlineKeyboardMarkup:
    """Quick-pick popular timezone buttons + manual input notice + back."""
    rows = []
    for label, tz_key in POPULAR_TIMEZONES:
        rows.append([InlineKeyboardButton(label, callback_data=f"tz_{tz_key}")])
    rows.append([InlineKeyboardButton("⬅️  Back to Profile", callback_data="back_profile")])
    return InlineKeyboardMarkup(rows)


def make_back_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️  Back to Profile", callback_data="back_profile"),
    ]])


def make_back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🏠  Main Menu", callback_data="back_main"),
    ]])

# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    upsert_user(user)   # Register/update user in DB on every /start

    text = (
        f"👋 *Welcome to BlockVeil Support, {user.first_name}!*\n\n"
        "আমরা আপনাকে সাহায্য করতে এখানে আছি।\n"
        "নিচের অপশন থেকে একটি বেছে নিন:\n\n"
        "_Select an option below to get started._"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_menu_keyboard(),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Main Menu Callbacks
# ---------------------------------------------------------------------------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Need Support ---
    if data == "menu_support":
        await query.edit_message_text(
            "🛟 *Need Support*\n\nকোন অ্যাপের জন্য সাপোর্ট দরকার?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_support_app_keyboard(),
        )
        return SELECT_APP

    # --- Report Bug ---
    elif data == "menu_bug":
        await query.edit_message_text(
            "🐛 *Report a Bug*\n\nকোন অ্যাপে বাগটি পেয়েছেন?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_bug_app_keyboard(),
        )
        return SELECT_APP  # Reuse SELECT_APP state, bug app handler will pick it up

    # --- Request Feature ---
    elif data == "menu_feature":
        context.user_data["ticket_type"] = TYPE_FEATURE
        context.user_data["app_name"] = "BlockVeil"
        await query.edit_message_text(
            "💡 *Request a Feature*\n\nআপনার ফিচার আইডিয়াটি বিস্তারিত লিখুন।\n\n"
            "_Describe your feature request in detail._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

    # --- View My Tickets ---
    elif data == "menu_tickets":
        user = query.from_user
        stats = get_ticket_stats(user.id)
        text = (
            "🎫 *Your Tickets*\n\n"
            f"📊 Total: {stats['total']}\n"
            f"🛟 Support: {stats['support']}\n"
            f"🐛 Bug Reports: {stats['bug']}\n"
            f"💡 Feature Requests: {stats['feature']}\n\n"
            "_Full ticket history with status tracking is coming soon!_"
        )
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
        return MAIN_MENU

    # --- FAQ ---
    elif data == "menu_faq":
        faq_text = (
            "❓ *Frequently Asked Questions*\n\n"
            "*Q: BlockVeil কি?*\n"
            "A: BlockVeil একটি প্রাইভেসি-ফোকাসড টেক ভেঞ্চার যা এনক্রিপ্টেড টুলস ও ক্রিপ্টো এডুকেশন প্রদান করে।\n\n"
            "*Q: সাপোর্ট পেতে কতক্ষণ লাগে?*\n"
            "A: সাধারণত ২৪-৪৮ ঘণ্টার মধ্যে রেসপন্স পাবেন।\n\n"
            "*Q: বাগ রিপোর্ট করলে কি পুরস্কার আছে?*\n"
            "A: হ্যাঁ! ভ্যালিড বাগ রিপোর্টারদের আমরা ক্রেডিট দিই।\n\n"
            "*Q: আমার ডেটা কি নিরাপদ?*\n"
            "A: হ্যাঁ। সব ডেটা AES-256-GCM দিয়ে এনক্রিপ্ট থাকে।\n\n"
            "*Q: /cancel কখন ব্যবহার করব?*\n"
            "A: কোনো flow থেকে বের হতে চাইলে /cancel দিন।"
        )
        await query.edit_message_text(
            faq_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
        return MAIN_MENU

    # --- Profile ---
    elif data in ("menu_profile", "back_profile"):
        return await show_profile(query)

    # --- Back to main ---
    elif data == "back_main":
        return await show_main_menu(query)

    return MAIN_MENU


async def show_main_menu(query) -> int:
    """Edit current message to main menu."""
    user = query.from_user
    await query.edit_message_text(
        f"👋 *Welcome back, {user.first_name}!*\n\n"
        "নিচের অপশন থেকে একটি বেছে নিন:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_menu_keyboard(),
    )
    return MAIN_MENU


async def show_profile(query) -> int:
    """Build and display the user profile page."""
    user = query.from_user
    row = get_user_row(user.id)
    stats = get_ticket_stats(user.id)

    # Joined date with user's timezone
    tz_name = row["timezone"] if row else "Asia/Dhaka"
    joined_iso = row["joined_at"] if row else datetime.now(timezone.utc).isoformat()
    joined_str = format_joined_date(joined_iso, tz_name)

    uname = f"@{user.username}" if user.username else "N/A"

    text = (
        "👤 *My Profile*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 *Name:* {user.full_name}\n"
        f"🔗 *Username:* {uname}\n"
        f"🆔 *User ID:* `{user.id}`\n"
        f"📅 *Joined:* {joined_str}\n"
        f"🌍 *Timezone:* `{tz_name}`\n\n"
        "📊 *My Activity*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 *Total Tickets Created:* {stats['total']}\n"
        f"🛟 *Support Tickets:* {stats['support']}\n"
        f"🐛 *Bug Reports:* {stats['bug']}\n"
        f"💡 *Feature Requests:* {stats['feature']}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍  Change Timezone", callback_data="change_timezone")],
        [InlineKeyboardButton("⬅️  Back",            callback_data="back_main")],
    ])

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Timezone Flow
# ---------------------------------------------------------------------------

async def change_timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show timezone selection screen."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🌍 *Change Timezone*\n\n"
        "নিচের popular timezone গুলো থেকে select করুন:\n\n"
        "অথবা নিজে টাইপ করুন, যেমন: `Asia/Dhaka`, `Europe/Paris`, `America/Chicago`\n\n"
        "_IANA timezone name টাইপ করে পাঠান।_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_timezone_keyboard(),
    )
    return TIMEZONE_INPUT


async def timezone_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User taps a quick-pick timezone button."""
    query = update.callback_query
    await query.answer()
    tz_name = query.data[3:]  # Strip "tz_" prefix

    set_user_timezone(query.from_user.id, tz_name)
    await query.edit_message_text(
        f"✅ *Timezone Updated!*\n\n"
        f"আপনার timezone এখন: `{tz_name}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_back_profile_keyboard(),
    )
    return MAIN_MENU


async def timezone_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User types a custom IANA timezone string."""
    tz_input = update.message.text.strip()

    # Validate the timezone
    try:
        zoneinfo.ZoneInfo(tz_input)
        valid = True
    except zoneinfo.ZoneInfoNotFoundError:
        valid = False

    if not valid:
        await update.message.reply_text(
            f"❌ *'{tz_input}'* একটি valid timezone নয়।\n\n"
            "সঠিক IANA timezone name দিন, যেমন:\n"
            "`Asia/Dhaka`, `Europe/London`, `America/New_York`\n\n"
            "অথবা উপরের বাটন থেকে select করুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_profile_keyboard(),
        )
        return TIMEZONE_INPUT

    set_user_timezone(update.effective_user.id, tz_input)
    await update.message.reply_text(
        f"✅ *Timezone Updated!*\n\n"
        f"আপনার timezone এখন: `{tz_input}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_back_profile_keyboard(),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# App Selection (Need Support + Report Bug share this state)
# ---------------------------------------------------------------------------

async def select_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle app selection for both Support and Bug flows."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "back_main":
        return await show_main_menu(query)

    # --- Need Support app picks ---
    if data in ("app_blockveil", "app_others"):
        context.user_data["ticket_type"] = TYPE_SUPPORT
        context.user_data["app_name"] = "BlockVeil App" if data == "app_blockveil" else "Others"
        label = "BlockVeil App" if data == "app_blockveil" else "Others"
        await query.edit_message_text(
            f"✍️ *{label} - Support*\n\n"
            "আপনার সমস্যাটি নিচে সুন্দর করে লিখুন।\n\n"
            "_লেখা শেষ হলে message পাঠান।_\n\n"
            "📎 Note: লেখার পরে আপনি চাইলে attachment যোগ করতে পারবেন।",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DESCRIBE_ISSUE

    # --- Report Bug app picks ---
    if data in ("bugapp_blockveil", "bugapp_others"):
        context.user_data["ticket_type"] = TYPE_BUG
        context.user_data["app_name"] = "BlockVeil App" if data == "bugapp_blockveil" else "Others"
        label = "BlockVeil App" if data == "bugapp_blockveil" else "Others"
        await query.edit_message_text(
            f"🐛 *{label} - Bug Report*\n\n"
            "আপনি যে বাগটি পেয়েছেন সেটি বিস্তারিত লিখুন।\n\n"
            "_Steps to reproduce হলে সেটাও লিখুন।_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

    return SELECT_APP

# ---------------------------------------------------------------------------
# Support Flow: Description -> Attachment -> Rating -> Submit
# ---------------------------------------------------------------------------

async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends support issue description text."""
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []

    await update.message.reply_text(
        f"✅ *আপনার বার্তা পাওয়া গেছে।*\n\n"
        f"_{update.message.text}_\n\n"
        "এখন *Next* চাপুন attachment যোগ করতে অথবা সরাসরি এগিয়ে যেতে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return AWAIT_ATTACHMENT


async def to_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📎 *Attachment (Optional)*\n\n"
        "এখন আপনার *ছবি, ভিডিও বা ভয়েস মেসেজ* পাঠান।\n\n"
        "একাধিক ফাইল পাঠাতে পারবেন। সব শেষ হলে *Done (Next)* চাপুন।\n"
        "কিছু না পাঠাতে চাইলে *Skip* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_done_keyboard(),
    )
    return COLLECT_ATTACHMENT


async def collect_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect any media attachment from user."""
    msg = update.message
    attachments = context.user_data.setdefault("attachments", [])

    if msg.photo:
        attachments.append({"type": "photo", "file_id": msg.photo[-1].file_id})
        label = "📷 ছবি"
    elif msg.video:
        attachments.append({"type": "video", "file_id": msg.video.file_id})
        label = "🎬 ভিডিও"
    elif msg.voice:
        attachments.append({"type": "voice", "file_id": msg.voice.file_id})
        label = "🎤 ভয়েস মেসেজ"
    elif msg.document:
        attachments.append({"type": "document", "file_id": msg.document.file_id})
        label = "📄 ফাইল"
    else:
        await msg.reply_text(
            "⚠️ শুধুমাত্র ছবি, ভিডিও, ভয়েস বা ফাইল পাঠান।\n"
            "অথবা *Done (Next)* চাপুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_skip_done_keyboard(),
        )
        return COLLECT_ATTACHMENT

    await msg.reply_text(
        f"✅ {label} যোগ হয়েছে! (মোট: {len(attachments)}টি)\n\n"
        "আরও পাঠান অথবা *Done (Next)* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_done_keyboard(),
    )
    return COLLECT_ATTACHMENT


async def attachment_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User finishes attachment step. Route by ticket type."""
    query = update.callback_query
    await query.answer()
    ticket_type = context.user_data.get("ticket_type")

    if ticket_type in (TYPE_BUG, TYPE_FEATURE):
        await query.edit_message_text(
            "🎉 *প্রায় শেষ!*\n\n*Submit* চাপলে আপনার রিপোর্টটি পাঠানো হবে।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_submit_keyboard(),
        )
        return BUG_FEATURE_SUBMIT

    # Support -> rating
    await query.edit_message_text(
        "⭐ *Rate Your Experience*\n\n"
        "এই সাপোর্ট সেশনকে কতটা রেট দেবেন? (ঐচ্ছিক)\n\n"
        "না দিতে চাইলে *Next (No Rating)* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_rating_keyboard(),
    )
    return RATE_EXPERIENCE


async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    context.user_data["rating"] = None if query.data == "rate_skip" else int(query.data.split("_")[1])

    await query.edit_message_text(
        "🎉 *প্রায় শেষ!*\n\n*Submit Ticket* চাপলে টিকেটটি পাঠানো হবে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_submit_keyboard(),
    )
    return CONFIRM_SUBMIT

# ---------------------------------------------------------------------------
# Bug / Feature Flow: Description -> Attachment -> Submit
# ---------------------------------------------------------------------------

async def bug_feature_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []

    await update.message.reply_text(
        f"✅ *পাওয়া গেছে:*\n\n_{update.message.text}_\n\n"
        "এখন *Next* চাপুন attachment যোগ করতে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return BUG_FEATURE_ATTACHMENT


async def bug_feature_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📎 *Attachment (Optional)*\n\n"
        "স্ক্রিনশট, ভিডিও বা অন্য ফাইল পাঠান।\n\n"
        "শেষ হলে *Done (Next)* চাপুন অথবা *Skip* করুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_done_keyboard(),
    )
    return BUG_FEATURE_COLLECT

# ---------------------------------------------------------------------------
# Submit (shared by all ticket types)
# ---------------------------------------------------------------------------

async def submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        context.user_data.clear()
        return await show_main_menu(query)

    user = query.from_user
    upsert_user(user)   # Ensure user exists in DB

    ud = context.user_data
    ticket_type = ud.get("ticket_type", TYPE_SUPPORT)
    tid = gen_ticket_id(user.id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    app_name = ud.get("app_name", "BlockVeil App")
    description = ud.get("description", "N/A")
    rating_val = ud.get("rating")

    type_labels = {
        TYPE_SUPPORT: "🛟 Support Request",
        TYPE_BUG:     "🐛 Bug Report",
        TYPE_FEATURE: "💡 Feature Request",
    }
    type_label = type_labels.get(ticket_type, "Ticket")
    rating_str = stars(rating_val) if rating_val else "Not rated"

    header = (
        f"*{type_label}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket ID: `{tid}`\n"
        f"👤 User: {username_display(user)} (`{user.id}`)\n"
        f"📱 App: {app_name}\n"
        f"🕐 Time: {now}\n"
        f"⭐ Rating: {rating_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 *Description:*\n{description}"
    )

    attachments = ud.get("attachments", [])
    target_group = SUPPORT_GROUP_ID if ticket_type == TYPE_SUPPORT else BUG_FEATURE_GROUP_ID

    try:
        await context.bot.send_message(
            chat_id=target_group,
            text=header,
            parse_mode=ParseMode.MARKDOWN,
        )
        for att in attachments:
            fid, att_type = att["file_id"], att["type"]
            if att_type == "photo":
                await context.bot.send_photo(chat_id=target_group, photo=fid)
            elif att_type == "video":
                await context.bot.send_video(chat_id=target_group, video=fid)
            elif att_type == "voice":
                await context.bot.send_voice(chat_id=target_group, voice=fid)
            elif att_type == "document":
                await context.bot.send_document(chat_id=target_group, document=fid)

        # Save ticket to DB for stats tracking
        save_ticket(tid, user.id, ticket_type, app_name, description, rating_val)

        await query.edit_message_text(
            f"✅ *Ticket Submitted Successfully!*\n\n"
            f"🎫 Your Ticket ID: `{tid}`\n\n"
            f"আমাদের টিম শীঘ্রই আপনার সাথে যোগাযোগ করবে।\n"
            f"ধন্যবাদ BlockVeil ব্যবহার করার জন্য! 🙏",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
    except Exception as e:
        logger.error("Failed to forward ticket %s: %s", tid, e)
        await query.edit_message_text(
            "❌ *কিছু একটা ভুল হয়েছে।*\n\nঅনুগ্রহ করে পরে আবার চেষ্টা করুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )

    context.user_data.clear()
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👆 অনুগ্রহ করে উপরের বাটন ব্যবহার করুন।",
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ বাতিল করা হয়েছে। /start দিয়ে আবার শুরু করুন।",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update: %s", context.error, exc_info=context.error)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(change_timezone_callback, pattern="^change_timezone$"),
                CallbackQueryHandler(timezone_button_callback, pattern="^tz_"),
                CallbackQueryHandler(menu_callback),
            ],
            TIMEZONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_text_input),
                CallbackQueryHandler(timezone_button_callback, pattern="^tz_"),
                CallbackQueryHandler(menu_callback, pattern="^back_profile$"),
                CallbackQueryHandler(menu_callback, pattern="^back_main$"),
            ],
            SELECT_APP: [
                CallbackQueryHandler(select_app_callback),
            ],
            DESCRIBE_ISSUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description),
            ],
            AWAIT_ATTACHMENT: [
                CallbackQueryHandler(to_attachment_callback, pattern="^to_attachment$"),
                CallbackQueryHandler(menu_callback, pattern="^back_main$"),
            ],
            COLLECT_ATTACHMENT: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL,
                    collect_attachment,
                ),
                CallbackQueryHandler(attachment_done_callback, pattern="^(skip_attachment|done_attachment)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            ],
            RATE_EXPERIENCE: [
                CallbackQueryHandler(rating_callback, pattern="^rate_"),
            ],
            CONFIRM_SUBMIT: [
                CallbackQueryHandler(submit_callback),
            ],
            BUG_FEATURE_DESCRIBE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bug_feature_describe),
            ],
            BUG_FEATURE_ATTACHMENT: [
                CallbackQueryHandler(bug_feature_attachment_callback, pattern="^to_attachment$"),
                CallbackQueryHandler(menu_callback, pattern="^back_main$"),
            ],
            BUG_FEATURE_COLLECT: [
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL,
                    collect_attachment,
                ),
                CallbackQueryHandler(attachment_done_callback, pattern="^(skip_attachment|done_attachment)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            ],
            BUG_FEATURE_SUBMIT: [
                CallbackQueryHandler(submit_callback),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    logger.info("BlockVeil Support Bot v2 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
