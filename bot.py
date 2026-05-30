"""
BlockVeil Support Bot v4
========================
Changes in v4:
  - Attachment screen: only "Skip" shown initially; "Done (Next)" appears only after
    at least one attachment has been received
  - One active ticket per user: a user cannot start a new ticket while one is in progress
    (tracked via active_tickets table; cleared on submit or /cancel)
  - Admin FAQ Manager: full add / edit / remove FAQ entries stored in DB;
    user-facing FAQ reads from DB dynamically
  - Statistics page: added "Download All Users" button -> sends a CSV file
    containing username + user_id for every registered user

Deploy: Railway (Procfile -> worker: python bot.py)
Env vars: BOT_TOKEN, SUPPORT_GROUP_ID, BUG_FEATURE_GROUP_ID, DB_PATH (optional)
"""

import csv
import io
import asyncio
import os
import sqlite3
import logging
import zoneinfo
from datetime import datetime, timezone

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
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN          = os.environ["BOT_TOKEN"]
SUPPORT_GROUP_ID   = int(os.environ["SUPPORT_GROUP_ID"])
BUG_FEATURE_GROUP_ID = int(os.environ["BUG_FEATURE_GROUP_ID"])
DB_PATH            = os.environ.get("DB_PATH", "blockveil_support.db")

# ---------------------------------------------------------------------------
# Maintenance & Broadcast state
# ---------------------------------------------------------------------------

# In-memory maintenance flag (loaded from DB on startup, persisted on change)
_maintenance_on: bool = False

# Per-chat pending state for admin text-input flows (broadcast, set message, etc.)
_admin_pending: dict = {}   # chat_id -> {"step": str, ...}

# ---------------------------------------------------------------------------
# Conversation States  (user-side)
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
    TIMEZONE_INPUT,
) = range(12)

# Admin conversation states
(
    ADMIN_DASHBOARD,
    ADMIN_PRODUCT_LIST,
    ADMIN_PRODUCT_ADD_NAME,
    ADMIN_PRODUCT_DETAIL,
    ADMIN_PRODUCT_EDIT_NAME,
    ADMIN_FAQ_LIST,
    ADMIN_FAQ_ADD_Q,
    ADMIN_FAQ_ADD_A,
    ADMIN_FAQ_DETAIL,
    ADMIN_FAQ_EDIT_Q,
    ADMIN_FAQ_EDIT_A,
    ADMIN_MAINTENANCE_MSG,
    ADMIN_BROADCAST,
    ADMIN_BROADCAST_MSG,
) = range(20, 34)

TYPE_SUPPORT = "support"
TYPE_BUG     = "bug"
TYPE_FEATURE = "feature"

