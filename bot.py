"""
BlockVeil Support Bot
=====================
Telegram-based support bot for BlockVeil.
Handles: Support tickets, Bug reports, Feature requests, FAQ, Profile, Tickets.

Flow:
  /start -> Main Menu
  Need Support -> Select App -> Describe Issue -> Attachments (optional) -> Rating -> Submit -> Forward to Support Group
  Report Bug / Request Feature -> Description -> Attachments -> Submit -> Forward to Bug/Feature Group

Admin Groups:
  SUPPORT_GROUP_ID   -> Need Support messages
  BUG_FEATURE_GROUP_ID -> Bug reports and feature requests

Deploy: Railway (via Procfile)
"""

import os
import logging
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
# Environment Variables
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPPORT_GROUP_ID = int(os.environ["SUPPORT_GROUP_ID"])          # Need Support messages
BUG_FEATURE_GROUP_ID = int(os.environ["BUG_FEATURE_GROUP_ID"]) # Bug / Feature messages

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

    # Bug / Feature flow (reuses DESCRIBE_ISSUE, AWAIT_ATTACHMENT, COLLECT_ATTACHMENT)
    BUG_FEATURE_DESCRIBE,
    BUG_FEATURE_ATTACHMENT,
    BUG_FEATURE_COLLECT,
    BUG_FEATURE_SUBMIT,
) = range(11)

# Ticket type constants
TYPE_SUPPORT = "support"
TYPE_BUG = "bug"
TYPE_FEATURE = "feature"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def username_display(user) -> str:
    """Return @username or Full Name if no username."""
    if user.username:
        return f"@{user.username}"
    return user.full_name


def ticket_id(user_id: int) -> str:
    """Generate a short readable ticket ID."""
    ts = datetime.now(timezone.utc).strftime("%y%m%d%H%M")
    return f"BV-{ts}-{user_id % 10000:04d}"


def stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def make_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛟  Need Support", callback_data="menu_support")],
        [InlineKeyboardButton("🐛  Report Bug",   callback_data="menu_bug"),
         InlineKeyboardButton("💡  Request Feature", callback_data="menu_feature")],
        [InlineKeyboardButton("🎫  View My Tickets", callback_data="menu_tickets"),
         InlineKeyboardButton("❓  FAQ",            callback_data="menu_faq")],
        [InlineKeyboardButton("👤  Profile",        callback_data="menu_profile")],
    ])


def make_app_select_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐  BlockVeil App", callback_data="app_blockveil")],
        [InlineKeyboardButton("⬅️  Back",           callback_data="back_main")],
    ])


def make_next_keyboard(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️  Next", callback_data=cb)],
    ])


def make_skip_next_keyboard(skip_cb: str, next_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️  Skip", callback_data=skip_cb),
         InlineKeyboardButton("✅  Done (Next)", callback_data=next_cb)],
    ])


def make_rating_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(1, 6):
        rows.append([InlineKeyboardButton(stars(i), callback_data=f"rate_{i}")])
    rows.append([InlineKeyboardButton("➡️  Next (No Rating)", callback_data="rate_skip")])
    return InlineKeyboardMarkup(rows)


def make_submit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨  Submit Ticket", callback_data="submit_confirm")],
        [InlineKeyboardButton("❌  Cancel",         callback_data="back_main")],
    ])