POPULAR_TIMEZONES = [
    ("🇧🇩 Dhaka (UTC+6)",        "Asia/Dhaka"),
    ("🇮🇳 Kolkata (UTC+5:30)",   "Asia/Kolkata"),
    ("🇵🇰 Karachi (UTC+5)",      "Asia/Karachi"),
    ("🇦🇪 Dubai (UTC+4)",        "Asia/Dubai"),
    ("🇹🇷 Istanbul (UTC+3)",     "Europe/Istanbul"),
    ("🇬🇧 London (UTC+0/1)",     "Europe/London"),
    ("🇺🇸 New York (UTC-5/-4)",  "America/New_York"),
    ("🇺🇸 Los Angeles (UTC-8)",  "America/Los_Angeles"),
    ("🇸🇬 Singapore (UTC+8)",    "Asia/Singapore"),
    ("🇯🇵 Tokyo (UTC+9)",        "Asia/Tokyo"),
]

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                full_name TEXT    NOT NULL,
                username  TEXT,
                joined_at TEXT    NOT NULL,
                timezone  TEXT    DEFAULT 'Asia/Dhaka'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   TEXT    NOT NULL UNIQUE,
                user_id     INTEGER NOT NULL,
                ticket_type TEXT    NOT NULL,
                app_name    TEXT,
                description TEXT,
                rating      INTEGER,
                created_at  TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL UNIQUE,
                created_at TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS faq (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT    NOT NULL,
                answer     TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_tickets (
                user_id    INTEGER PRIMARY KEY,
                started_at TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
    # Load persisted maintenance state into memory
    _load_maintenance_state()
    logger.info("Database initialized at %s", DB_PATH)


# --- User helpers ---
def upsert_user(user) -> None:
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


def get_user_row(user_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def set_user_timezone(user_id: int, tz: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE users SET timezone = ? WHERE user_id = ?", (tz, user_id))
        conn.commit()


def get_all_users():
    with get_db() as conn:
        return conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()


# --- Ticket helpers ---
def save_ticket(ticket_id_str, user_id, ticket_type, app_name, description, rating) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO tickets (ticket_id, user_id, ticket_type, app_name, description, rating, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticket_id_str, user_id, ticket_type, app_name, description, rating, now_iso))
        conn.commit()


def get_ticket_stats(user_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*)                     AS total,
                SUM(ticket_type='support')   AS support_count,
                SUM(ticket_type='bug')       AS bug_count,
                SUM(ticket_type='feature')   AS feature_count
            FROM tickets WHERE user_id = ?
        """, (user_id,)).fetchone()
    return {
        "total":   row["total"]         or 0,
        "support": row["support_count"] or 0,
        "bug":     row["bug_count"]     or 0,
        "feature": row["feature_count"] or 0,
    }


def get_global_stats() -> dict:
    with get_db() as conn:
        t = conn.execute("""
            SELECT
                COUNT(*)                     AS total,
                SUM(ticket_type='support')   AS support_count,
                SUM(ticket_type='bug')       AS bug_count,
                SUM(ticket_type='feature')   AS feature_count
            FROM tickets
        """).fetchone()
        u = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()
        recent = conn.execute("""
            SELECT t.ticket_id, t.ticket_type, t.app_name, t.created_at,
                   u.full_name, u.username
            FROM tickets t LEFT JOIN users u ON t.user_id = u.user_id
            ORDER BY t.created_at DESC LIMIT 5
        """).fetchall()
    return {
        "total_tickets":  t["total"]         or 0,
        "support":        t["support_count"] or 0,
        "bug":            t["bug_count"]     or 0,
        "feature":        t["feature_count"] or 0,
        "total_users":    u["cnt"]           or 0,
        "recent":         recent,
    }


def get_all_tickets(limit=20):
    with get_db() as conn:
        return conn.execute("""
            SELECT t.*, u.full_name, u.username
            FROM tickets t LEFT JOIN users u ON t.user_id = u.user_id
            ORDER BY t.created_at DESC LIMIT ?
        """, (limit,)).fetchall()


# --- Product helpers ---
def get_products():
    with get_db() as conn:
        return conn.execute("SELECT * FROM products ORDER BY created_at ASC").fetchall()


def add_product(name: str) -> bool:
    """Returns False if name already exists."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db() as conn:
            conn.execute("INSERT INTO products (name, created_at) VALUES (?, ?)", (name, now_iso))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def edit_product(product_id: int, new_name: str) -> bool:
    try:
        with get_db() as conn:
            conn.execute("UPDATE products SET name = ? WHERE id = ?", (new_name, product_id))
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def delete_product(product_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()


def get_product_by_id(product_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


# --- Active ticket lock helpers ---
def set_active_ticket(user_id: int) -> None:
    """Mark user as having an in-progress ticket."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("""
            INSERT INTO active_tickets (user_id, started_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET started_at = excluded.started_at
        """, (user_id, now_iso))
        conn.commit()


def clear_active_ticket(user_id: int) -> None:
    """Remove the in-progress ticket lock for this user."""
    with get_db() as conn:
        conn.execute("DELETE FROM active_tickets WHERE user_id = ?", (user_id,))
        conn.commit()


def has_active_ticket(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM active_tickets WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row is not None


# --- FAQ helpers ---
def get_faqs():
    with get_db() as conn:
        return conn.execute("SELECT * FROM faq ORDER BY id ASC").fetchall()


def get_faq_by_id(faq_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM faq WHERE id = ?", (faq_id,)).fetchone()


def add_faq(question: str, answer: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO faq (question, answer, created_at) VALUES (?, ?, ?)",
            (question, answer, now_iso),
        )
        conn.commit()


def edit_faq(faq_id: int, question: str, answer: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE faq SET question = ?, answer = ? WHERE id = ?",
            (question, answer, faq_id),
        )
        conn.commit()


def delete_faq(faq_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM faq WHERE id = ?", (faq_id,))
        conn.commit()


# --- CSV export helper ---
def export_users_csv() -> bytes:
    """Return a UTF-8 CSV of all users as bytes: username, user_id, full_name, joined_at."""
    users = get_all_users()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["username", "user_id", "full_name", "joined_at"])
    for u in users:
        writer.writerow([
            f"@{u['username']}" if u['username'] else "",
            u['user_id'],
            u['full_name'],
            u['joined_at'][:19].replace("T", " "),
        ])
    return buf.getvalue().encode("utf-8")


# --- Maintenance setting helpers ---
def _load_maintenance_state() -> None:
    """Load maintenance flag from DB into memory."""
    global _maintenance_on
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'maintenance'",
        ).fetchone()
    _maintenance_on = (row["value"] == "1") if row else False


def _save_maintenance_state(value: bool) -> None:
    global _maintenance_on
    _maintenance_on = value
    with get_db() as conn:
        conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('maintenance', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if value else "0",),
        )
        conn.commit()


def is_maintenance() -> bool:
    return _maintenance_on


def get_maintenance_message() -> str:
    """Return admin-set maintenance message or a sensible default."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'maintenance_message'",
        ).fetchone()
    if row and row["value"]:
        return row["value"]
    return (
        "🔧 *BlockVeil Support is under maintenance.*\n\n"
        "We are making improvements to serve you better.\n"
        "Please check back soon. Thank you for your patience!"
    )


def save_maintenance_message(text: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('maintenance_message', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (text,),
        )
        conn.commit()


# --- Broadcast helper ---
async def auto_delete_msg(message, delay: int = 30) -> None:
    """Delete a message after `delay` seconds. Used to keep the admin group clean."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def username_display(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


def gen_ticket_id(user_id: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
    return f"BV-{ts}-{user_id % 10000:04d}"


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def format_date(iso_str: str, tz_name: str = "UTC") -> str:
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(zoneinfo.ZoneInfo(tz_name))
        return dt.strftime("%-d %B %Y")
    except Exception:
        return iso_str[:10]


def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"


def is_admin_group(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == BUG_FEATURE_GROUP_ID

# ---------------------------------------------------------------------------
# User-side Keyboards
# ---------------------------------------------------------------------------

def make_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛟  Need Support",    callback_data="menu_support")],
        [InlineKeyboardButton("🐛  Report Bug",      callback_data="menu_bug"),
         InlineKeyboardButton("💡  Request Feature", callback_data="menu_feature")],
        [InlineKeyboardButton("🎫  View My Tickets", callback_data="menu_tickets"),
         InlineKeyboardButton("❓  FAQ",             callback_data="menu_faq")],
        [InlineKeyboardButton("👤  Profile",         callback_data="menu_profile")],
    ])


def make_product_select_keyboard(flow: str) -> InlineKeyboardMarkup:
    """
    Build dynamic product buttons for Support or Bug flow.
    flow = 'support' -> callback prefix 'app_'
    flow = 'bug'     -> callback prefix 'bugapp_'
    Always appends an 'Others' button and a Back button.
    """
    prefix = "app_" if flow == TYPE_SUPPORT else "bugapp_"
    rows = []
    for p in get_products():
        safe_name = p["name"].replace(" ", "_")
        rows.append([InlineKeyboardButton(
            f"📦  {p['name']}",
            callback_data=f"{prefix}prod_{p['id']}_{safe_name}"
        )])
    rows.append([InlineKeyboardButton("🔧  Others", callback_data=f"{prefix}others")])
    rows.append([InlineKeyboardButton("⬅️  Back",   callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def make_next_keyboard(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("➡️  Next", callback_data=cb)]])


def make_skip_only_keyboard() -> InlineKeyboardMarkup:
    """Shown before any attachment is received: only Skip."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭️  Skip", callback_data="skip_attachment"),
    ]])


def make_skip_done_keyboard() -> InlineKeyboardMarkup:
    """Shown after at least one attachment is received: Skip + Done."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭️  Skip",        callback_data="skip_attachment"),
        InlineKeyboardButton("✅  Done (Next)", callback_data="done_attachment"),
    ]])


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
    rows = [[InlineKeyboardButton(label, callback_data=f"tz_{key}")] for label, key in POPULAR_TIMEZONES]
    rows.append([InlineKeyboardButton("⬅️  Back to Profile", callback_data="back_profile")])
    return InlineKeyboardMarkup(rows)


def make_back_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠  Main Menu", callback_data="back_main")]])


def make_back_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️  Back to Profile", callback_data="back_profile")]])

# ---------------------------------------------------------------------------
# Admin Keyboards
# ---------------------------------------------------------------------------

def make_admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥  User Info",    callback_data="adm_user_info"),
         InlineKeyboardButton("🔧  Maintenance",  callback_data="adm_maintenance")],
        [InlineKeyboardButton("📢  Broadcast",    callback_data="adm_broadcast"),
         InlineKeyboardButton("🛡️  User Control", callback_data="adm_user_control")],
        [InlineKeyboardButton("📊  Statistics",   callback_data="adm_statistics"),
         InlineKeyboardButton("📦  Product",      callback_data="adm_product")],
        [InlineKeyboardButton("❓  FAQ",          callback_data="adm_faq"),
         InlineKeyboardButton("🎫  Ticket Info",  callback_data="adm_ticket_info")],
        [InlineKeyboardButton("💾  Backup",        callback_data="adm_backup")],
    ])


def make_product_list_keyboard() -> InlineKeyboardMarkup:
    """Show all existing products as buttons + Add New Product + Back."""
    rows = []
    for p in get_products():
        rows.append([InlineKeyboardButton(
            f"📦  {p['name']}",
            callback_data=f"adm_prod_detail_{p['id']}"
        )])
    rows.append([InlineKeyboardButton("➕  Add New Product", callback_data="adm_prod_add")])
    rows.append([InlineKeyboardButton("⬅️  Back",           callback_data="adm_back_dash")])
    return InlineKeyboardMarkup(rows)


def make_product_detail_keyboard(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️  Edit",   callback_data=f"adm_prod_edit_{product_id}"),
         InlineKeyboardButton("🗑️  Remove", callback_data=f"adm_prod_remove_{product_id}")],
        [InlineKeyboardButton("⬅️  Back",   callback_data="adm_product")],
    ])


def make_admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️  Back to Dashboard", callback_data="adm_back_dash")]])


def make_faq_list_keyboard() -> InlineKeyboardMarkup:
    """All FAQ entries as buttons + Add New + Back."""
    rows = []
    for f in get_faqs():
        short_q = f["question"][:40] + ("..." if len(f["question"]) > 40 else "")
        rows.append([InlineKeyboardButton(
            f"❓  {short_q}",
            callback_data=f"adm_faq_detail_{f['id']}"
        )])
    rows.append([InlineKeyboardButton("➕  Add New FAQ",  callback_data="adm_faq_add")])
    rows.append([InlineKeyboardButton("⬅️  Back",         callback_data="adm_back_dash")])
    return InlineKeyboardMarkup(rows)


def make_faq_detail_keyboard(faq_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️  Edit",   callback_data=f"adm_faq_edit_{faq_id}"),
         InlineKeyboardButton("🗑️  Remove", callback_data=f"adm_faq_remove_{faq_id}")],
        [InlineKeyboardButton("⬅️  Back",   callback_data="adm_faq")],
    ])

# ---------------------------------------------------------------------------
# /start  (routes by chat type)
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat

    # In any group that is NOT the admin group -> silently ignore
    if chat.type != "private" and chat.id != BUG_FEATURE_GROUP_ID:
        return ConversationHandler.END

    # Admin dashboard in BUG_FEATURE_GROUP
    if chat.id == BUG_FEATURE_GROUP_ID:
        context.user_data.clear()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stats = get_global_stats()
        text = (
            "🔐 *BlockVeil Admin Dashboard*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now}\n"
            f"👥 Total Users: {stats['total_users']}\n"
            f"🎫 Total Tickets: {stats['total_tickets']}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Select a section below:"
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_dashboard_keyboard(),
        )
        return ADMIN_DASHBOARD

    # Private chat -> user main menu
    context.user_data.clear()
    user = update.effective_user
    upsert_user(user)

    # Block users when maintenance is active
    if is_maintenance():
        await update.message.reply_text(
            get_maintenance_message(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"👋 *Welcome to BlockVeil Support, {user.first_name}!*\n\n"
        "We are here to help you. Select an option below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_menu_keyboard(),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Admin Dashboard Callbacks
# ---------------------------------------------------------------------------

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    # --- Back to dashboard ---
    if data == "adm_back_dash":
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stats = get_global_stats()
        await query.edit_message_text(
            "🔐 *BlockVeil Admin Dashboard*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🕐 {now}\n"
            f"👥 Total Users: {stats['total_users']}\n"
            f"🎫 Total Tickets: {stats['total_tickets']}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Select a section below:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_dashboard_keyboard(),
        )
        return ADMIN_DASHBOARD

    # --- User Info ---
    if data == "adm_user_info":
        users = get_all_users()
        if not users:
            body = "_No users registered yet._"
        else:
            lines = []
            for u in users[:20]:
                uname = f"@{u['username']}" if u['username'] else u['full_name']
                date  = u['joined_at'][:10]
                lines.append(f"• {uname} (`{u['user_id']}`) — Joined: {date}")
            body = "\n".join(lines)
            if len(users) > 20:
                body += f"\n\n_...and {len(users) - 20} more._"
        await query.edit_message_text(
            f"👥 *User Info*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Total registered: {len(users)}\n\n"
            f"{body}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_back_keyboard(),
        )
        return ADMIN_DASHBOARD

    # --- Statistics ---
    if data == "adm_statistics":
        s = get_global_stats()
        recent_lines = []
        for r in s["recent"]:
            uname = f"@{r['username']}" if r['username'] else r['full_name'] or "Unknown"
            recent_lines.append(
                f"• `{r['ticket_id']}` | {r['ticket_type'].upper()} | {uname}"
            )
        recent_text = "\n".join(recent_lines) if recent_lines else "_No tickets yet._"
        await query.edit_message_text(
            "📊 *Statistics*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Users:    {s['total_users']}\n"
            f"🎫 Total Tickets:  {s['total_tickets']}\n"
            f"🛟 Support:        {s['support']}\n"
            f"🐛 Bug Reports:    {s['bug']}\n"
            f"💡 Feature Reqs:   {s['feature']}\n\n"
            f"*Recent Tickets:*\n{recent_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥  Download All Users (CSV)", callback_data="adm_download_users")],
                [InlineKeyboardButton("⬅️  Back to Dashboard", callback_data="adm_back_dash")],
            ]),
        )
        return ADMIN_DASHBOARD

    # --- Download All Users CSV ---
    if data == "adm_download_users":
        users = get_all_users()
        if not users:
            await query.answer("No users to export.", show_alert=True)
            return ADMIN_DASHBOARD
        csv_bytes = export_users_csv()
        now_str   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        filename  = f"blockveil_users_{now_str}.csv"
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=io.BytesIO(csv_bytes),
            filename=filename,
            caption=f"👥 *All Users Export*\n{len(users)} users | {now_str} UTC",
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer("CSV sent above.", show_alert=False)
        return ADMIN_DASHBOARD

    # --- Ticket Info ---
    if data == "adm_ticket_info":
        tickets = get_all_tickets(15)
        if not tickets:
            body = "_No tickets found._"
        else:
            lines = []
            for t in tickets:
                uname = f"@{t['username']}" if t['username'] else t['full_name'] or "Unknown"
                lines.append(
                    f"• `{t['ticket_id']}` [{t['ticket_type'].upper()}]\n"
                    f"  User: {uname} | App: {t['app_name'] or 'N/A'}\n"
                    f"  Date: {t['created_at'][:10]}"
                )
            body = "\n\n".join(lines)
        await query.edit_message_text(
            f"🎫 *Ticket Info* (last 15)\n━━━━━━━━━━━━━━━━━━━━\n\n{body}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_back_keyboard(),
        )
        return ADMIN_DASHBOARD

    # --- Maintenance ---
    if data == "adm_maintenance":
        return await adm_maintenance_cb(update, context)

    # --- Maintenance toggle ---
    if data == "adm_maintenance_toggle":
        return await adm_maintenance_toggle_cb(update, context)

    # --- Maintenance set message ---
    if data == "adm_maintenance_set_msg":
        return await adm_maintenance_set_msg_cb(update, context)

    # --- Broadcast ---
    if data == "adm_broadcast":
        return await adm_broadcast_cb(update, context)

    # --- User Control ---
    if data == "adm_user_control":
        await query.edit_message_text(
            "🛡️ *User Control*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "User ban, unban, and restriction controls are coming soon.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_back_keyboard(),
        )
        return ADMIN_DASHBOARD

    # --- FAQ Manager (routes to ADMIN_FAQ_LIST state) ---
    if data == "adm_faq":
        faqs  = get_faqs()
        count = len(faqs)
        await query.edit_message_text(
            f"❓ *FAQ Manager*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total FAQ entries: {count}\n\n"
            "Tap an entry to edit or remove it, or add a new one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_faq_list_keyboard(),
        )
        return ADMIN_FAQ_LIST

    # --- Backup ---
    if data == "adm_backup":
        await query.edit_message_text(
            "💾 *Backup*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Database file: `{DB_PATH}`\n\n"
            "Automated backup scheduling is coming soon.\n"
            "For now, download the SQLite file from your Railway volume.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_admin_back_keyboard(),
        )
        return ADMIN_DASHBOARD

    # --- Product list ---
    if data == "adm_product":
        products = get_products()
        count = len(products)
        await query.edit_message_text(
            f"📦 *Product Manager*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total products: {count}\n\n"
            "Tap a product to edit or remove it.\n"
            "Products appear as buttons inside Need Support and Report Bug flows.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_list_keyboard(),
        )
        return ADMIN_PRODUCT_LIST

    return ADMIN_DASHBOARD


# --- Product List callbacks ---
async def admin_product_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_back_dash":
        return await admin_callback(update, context)  # Re-use back-to-dash handler

    # Add new product
    if data == "adm_prod_add":
        await query.edit_message_text(
            "📦 *Add New Product*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the product name you want to add.\n\n"
            "_Example: BlockVeil Wallet_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_product")
            ]]),
        )
        return ADMIN_PRODUCT_ADD_NAME

    # View product detail
    if data.startswith("adm_prod_detail_"):
        pid = int(data.split("_")[-1])
        prod = get_product_by_id(pid)
        if not prod:
            await query.edit_message_text(
                "Product not found.",
                reply_markup=make_product_list_keyboard(),
            )
            return ADMIN_PRODUCT_LIST
        await query.edit_message_text(
            f"📦 *Product: {prod['name']}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"ID: `{prod['id']}`\n"
            f"Added: {prod['created_at'][:10]}\n\n"
            "Choose an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_detail_keyboard(pid),
        )
        return ADMIN_PRODUCT_DETAIL

    return ADMIN_PRODUCT_LIST


async def admin_product_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends product name text."""
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text(
            "Product name cannot be empty. Please try again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_product")
            ]]),
        )
        return ADMIN_PRODUCT_ADD_NAME

    success = add_product(name)
    if not success:
        await update.message.reply_text(
            f"❌ A product named *{name}* already exists.\n\nTry a different name:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADMIN_PRODUCT_ADD_NAME

    products = get_products()
    await update.message.reply_text(
        f"✅ *Product Added:* {name}\n\n"
        f"Total products: {len(products)}\n\n"
        "It will now appear in user support and bug report flows.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦  Back to Products", callback_data="adm_product")],
            [InlineKeyboardButton("🏠  Dashboard",        callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_PRODUCT_LIST


async def admin_product_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "adm_product":
        products = get_products()
        await query.edit_message_text(
            f"📦 *Product Manager*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total products: {len(products)}\n\n"
            "Tap a product to edit or remove it.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_list_keyboard(),
        )
        return ADMIN_PRODUCT_LIST

    # Edit
    if data.startswith("adm_prod_edit_"):
        pid = int(data.split("_")[-1])
        prod = get_product_by_id(pid)
        context.user_data["editing_product_id"] = pid
        await query.edit_message_text(
            f"✏️ *Edit Product: {prod['name']}*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the new product name:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_product")
            ]]),
        )
        return ADMIN_PRODUCT_EDIT_NAME

    # Remove
    if data.startswith("adm_prod_remove_"):
        pid = int(data.split("_")[-1])
        prod = get_product_by_id(pid)
        name = prod["name"] if prod else "Unknown"
        delete_product(pid)
        products = get_products()
        await query.edit_message_text(
            f"🗑️ *Product Removed:* {name}\n\n"
            f"Remaining products: {len(products)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_list_keyboard(),
        )
        return ADMIN_PRODUCT_LIST

    return ADMIN_PRODUCT_DETAIL