# ---------------------------------------------------------------------------
# /start  ->  Main Menu
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point. Shows main menu."""
    context.user_data.clear()  # Reset any previous flow state
    user = update.effective_user

    text = (
        f"👋 *Welcome to BlockVeil Support, {user.first_name}!*\n\n"
        "আমরা আপনাকে সাহায্য করতে এখানে আছি। নিচের অপশন থেকে একটি বেছে নিন:\n\n"
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

    if data == "menu_support":
        await query.edit_message_text(
            "🛟 *Need Support*\n\nকোন অ্যাপের জন্য সাপোর্ট দরকার?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_app_select_keyboard(),
        )
        return SELECT_APP

    elif data == "menu_bug":
        context.user_data["ticket_type"] = TYPE_BUG
        await query.edit_message_text(
            "🐛 *Report a Bug*\n\nআপনি যে বাগটি খুঁজে পেয়েছেন সেটি বিস্তারিতভাবে লিখুন।\n\n"
            "_Describe the bug in detail. Steps to reproduce are very helpful!_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

    elif data == "menu_feature":
        context.user_data["ticket_type"] = TYPE_FEATURE
        await query.edit_message_text(
            "💡 *Request a Feature*\n\nআপনার ফিচার আইডিয়াটি বিস্তারিত লিখুন।\n\n"
            "_Describe your feature request in detail._",
            parse_mode=ParseMode.MARKDOWN,
        )
        return BUG_FEATURE_DESCRIBE

    elif data == "menu_tickets":
        # Simple informational reply (no DB in this version)
        await query.edit_message_text(
            "🎫 *Your Tickets*\n\n"
            "এই ফিচারটি শীঘ্রই আসছে। আপাতত আপনার সাবমিট করা টিকেটগুলো গ্রুপে ফরওয়ার্ড হয়ে যায়।\n\n"
            "_Full ticket history is coming soon!_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️  Back", callback_data="back_main")
            ]]),
        )
        return MAIN_MENU

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
            "A: হ্যাঁ। সব ডেটা AES-256-GCM দিয়ে এনক্রিপ্ট থাকে।"
        )
        await query.edit_message_text(
            faq_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️  Back", callback_data="back_main")
            ]]),
        )
        return MAIN_MENU

    elif data == "menu_profile":
        user = update.effective_user
        profile_text = (
            f"👤 *Your Profile*\n\n"
            f"🆔 User ID: `{user.id}`\n"
            f"👤 Name: {user.full_name}\n"
            f"🔗 Username: {username_display(user)}\n"
            f"🌐 Language: {user.language_code or 'N/A'}"
        )
        await query.edit_message_text(
            profile_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️  Back", callback_data="back_main")
            ]]),
        )
        return MAIN_MENU

    elif data == "back_main":
        return await _show_main_menu(query)

    return MAIN_MENU


async def _show_main_menu(query) -> int:
    """Helper: edit current message to show main menu."""
    user = query.from_user
    text = (
        f"👋 *Welcome back, {user.first_name}!*\n\n"
        "নিচের অপশন থেকে একটি বেছে নিন:"
    )
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_main_menu_keyboard(),
    )
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Support Flow
# ---------------------------------------------------------------------------

async def select_app_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await _show_main_menu(query)

    if query.data == "app_blockveil":
        context.user_data["ticket_type"] = TYPE_SUPPORT
        context.user_data["app_name"] = "BlockVeil App"

        await query.edit_message_text(
            "✍️ *BlockVeil App Support*\n\n"
            "আপনার সমস্যাটি নিচে সুন্দর করে লিখুন।\n\n"
            "_লেখা শেষ হলে, আপনার পরবর্তী মেসেজে সমস্যাটি পাঠান।_\n\n"
            "📎 *Note:* লেখার পরে আপনি চাইলে attachment (ছবি/ভিডিও/ভয়েস) যোগ করতে পারবেন।",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DESCRIBE_ISSUE

    return SELECT_APP


async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends their issue description text."""
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []  # initialize attachment list

    await update.message.reply_text(
        f"✅ *আপনার বার্তা পাওয়া গেছে:*\n\n_{update.message.text}_\n\n"
        "এখন *Next* চাপুন attachment যোগ করতে অথবা সরাসরি এগিয়ে যেতে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return AWAIT_ATTACHMENT


async def to_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User clicks Next after description."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📎 *Attachment (Optional)*\n\n"
        "এখন আপনার *ছবি, ভিডিও বা ভয়েস মেসেজ* পাঠান।\n\n"
        "একাধিক ফাইল পাঠাতে পারবেন। সব শেষ হলে *Done (Next)* চাপুন।\n"
        "কিছু না পাঠাতে চাইলে *Skip* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_next_keyboard("skip_attachment", "done_attachment"),
    )
    return COLLECT_ATTACHMENT