async def admin_product_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_name = update.message.text.strip()
    pid = context.user_data.get("editing_product_id")

    if not new_name or not pid:
        await update.message.reply_text("Invalid. Please try again or cancel.")
        return ADMIN_PRODUCT_EDIT_NAME

    success = edit_product(pid, new_name)
    if not success:
        await update.message.reply_text(
            f"❌ A product named *{new_name}* already exists. Try a different name:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADMIN_PRODUCT_EDIT_NAME

    await update.message.reply_text(
        f"✅ *Product Updated:* {new_name}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📦  Back to Products", callback_data="adm_product")],
            [InlineKeyboardButton("🏠  Dashboard",        callback_data="adm_back_dash")],
        ]),
    )
    context.user_data.pop("editing_product_id", None)
    return ADMIN_PRODUCT_LIST

# ---------------------------------------------------------------------------
# Admin FAQ Manager Handlers
# ---------------------------------------------------------------------------

async def admin_faq_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles buttons on the FAQ list screen."""
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "adm_back_dash":
        return await admin_callback(update, context)

    # Show FAQ list (re-render)
    if data == "adm_faq":
        faqs = get_faqs()
        await query.edit_message_text(
            f"❓ *FAQ Manager*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total FAQ entries: {len(faqs)}\n\n"
            "Tap an entry to edit or remove it, or add a new one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_faq_list_keyboard(),
        )
        return ADMIN_FAQ_LIST

    # Start adding a new FAQ
    if data == "adm_faq_add":
        context.user_data.pop("new_faq_question", None)
        await query.edit_message_text(
            "➕ *Add New FAQ*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Step 1 of 2: Send the *question* text.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_faq")
            ]]),
        )
        return ADMIN_FAQ_ADD_Q

    # View FAQ detail
    if data.startswith("adm_faq_detail_"):
        fid  = int(data.split("_")[-1])
        faq  = get_faq_by_id(fid)
        if not faq:
            await query.edit_message_text("FAQ not found.", reply_markup=make_faq_list_keyboard())
            return ADMIN_FAQ_LIST
        await query.edit_message_text(
            f"❓ *FAQ Entry*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"*Q:* {faq['question']}\n\n"
            f"*A:* {faq['answer']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_faq_detail_keyboard(fid),
        )
        return ADMIN_FAQ_DETAIL

    return ADMIN_FAQ_LIST


async def admin_faq_add_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends the question text for a new FAQ."""
    q = update.message.text.strip()
    if not q:
        await update.message.reply_text("Question cannot be empty. Please try again.")
        return ADMIN_FAQ_ADD_Q
    context.user_data["new_faq_question"] = q
    await update.message.reply_text(
        f"✅ *Question saved.*\n\n_{q}_\n\n"
        "Step 2 of 2: Now send the *answer* text.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm_faq")
        ]]),
    )
    return ADMIN_FAQ_ADD_A


async def admin_faq_add_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends the answer text, completing the new FAQ entry."""
    a = update.message.text.strip()
    q = context.user_data.pop("new_faq_question", None)
    if not a or not q:
        await update.message.reply_text("Something went wrong. Please start over.")
        return ADMIN_FAQ_LIST
    add_faq(q, a)
    faqs = get_faqs()
    await update.message.reply_text(
        f"✅ *FAQ Added!*\n\n*Q:* {q}\n*A:* {a}\n\n"
        f"Total FAQ entries: {len(faqs)}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❓  Back to FAQ List", callback_data="adm_faq")],
            [InlineKeyboardButton("🏠  Dashboard",        callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_FAQ_LIST


async def admin_faq_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles Edit / Remove on a specific FAQ entry."""
    query = update.callback_query
    await query.answer()
    data  = query.data

    # Back to FAQ list
    if data == "adm_faq":
        faqs = get_faqs()
        await query.edit_message_text(
            f"❓ *FAQ Manager*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total FAQ entries: {len(faqs)}\n\n"
            "Tap an entry to edit or remove it, or add a new one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_faq_list_keyboard(),
        )
        return ADMIN_FAQ_LIST

    if data.startswith("adm_faq_edit_"):
        fid = int(data.split("_")[-1])
        faq = get_faq_by_id(fid)
        context.user_data["editing_faq_id"] = fid
        context.user_data.pop("editing_faq_new_q", None)
        await query.edit_message_text(
            f"✏️ *Edit FAQ*\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Current Q: _{faq['question']}_\n\n"
            "Step 1 of 2: Send the new *question* (or send the same to keep it).",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="adm_faq")
            ]]),
        )
        return ADMIN_FAQ_EDIT_Q

    if data.startswith("adm_faq_remove_"):
        fid  = int(data.split("_")[-1])
        faq  = get_faq_by_id(fid)
        q_preview = faq["question"][:60] if faq else "Unknown"
        delete_faq(fid)
        faqs = get_faqs()
        await query.edit_message_text(
            f"🗑️ *FAQ Removed.*\n\n_{q_preview}_\n\n"
            f"Remaining entries: {len(faqs)}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_faq_list_keyboard(),
        )
        return ADMIN_FAQ_LIST

    return ADMIN_FAQ_DETAIL