async def collect_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends photo, video, or voice note."""
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
            "⚠️ শুধুমাত্র ছবি, ভিডিও, ভয়েস মেসেজ বা ফাইল পাঠান।\n"
            "অথবা *Done (Next)* চাপুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_skip_next_keyboard("skip_attachment", "done_attachment"),
        )
        return COLLECT_ATTACHMENT

    count = len(attachments)
    await msg.reply_text(
        f"✅ {label} যোগ হয়েছে! (মোট: {count}টি)\n\n"
        "আরও পাঠান অথবা *Done (Next)* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_next_keyboard("skip_attachment", "done_attachment"),
    )
    return COLLECT_ATTACHMENT


async def attachment_done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User finishes attachment phase (skip or done)."""
    query = update.callback_query
    await query.answer()

    ticket_type = context.user_data.get("ticket_type")

    # Bug/Feature flow goes directly to submit
    if ticket_type in (TYPE_BUG, TYPE_FEATURE):
        await query.edit_message_text(
            "🎉 *প্রায় শেষ!*\n\nনিচের *Submit* বাটনে চাপলে আপনার রিপোর্টটি পাঠানো হবে।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=make_submit_keyboard(),
        )
        return BUG_FEATURE_SUBMIT

    # Support flow -> rating
    await query.edit_message_text(
        "⭐ *Rate Your Experience*\n\n"
        "এই সাপোর্ট সেশনকে কতটা রেট দেবেন? (ঐচ্ছিক)\n\n"
        "না দিতে চাইলে *Next (No Rating)* চাপুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_rating_keyboard(),
    )
    return RATE_EXPERIENCE


async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User selects a star rating or skips."""
    query = update.callback_query
    await query.answer()

    if query.data == "rate_skip":
        context.user_data["rating"] = None
    else:
        rating = int(query.data.split("_")[1])
        context.user_data["rating"] = rating

    await query.edit_message_text(
        "🎉 *প্রায় শেষ!*\n\n"
        "নিচের *Submit Ticket* বাটন চাপলে আপনার টিকেটটি পাঠানো হবে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_submit_keyboard(),
    )
    return CONFIRM_SUBMIT


async def submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Final submit: forward ticket to the correct admin group."""
    query = update.callback_query
    await query.answer()

    if query.data == "back_main":
        return await _show_main_menu(query)

    user = query.from_user
    data = context.user_data
    tid = ticket_id(user.id)
    ticket_type = data.get("ticket_type", TYPE_SUPPORT)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # --- Build header for admin group message ---
    type_labels = {
        TYPE_SUPPORT: "🛟 Support Request",
        TYPE_BUG:     "🐛 Bug Report",
        TYPE_FEATURE: "💡 Feature Request",
    }
    type_label = type_labels.get(ticket_type, "Ticket")
    rating_val = data.get("rating")
    rating_str = stars(rating_val) if rating_val else "Not rated"
    app_name = data.get("app_name", "BlockVeil App")

    header = (
        f"*{type_label}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 Ticket ID: `{tid}`\n"
        f"👤 User: {username_display(user)} (`{user.id}`)\n"
        f"📱 App: {app_name}\n"
        f"🕐 Time: {now}\n"
        f"⭐ Rating: {rating_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📝 *Description:*\n{data.get('description', 'N/A')}"
    )

    attachments = data.get("attachments", [])
    target_group = SUPPORT_GROUP_ID if ticket_type == TYPE_SUPPORT else BUG_FEATURE_GROUP_ID

    try:
        # Send text header first
        await context.bot.send_message(
            chat_id=target_group,
            text=header,
            parse_mode=ParseMode.MARKDOWN,
        )

        # Send each attachment
        for att in attachments:
            fid = att["file_id"]
            att_type = att["type"]
            if att_type == "photo":
                await context.bot.send_photo(chat_id=target_group, photo=fid)
            elif att_type == "video":
                await context.bot.send_video(chat_id=target_group, video=fid)
            elif att_type == "voice":
                await context.bot.send_voice(chat_id=target_group, voice=fid)
            elif att_type == "document":
                await context.bot.send_document(chat_id=target_group, document=fid)

        # Confirm to user
        await query.edit_message_text(
            f"✅ *Ticket Submitted Successfully!*\n\n"
            f"🎫 Your Ticket ID: `{tid}`\n\n"
            f"আমাদের টিম শীঘ্রই আপনার সাথে যোগাযোগ করবে। ধন্যবাদ!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Main Menu", callback_data="back_main")
            ]]),
        )

    except Exception as e:
        logger.error(f"Failed to forward ticket {tid} to group: {e}")
        await query.edit_message_text(
            "❌ *কিছু একটা ভুল হয়েছে।*\n\nঅনুগ্রহ করে পরে আবার চেষ্টা করুন।",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Main Menu", callback_data="back_main")
            ]]),
        )

    context.user_data.clear()
    return MAIN_MENU