async def admin_faq_edit_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends the new question text when editing a FAQ."""
    q = update.message.text.strip()
    if not q:
        await update.message.reply_text("Question cannot be empty. Please try again.")
        return ADMIN_FAQ_EDIT_Q
    context.user_data["editing_faq_new_q"] = q
    fid = context.user_data.get("editing_faq_id")
    faq = get_faq_by_id(fid) if fid else None
    await update.message.reply_text(
        f"✅ *New question saved.*\n\n_{q}_\n\n"
        f"Current answer: _{faq['answer'] if faq else 'N/A'}_\n\n"
        "Step 2 of 2: Send the new *answer* (or send the same to keep it).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm_faq")
        ]]),
    )
    return ADMIN_FAQ_EDIT_A


async def admin_faq_edit_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sends the new answer, completing the FAQ edit."""
    a   = update.message.text.strip()
    fid = context.user_data.pop("editing_faq_id", None)
    q   = context.user_data.pop("editing_faq_new_q", None)
    if not a or not fid or not q:
        await update.message.reply_text("Something went wrong. Please start over.")
        return ADMIN_FAQ_LIST
    edit_faq(fid, q, a)
    await update.message.reply_text(
        f"✅ *FAQ Updated!*\n\n*Q:* {q}\n*A:* {a}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❓  Back to FAQ List", callback_data="adm_faq")],
            [InlineKeyboardButton("🏠  Dashboard",        callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_FAQ_LIST

# ---------------------------------------------------------------------------
# Admin: Maintenance
# ---------------------------------------------------------------------------

async def adm_maintenance_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show maintenance status with toggle + set message button."""
    query = update.callback_query
    await query.answer()
    on = is_maintenance()
    status = "🔴 *Maintenance Mode: ON*\n\nAll users are currently blocked." if on \
             else "✅ *Maintenance Mode: OFF*\n\nBot is live for all users."
    toggle_label = "🟢 Turn OFF Maintenance" if on else "🔴 Turn ON Maintenance"
    await query.edit_message_text(
        f"🔧 *Maintenance*\n━━━━━━━━━━━━━━━━━━━━\n\n{status}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_label,             callback_data="adm_maintenance_toggle")],
            [InlineKeyboardButton("✉️ Set Maintenance Message", callback_data="adm_maintenance_set_msg")],
            [InlineKeyboardButton("⬅️ Back to Dashboard",   callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_DASHBOARD


async def adm_maintenance_toggle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggle maintenance on/off."""
    query = update.callback_query
    await query.answer()
    new_state = not is_maintenance()
    _save_maintenance_state(new_state)
    on = new_state
    status = "🔴 *Maintenance Mode: ON*\n\nAll users are currently blocked." if on \
             else "✅ *Maintenance Mode: OFF*\n\nBot is live for all users."
    toggle_label = "🟢 Turn OFF Maintenance" if on else "🔴 Turn ON Maintenance"
    await query.edit_message_text(
        f"🔧 *Maintenance*\n━━━━━━━━━━━━━━━━━━━━\n\n{status}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_label,             callback_data="adm_maintenance_toggle")],
            [InlineKeyboardButton("✉️ Set Maintenance Message", callback_data="adm_maintenance_set_msg")],
            [InlineKeyboardButton("⬅️ Back to Dashboard",   callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_DASHBOARD


async def adm_maintenance_set_msg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask admin to type the new maintenance message."""
    query = update.callback_query
    await query.answer()
    _admin_pending[update.effective_chat.id] = {"step": "maintenance_msg_wait"}
    await query.edit_message_text(
        "✉️ *Set Maintenance Message*\n\n"
        "Send the message you want users to see when maintenance is ON.\n\n"
        "_Markdown formatting is supported._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="adm_maintenance")
        ]]),
    )
    return ADMIN_MAINTENANCE_MSG


# ---------------------------------------------------------------------------
# Admin: Broadcast
# ---------------------------------------------------------------------------

def _bc_menu_kb(mode: str, has_inline: bool = False) -> InlineKeyboardMarkup:
    """Keyboard shown after admin sends the broadcast message (before sending)."""
    rows = [
        [InlineKeyboardButton("➕ Add Inline Button", callback_data=f"adm_bc_add_inline_{mode}")],
    ]
    if has_inline:
        rows.append([InlineKeyboardButton("📤 Send", callback_data=f"adm_bc_send_{mode}")])
    else:
        rows.append([InlineKeyboardButton("📤 Send Directly", callback_data=f"adm_bc_send_{mode}")])
    rows.append([InlineKeyboardButton("🏠 Dashboard", callback_data="adm_back_dash")])
    return InlineKeyboardMarkup(rows)


async def adm_broadcast_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Broadcast entry: choose type."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📢 *Broadcast*\n━━━━━━━━━━━━━━━━━━━━\n\nChoose broadcast type:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Public Broadcast",        callback_data="adm_bc_public")],
            [InlineKeyboardButton("👤 Specific User",           callback_data="adm_bc_specific")],
            [InlineKeyboardButton("⬅️ Back to Dashboard",       callback_data="adm_back_dash")],
        ]),
    )
    return ADMIN_BROADCAST


async def adm_bc_public_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Public broadcast: wait for admin to send the message."""
    query = update.callback_query
    await query.answer()
    _admin_pending[update.effective_chat.id] = {"step": "adm_bc_msg_wait", "mode": "public"}
    await query.edit_message_text(
        "📢 *Public Broadcast*\n\nSend the message you want to broadcast to all users.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_BROADCAST_MSG


async def adm_bc_specific_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Specific user broadcast: ask for user ID first."""
    query = update.callback_query
    await query.answer()
    _admin_pending[update.effective_chat.id] = {"step": "adm_bc_specific_id_wait"}
    await query.edit_message_text(
        "👤 *Specific User Broadcast*\n\nSend the Telegram User ID of the target user.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_BROADCAST_MSG


async def adm_bc_add_inline_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin clicked Add Inline Button: ask for button name."""
    query = update.callback_query
    await query.answer()
    mode = query.data.replace("adm_bc_add_inline_", "")
    pending = _admin_pending.get(update.effective_chat.id, {})
    buttons = pending.get("inline_buttons", [])
    if len(buttons) >= 5:
        await query.answer("Maximum 5 inline buttons allowed.", show_alert=True)
        return ADMIN_BROADCAST_MSG
    pending["step"]          = "adm_bc_inline_name_wait"
    pending["mode"]          = mode
    _admin_pending[update.effective_chat.id] = pending
    await query.edit_message_text(
        f"➕ *Add Inline Button* ({len(buttons)}/5)\n\nSend the button label text.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADMIN_BROADCAST_MSG


async def adm_bc_send_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin clicked Send: broadcast the stored message."""
    query = update.callback_query
    await query.answer()
    mode    = query.data.replace("adm_bc_send_", "")
    chat_id = update.effective_chat.id
    pending = _admin_pending.pop(chat_id, {})
    bc_chat = pending.get("bc_chat_id")
    bc_msg  = pending.get("bc_msg_id")
    inline_buttons = pending.get("inline_buttons", [])

    if not bc_chat or not bc_msg:
        await query.edit_message_text(
            "No broadcast message stored. Please start again.",
            reply_markup=make_admin_back_keyboard(),
        )
        return ADMIN_DASHBOARD

    bc_kb = None
    if inline_buttons:
        bc_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn["text"], url=btn["url"])]
            for btn in inline_buttons
        ])

    # Specific user send
    if mode == "specific":
        target_id = pending.get("target_tid")
        if not target_id:
            await query.edit_message_text(
                "Target user not found. Please start again.",
                reply_markup=make_admin_back_keyboard(),
            )
            return ADMIN_DASHBOARD
        try:
            await context.bot.copy_message(
                chat_id=target_id,
                from_chat_id=bc_chat,
                message_id=bc_msg,
                reply_markup=bc_kb,
            )
            await query.edit_message_text(
                f"✅ Message sent to user `{target_id}`.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=make_admin_back_keyboard(),
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Failed to send: {e}",
                reply_markup=make_admin_back_keyboard(),
            )
        return ADMIN_DASHBOARD

    # Public: send to all users
    all_users = get_all_users()
    total     = len(all_users)
    sent = failed = 0
    failed_ids: list[int] = []

    progress = await context.bot.send_message(
        chat_id=chat_id,
        text=f"📢 Broadcasting to {total} user(s)... please wait.",
    )
    for u in all_users:
        tid = u["user_id"]
        try:
            await context.bot.copy_message(
                chat_id=tid,
                from_chat_id=bc_chat,
                message_id=bc_msg,
                reply_markup=bc_kb,
            )
            sent += 1
            await asyncio.sleep(0.05)   # respect Telegram rate limits
        except Exception:
            failed += 1
            failed_ids.append(tid)

    try:
        await progress.delete()
    except Exception:
        pass

    summary = (
        f"📢 *Broadcast Complete!*\n\n"
        f"✅ Sent:   {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total:  {total}"
    )
    await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode=ParseMode.MARKDOWN)

    if failed_ids:
        lines   = "\n".join(str(tid) for tid in failed_ids)
        header  = f"Broadcast Failed IDs\nTotal: {failed}\n{'='*30}\n"
        bio     = io.BytesIO((header + lines + "\n").encode("utf-8"))
        bio.name = "broadcast_failed.txt"
        await context.bot.send_document(
            chat_id=chat_id,
            document=bio,
            filename="broadcast_failed.txt",
            caption=f"⚠️ {failed} user(s) could not be reached.",
        )
    return ADMIN_DASHBOARD


async def admin_group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles all text messages sent in the admin group (BUG_FEATURE_GROUP).
    Used for multi-step admin flows: maintenance message, broadcast message/buttons.
    """
    chat_id = update.effective_chat.id
    if chat_id != BUG_FEATURE_GROUP_ID:
        return ADMIN_DASHBOARD

    pending = _admin_pending.get(chat_id, {})
    step    = pending.get("step", "")
    raw     = (update.message.text or "").strip() if update.message else ""

    # --- Maintenance message input ---
    if step == "maintenance_msg_wait":
        asyncio.create_task(auto_delete_msg(update.message, delay=10))
        if not raw:
            msg = await update.message.reply_text("Message cannot be empty. Please send the maintenance message text.")
            asyncio.create_task(auto_delete_msg(msg, delay=60))
            return ADMIN_MAINTENANCE_MSG
        save_maintenance_message(raw)
        _admin_pending.pop(chat_id, None)
        msg = await update.message.reply_text(
            "✅ Maintenance message saved successfully!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back to Maintenance", callback_data="adm_maintenance"),
            ]]),
        )
        asyncio.create_task(auto_delete_msg(msg, delay=60))
        return ADMIN_DASHBOARD

    # --- Broadcast: specific user ID ---
    if step == "adm_bc_specific_id_wait":
        asyncio.create_task(auto_delete_msg(update.message, delay=5))
        try:
            target_id = int(raw)
        except ValueError:
            msg = await update.message.reply_text("Invalid user ID. Please send a numeric Telegram User ID.")
            asyncio.create_task(auto_delete_msg(msg, delay=60))
            return ADMIN_BROADCAST_MSG
        _admin_pending[chat_id] = {
            "step":       "adm_bc_msg_wait",
            "mode":       "specific",
            "target_tid": target_id,
        }
        msg = await update.message.reply_text(
            f"User ID: `{target_id}`\n\nNow send the message you want to deliver to this user.",
            parse_mode=ParseMode.MARKDOWN,
        )
        asyncio.create_task(auto_delete_msg(msg, delay=120))
        return ADMIN_BROADCAST_MSG

    # --- Broadcast: receive the actual message (any media type) ---
    if step == "adm_bc_msg_wait":
        pending["bc_chat_id"]     = update.message.chat_id
        pending["bc_msg_id"]      = update.message.message_id
        pending["inline_buttons"] = pending.get("inline_buttons", [])
        _admin_pending[chat_id]   = pending
        mode       = pending.get("mode", "public")
        has_inline = len(pending["inline_buttons"]) > 0
        lbl        = "Specific user broadcast" if mode == "specific" else "Public broadcast"
        msg = await update.message.reply_text(
            f"✅ *{lbl} message received.*\n"
            f"Inline buttons: {len(pending['inline_buttons'])}/5\n\n"
            "Choose an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_bc_menu_kb(mode, has_inline=has_inline),
        )
        asyncio.create_task(auto_delete_msg(msg, delay=300))
        return ADMIN_BROADCAST_MSG

    # --- Broadcast: inline button name ---
    if step == "adm_bc_inline_name_wait":
        asyncio.create_task(auto_delete_msg(update.message, delay=5))
        if not raw:
            msg = await update.message.reply_text("Button name cannot be empty. Please try again.")
            asyncio.create_task(auto_delete_msg(msg, delay=60))
            return ADMIN_BROADCAST_MSG
        pending["inline_pending_name"] = raw
        pending["step"]                = "adm_bc_inline_url_wait"
        _admin_pending[chat_id]        = pending
        msg = await update.message.reply_text(
            f"Button name: *{raw}*\n\nNow send the URL for this button.",
            parse_mode=ParseMode.MARKDOWN,
        )
        asyncio.create_task(auto_delete_msg(msg, delay=120))
        return ADMIN_BROADCAST_MSG

    # --- Broadcast: inline button URL ---
    if step == "adm_bc_inline_url_wait":
        asyncio.create_task(auto_delete_msg(update.message, delay=5))
        url = raw
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("t.me")):
            msg = await update.message.reply_text(
                "Invalid URL. Please send a valid link starting with http://, https://, or t.me"
            )
            asyncio.create_task(auto_delete_msg(msg, delay=60))
            return ADMIN_BROADCAST_MSG
        buttons  = pending.get("inline_buttons", [])
        btn_name = pending.pop("inline_pending_name", "Button")
        if len(buttons) < 5:
            buttons.append({"text": btn_name, "url": url})
        pending["inline_buttons"] = buttons
        pending["step"]           = "adm_bc_msg_wait"
        _admin_pending[chat_id]   = pending
        mode = pending.get("mode", "public")
        msg  = await update.message.reply_text(
            f"✅ Inline button added: *{btn_name}*\nTotal: {len(buttons)}/5\n\nChoose an action:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_bc_menu_kb(mode, has_inline=True),
        )
        asyncio.create_task(auto_delete_msg(msg, delay=300))
        return ADMIN_BROADCAST_MSG

    return ADMIN_DASHBOARD


# ---------------------------------------------------------------------------
# User Main Menu Callbacks
# ---------------------------------------------------------------------------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_support":
        if has_active_ticket(query.from_user.id):
            await query.answer(
                "You already have an active ticket in progress. "
                "Please submit or cancel it before creating a new one.",
                show_alert=True,
            )
            return MAIN_MENU
        set_active_ticket(query.from_user.id)
        await query.edit_message_text(
            "🛟 *Need Support*\n\nWhich product do you need help with?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_select_keyboard(TYPE_SUPPORT),
        )
        return SELECT_APP

    elif data == "menu_bug":
        if has_active_ticket(query.from_user.id):
            await query.answer(
                "You already have an active ticket in progress. "
                "Please submit or cancel it before creating a new one.",
                show_alert=True,
            )
            return MAIN_MENU
        set_active_ticket(query.from_user.id)
        await query.edit_message_text(
            "🐛 *Report a Bug*\n\nWhich product has the bug?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_product_select_keyboard(TYPE_BUG),
        )
        return SELECT_APP

    elif data == "menu_feature":
        if has_active_ticket(query.from_user.id):
            await query.answer(
                "You already have an active ticket in progress. "
                "Please submit or cancel it before creating a new one.",
                show_alert=True,
            )
            return MAIN_MENU
        set_active_ticket(query.from_user.id)
        context.user_data["ticket_type"] = TYPE_FEATURE
        context.user_data["app_name"]    = "BlockVeil"
        await query.edit_message_text(
            "💡 *Request a Feature*\n\nDescribe your feature idea in detail.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

    elif data == "menu_tickets":
        user  = query.from_user
        stats = get_ticket_stats(user.id)
        await query.edit_message_text(
            "🎫 *Your Tickets*\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total:             {stats['total']}\n"
            f"Support Tickets:   {stats['support']}\n"
            f"Bug Reports:       {stats['bug']}\n"
            f"Feature Requests:  {stats['feature']}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
        return MAIN_MENU

    elif data == "menu_faq":
        faqs = get_faqs()
        if faqs:
            lines = []
            for f in faqs:
                lines.append(f"*Q: {f['question']}*\nA: {f['answer']}")
            faq_body = "\n\n".join(lines)
        else:
            faq_body = "_No FAQ entries yet. Check back soon!_"
        await query.edit_message_text(
            f"❓ *FAQ*\n━━━━━━━━━━━━━━━━━━━━\n\n{faq_body}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
        return MAIN_MENU

    elif data in ("menu_profile", "back_profile"):
        return await show_profile(query)

    elif data == "back_main":
        return await show_main_menu(query)

    return MAIN_MENU


async def show_main_menu(query) -> int:
    user = query.from_user
    await query.edit_message_text(
        f"👋 *Welcome back, {user.first_name}!*\n\nSelect an option below:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_menu_keyboard(),
    )
    return MAIN_MENU


async def show_profile(query) -> int:
    user  = query.from_user
    row   = get_user_row(user.id)
    stats = get_ticket_stats(user.id)
    tz    = row["timezone"] if row else "Asia/Dhaka"
    joined_iso = row["joined_at"] if row else datetime.now(timezone.utc).isoformat()
    joined_str = format_date(joined_iso, tz)
    uname = f"@{user.username}" if user.username else "N/A"

    await query.edit_message_text(
        "👤 *My Profile*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📛 *Name:*      {user.full_name}\n"
        f"🔗 *Username:*  {uname}\n"
        f"🆔 *User ID:*   `{user.id}`\n"
        f"📅 *Joined:*    {joined_str}\n"
        f"🌍 *Timezone:*  `{tz}`\n\n"
        "📊 *My Activity*\n━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 *Total Tickets Created:*    {stats['total']}\n"
        f"🛟 *Support Tickets:*          {stats['support']}\n"
        f"🐛 *Bug Reports:*              {stats['bug']}\n"
        f"💡 *Feature Requests:*         {stats['feature']}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍  Change Timezone", callback_data="change_timezone")],
            [InlineKeyboardButton("⬅️  Back",            callback_data="back_main")],
        ]),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Timezone Flow
# ---------------------------------------------------------------------------

async def change_timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🌍 *Change Timezone*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "Select from the list below, or type any valid IANA timezone name.\n\n"
        "_Example: Asia/Dhaka, Europe/Paris, America/Chicago_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_timezone_keyboard(),
    )
    return TIMEZONE_INPUT


async def timezone_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    tz_name = query.data[3:]  # Strip "tz_"
    set_user_timezone(query.from_user.id, tz_name)
    await query.edit_message_text(
        f"✅ *Timezone updated to:* `{tz_name}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_back_profile_keyboard(),
    )
    return MAIN_MENU


async def timezone_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tz_input = update.message.text.strip()
    try:
        zoneinfo.ZoneInfo(tz_input)
        valid = True
    except zoneinfo.ZoneInfoNotFoundError:
        valid = False

    if not valid:
        await update.message.reply_text(
            f"❌ *'{tz_input}'* is not a valid timezone.\n\n"
            "Use a valid IANA name, e.g. `Asia/Dhaka`, `Europe/London`, `America/New_York`\n\n"
            "Or select from the buttons above.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_profile_keyboard(),
        )
        return TIMEZONE_INPUT

    set_user_timezone(update.effective_user.id, tz_input)
    await update.message.reply_text(
        f"✅ *Timezone updated to:* `{tz_input}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_back_profile_keyboard(),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# App / Product Selection (shared state for both Support and Bug)
# ---------------------------------------------------------------------------

async def select_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data == "back_main":
        return await show_main_menu(query)

    # Determine flow from prefix
    if data.startswith("app_"):
        context.user_data["ticket_type"] = TYPE_SUPPORT
        raw = data[4:]   # Strip "app_"
    elif data.startswith("bugapp_"):
        context.user_data["ticket_type"] = TYPE_BUG
        raw = data[7:]   # Strip "bugapp_"
    else:
        return SELECT_APP

    # Resolve app name
    if raw == "others":
        app_name = "Others"
    elif raw.startswith("prod_"):
        parts    = raw.split("_", 2)   # prod, id, name
        app_name = parts[2].replace("_", " ") if len(parts) == 3 else "Unknown"
    else:
        app_name = raw.replace("_", " ").title()

    context.user_data["app_name"] = app_name
    ticket_type = context.user_data["ticket_type"]

    if ticket_type == TYPE_SUPPORT:
        await query.edit_message_text(
            f"✍️ *{app_name} - Support*\n\n"
            "Please describe your issue in detail.\n\n"
            "_After sending your message, you can optionally add attachments._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DESCRIBE_ISSUE
    else:
        await query.edit_message_text(
            f"🐛 *{app_name} - Bug Report*\n\n"
            "Please describe the bug in detail.\n\n"
            "_Steps to reproduce are very helpful._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

# ---------------------------------------------------------------------------
# Support Flow
# ---------------------------------------------------------------------------

async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []
    await update.message.reply_text(
        f"✅ *Message received.*\n\n_{update.message.text}_\n\n"
        "Click *Next* to add attachments or continue.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return AWAIT_ATTACHMENT


async def to_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    # Reset attachment list so count is fresh
    context.user_data["attachments"] = []
    await query.edit_message_text(
        "📎 *Attachments (Optional)*\n\n"
        "Send photos, videos, voice messages, or files now.\n\n"
        "You can send multiple files. When done, tap *Done (Next)*.\n"
        "To skip, tap *Skip*.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_only_keyboard(),   # No "Done" yet
    )
    return COLLECT_ATTACHMENT


async def collect_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg         = update.message
    attachments = context.user_data.setdefault("attachments", [])

    if msg.photo:
        attachments.append({"type": "photo",    "file_id": msg.photo[-1].file_id})
        label = "📷 Photo"
    elif msg.video:
        attachments.append({"type": "video",    "file_id": msg.video.file_id})
        label = "🎬 Video"
    elif msg.voice:
        attachments.append({"type": "voice",    "file_id": msg.voice.file_id})
        label = "🎤 Voice message"
    elif msg.document:
        attachments.append({"type": "document", "file_id": msg.document.file_id})
        label = "📄 File"
    else:
        await msg.reply_text(
            "⚠️ Please send a photo, video, voice message, or file.\nOr tap *Skip*.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_skip_only_keyboard(),
        )
        return COLLECT_ATTACHMENT

    # At least 1 attachment exists: show Skip + Done
    await msg.reply_text(
        f"✅ {label} added! (Total: {len(attachments)})\n\nSend more, or tap *Done (Next)* to continue.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_done_keyboard(),
    )
    return COLLECT_ATTACHMENT


async def attachment_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ticket_type = context.user_data.get("ticket_type")

    if ticket_type in (TYPE_BUG, TYPE_FEATURE):
        await query.edit_message_text(
            "🎉 *Almost done!*\n\nClick *Submit* to send your report.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_submit_keyboard(),
        )
        return BUG_FEATURE_SUBMIT

    await query.edit_message_text(
        "⭐ *Rate Your Experience*\n\n"
        "How would you rate this support session? (Optional)\n\n"
        "Click *Next (No Rating)* to skip.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_rating_keyboard(),
    )
    return RATE_EXPERIENCE


async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["rating"] = None if query.data == "rate_skip" else int(query.data.split("_")[1])
    await query.edit_message_text(
        "🎉 *Almost done!*\n\nClick *Submit Ticket* to send your request.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_submit_keyboard(),
    )
    return CONFIRM_SUBMIT

# ---------------------------------------------------------------------------
# Bug / Feature Flow
# ---------------------------------------------------------------------------

async def bug_feature_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []
    await update.message.reply_text(
        f"✅ *Received:*\n\n_{update.message.text}_\n\nClick *Next* to add attachments.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return BUG_FEATURE_ATTACHMENT


async def bug_feature_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["attachments"] = []
    await query.edit_message_text(
        "📎 *Attachments (Optional)*\n\n"
        "Send screenshots, videos, or files.\n\n"
        "Tap *Done (Next)* when finished or *Skip* to continue.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_only_keyboard(),   # No "Done" until first file received
    )
    return BUG_FEATURE_COLLECT

# ---------------------------------------------------------------------------
# Submit (shared)
# ---------------------------------------------------------------------------

async def submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        clear_active_ticket(query.from_user.id)
        context.user_data.clear()
        return await show_main_menu(query)

    user  = query.from_user
    upsert_user(user)
    ud    = context.user_data
    ticket_type = ud.get("ticket_type", TYPE_SUPPORT)
    tid   = gen_ticket_id(user.id)
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    app_name    = ud.get("app_name", "Unknown")
    description = ud.get("description", "N/A")
    rating_val  = ud.get("rating")

    type_labels = {
        TYPE_SUPPORT: "🛟 Support Request",
        TYPE_BUG:     "🐛 Bug Report",
        TYPE_FEATURE: "💡 Feature Request",
    }
    header = (
        f"*{type_labels.get(ticket_type, 'Ticket')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket ID: `{tid}`\n"
        f"👤 User: {username_display(user)} (`{user.id}`)\n"
        f"📱 Product: {app_name}\n"
        f"🕐 Time: {now}\n"
        f"⭐ Rating: {stars(rating_val) if rating_val else 'Not rated'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 *Description:*\n{description}"
    )

    attachments  = ud.get("attachments", [])
    target_group = SUPPORT_GROUP_ID if ticket_type == TYPE_SUPPORT else BUG_FEATURE_GROUP_ID

    try:
        await context.bot.send_message(
            chat_id=target_group,
            text=header,
            parse_mode=ParseMode.MARKDOWN,
        )
        for att in attachments:
            fid, t = att["file_id"], att["type"]
            if t == "photo":
                await context.bot.send_photo(chat_id=target_group, photo=fid)
            elif t == "video":
                await context.bot.send_video(chat_id=target_group, video=fid)
            elif t == "voice":
                await context.bot.send_voice(chat_id=target_group, voice=fid)
            elif t == "document":
                await context.bot.send_document(chat_id=target_group, document=fid)

        save_ticket(tid, user.id, ticket_type, app_name, description, rating_val)
        clear_active_ticket(user.id)   # Release the lock

        await query.edit_message_text(
            f"✅ *Ticket Submitted Successfully!*\n\n"
            f"🎫 Ticket ID: `{tid}`\n\n"
            "Our team will get back to you within 24 to 48 hours.\n"
            "Thank you for using BlockVeil Support!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )
    except Exception as e:
        logger.error("Failed to forward ticket %s: %s", tid, e)
        await query.edit_message_text(
            "❌ *Something went wrong.*\n\nPlease try again later.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_back_main_keyboard(),
        )

    context.user_data.clear()
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_private(update):
        await update.message.reply_text("Please use the buttons above.")


async def ignore_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Silently ignore everything in groups (except admin group /start, handled above)."""
    pass


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user:
        clear_active_ticket(update.effective_user.id)
    context.user_data.clear()
    if is_private(update):
        await update.message.reply_text(
            "Cancelled. Use /start to begin again.",
            reply_markup=ReplyKeyboardRemove(),
        )
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update exception: %s", context.error, exc_info=context.error)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # --- User-side conversation (private chats only) ---
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start, filters=filters.ChatType.PRIVATE)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(change_timezone_callback, pattern="^change_timezone$"),
                CallbackQueryHandler(timezone_button_callback, pattern="^tz_"),
                CallbackQueryHandler(menu_callback),
            ],
            TIMEZONE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, timezone_text_input),
                CallbackQueryHandler(timezone_button_callback, pattern="^tz_"),
                CallbackQueryHandler(menu_callback, pattern="^(back_profile|back_main)$"),
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

    # --- Admin conversation (BUG_FEATURE_GROUP only) ---
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler(
                "start", cmd_start,
                filters=filters.Chat(BUG_FEATURE_GROUP_ID),
            )
        ],
        states={
            ADMIN_DASHBOARD: [
                CallbackQueryHandler(admin_callback),
            ],
            ADMIN_PRODUCT_LIST: [
                CallbackQueryHandler(admin_product_list_callback),
            ],
            ADMIN_PRODUCT_ADD_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_product_add_name,
                ),
                CallbackQueryHandler(admin_callback, pattern="^adm_product$"),
            ],
            ADMIN_PRODUCT_DETAIL: [
                CallbackQueryHandler(admin_product_detail_callback),
            ],
            ADMIN_PRODUCT_EDIT_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_product_edit_name,
                ),
                CallbackQueryHandler(admin_callback, pattern="^adm_product$"),
            ],
            ADMIN_FAQ_LIST: [
                CallbackQueryHandler(admin_faq_list_callback),
            ],
            ADMIN_FAQ_ADD_Q: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_faq_add_question,
                ),
                CallbackQueryHandler(admin_faq_list_callback, pattern="^adm_faq$"),
            ],
            ADMIN_FAQ_ADD_A: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_faq_add_answer,
                ),
                CallbackQueryHandler(admin_faq_list_callback, pattern="^adm_faq$"),
            ],
            ADMIN_FAQ_DETAIL: [
                CallbackQueryHandler(admin_faq_detail_callback),
            ],
            ADMIN_FAQ_EDIT_Q: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_faq_edit_question,
                ),
                CallbackQueryHandler(admin_faq_list_callback, pattern="^adm_faq$"),
            ],
            ADMIN_FAQ_EDIT_A: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_faq_edit_answer,
                ),
                CallbackQueryHandler(admin_faq_list_callback, pattern="^adm_faq$"),
            ],
            ADMIN_MAINTENANCE_MSG: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_group_message_handler,
                ),
                CallbackQueryHandler(adm_maintenance_cb, pattern="^adm_maintenance$"),
                CallbackQueryHandler(admin_callback, pattern="^adm_back_dash$"),
            ],
            ADMIN_BROADCAST: [
                CallbackQueryHandler(adm_bc_public_cb,   pattern="^adm_bc_public$"),
                CallbackQueryHandler(adm_bc_specific_cb, pattern="^adm_bc_specific$"),
                CallbackQueryHandler(admin_callback,     pattern="^adm_back_dash$"),
            ],
            ADMIN_BROADCAST_MSG: [
                # Any message type: text, photo, video, voice, document, sticker, etc.
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.VOICE |
                     filters.Document.ALL | filters.Sticker.ALL) &
                    ~filters.COMMAND & filters.Chat(BUG_FEATURE_GROUP_ID),
                    admin_group_message_handler,
                ),
                CallbackQueryHandler(adm_bc_add_inline_cb, pattern="^adm_bc_add_inline_"),
                CallbackQueryHandler(adm_bc_send_cb,       pattern="^adm_bc_send_"),
                CallbackQueryHandler(adm_broadcast_cb,     pattern="^adm_broadcast$"),
                CallbackQueryHandler(admin_callback,       pattern="^adm_back_dash$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    # --- Ignore all non-/start messages in groups ---
    group_ignore = MessageHandler(
        filters.ChatType.GROUPS & ~filters.COMMAND,
        ignore_group_message,
    )

    app.add_handler(user_conv)
    app.add_handler(admin_conv)
    app.add_handler(group_ignore)
    app.add_error_handler(error_handler)

    logger.info("BlockVeil Support Bot v4 (Maintenance + Broadcast) starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