# ---------------------------------------------------------------------------
# Bug / Feature Flow
# ---------------------------------------------------------------------------

async def bug_feature_describe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive bug/feature description text."""
    context.user_data["description"] = update.message.text
    context.user_data["attachments"] = []
    ticket_type = context.user_data.get("ticket_type")
    label = "বাগটি" if ticket_type == TYPE_BUG else "ফিচার আইডিয়াটি"

    await update.message.reply_text(
        f"✅ *পাওয়া গেছে:*\n\n_{update.message.text}_\n\n"
        f"এখন *Next* চাপুন attachment যোগ করতে।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_next_keyboard("to_attachment"),
    )
    return BUG_FEATURE_ATTACHMENT


async def bug_feature_attachment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Bug/feature: show attachment screen."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "📎 *Attachment (Optional)*\n\n"
        "স্ক্রিনশট, ভিডিও বা অন্য ফাইল পাঠান।\n\n"
        "শেষ হলে *Done (Next)* চাপুন অথবা *Skip* করুন।",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_skip_next_keyboard("skip_attachment", "done_attachment"),
    )
    return BUG_FEATURE_COLLECT


# Bug/feature attachment collection reuses collect_attachment handler.
# The done callback routes to submit based on ticket_type (handled in attachment_done_callback).

# ---------------------------------------------------------------------------
# Fallback for unexpected text during attachment phase
# ---------------------------------------------------------------------------

async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remind users to use buttons during button-only phases."""
    await update.message.reply_text(
        "👆 অনুগ্রহ করে উপরের বাটন ব্যবহার করুন।",
    )

# ---------------------------------------------------------------------------
# Cancel command
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "❌ বাতিল করা হয়েছে। /start দিয়ে আবার শুরু করুন।",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Error Handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling update: {context.error}", exc_info=context.error)

# ---------------------------------------------------------------------------
# Application Setup
# ---------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler covering all flows
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(menu_callback),
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
                # Media messages
                MessageHandler(
                    filters.PHOTO | filters.VIDEO | filters.VOICE | filters.Document.ALL,
                    collect_attachment,
                ),
                # Next/Skip/Done buttons
                CallbackQueryHandler(attachment_done_callback, pattern="^(skip_attachment|done_attachment)$"),
                # Ignore stray text
                MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            ],
            RATE_EXPERIENCE: [
                CallbackQueryHandler(rating_callback, pattern="^rate_"),
            ],
            CONFIRM_SUBMIT: [
                CallbackQueryHandler(submit_callback),
            ],

            # Bug / Feature states
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
        allow_reentry=True,  # Allow /start to restart flow at any time
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    logger.info("BlockVeil Support Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
