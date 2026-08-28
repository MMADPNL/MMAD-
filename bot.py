# ============================================================
# BOT.PY
# Telegram Group Games Bot
# Python 3.10+
# python-telegram-bot 20+
#
# بازی‌ها:
# 🎲 تاس
# 🎳 بولینگ
# 🏀 بسکتبال
# 🎯 دارت
#
# امکانات:
# 💰 موجودی TRX داخلی
# 💸 انتقال با Reply
# 👥 بازی با دوستان
# 📤 درخواست
# 👑 پنل مدیریت
# 🛡️ ضد موجودی
#
# نکته:
# TRX این برنامه اعتبار داخلی است و به شبکه TRON متصل نیست.
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from contextlib import closing

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# تنظیمات
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_FILE = "bot.db"

START_BALANCE = 10
MIN_GAME = 1
MAX_GAME = 1_000_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():
    with closing(db()) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance INTEGER DEFAULT 10,
            blocked INTEGER DEFAULT 0,
            virtual_user INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            creator_id INTEGER,
            opponent_id INTEGER,
            game_type TEXT,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            winner_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.commit()


def ensure_user(user):
    if not user:
        return

    with closing(db()) as con:

        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,)
        ).fetchone()

        if row:
            con.execute("""
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """, (
                user.username or "",
                user.first_name or "",
                user.id
            ))

        else:
            con.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance)
            VALUES (?, ?, ?, ?)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                START_BALANCE
            ))

        con.commit()


def get_user(user_id):
    with closing(db()) as con:
        return con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0

    return int(row["balance"])


def change_balance(user_id, amount):
    with closing(db()) as con:

        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        old = int(row["balance"])
        new = old + int(amount)

        if new < 0:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new, user_id)
        )

        con.commit()

        return True


def set_balance(user_id, amount):
    with closing(db()) as con:

        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        amount = max(0, int(amount))

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (amount, user_id)
        )

        con.commit()

        return True


def is_blocked(user_id):
    row = get_user(user_id)

    return bool(row and row["blocked"])


def is_virtual_user(user_id):
    row = get_user(user_id)

    return bool(row and row["virtual_user"])


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return text.translate(table)


def parse_amount(text):
    text = normalize_digits(text)

    text = text.replace(",", "")
    text = text.replace("٬", "")
    text = text.strip()

    m = re.search(r"\d+", text)

    if not m:
        return None

    amount = int(m.group())

    if amount < MIN_GAME:
        return None

    if amount > MAX_GAME:
        return None

    return amount


def name_of(user):
    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


# ============================================================
# KEYBOARDS
# ============================================================

def user_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["👥 بازی با دوستان", "💸 انتقال"],
            ["📤 درخواست", "📖 راهنما"]
        ],
        resize_keyboard=True
    )


def game_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎲 تاس",
                callback_data="game_dice"
            ),
            InlineKeyboardButton(
                "🎳 بولینگ",
                callback_data="game_bowling"
            )
        ],
        [
            InlineKeyboardButton(
                "🏀 بسکتبال",
                callback_data="game_basketball"
            ),
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="game_darts"
            )
        ]
    ])


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin_users"
            ),
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add"
            ),
            InlineKeyboardButton(
                "➖ کاهش موجودی",
                callback_data="admin_remove"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌ها",
                callback_data="admin_requests"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "⛔ دسترسی شما مسدود شده است."
        )

        return

    if update.effective_chat.type != ChatType.PRIVATE:

        await update.message.reply_text(
            "👋 سلام\n"
            "برای استفاده از منوی کاربری، ربات را در خصوصی باز کن."
        )

        return

    await update.message.reply_text(
        "👋 سلام!\n\n"
        "به ربات خوش آمدی.",
        reply_markup=user_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    amount = get_balance(user.id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"💎 {amount:,} TRX"
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    await update.message.reply_text(
        "🎮 انتخاب بازی:",
        reply_markup=game_keyboard()
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_NAMES = {
    "تاس": "dice",
    "dice": "dice",

    "بولینگ": "bowling",
    "بولينگ": "bowling",
    "bowling": "bowling",

    "بسکتبال": "basketball",
    "بسكتبال": "basketball",
    "basketball": "basketball",

    "دارت": "darts",
    "darts": "darts",
}


def parse_game(text):

    text = normalize_digits(text or "").strip()

    m = re.match(
        r"^1\s+([^\s]+)\s+(\d+)$",
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    game_name = m.group(1).lower()
    amount = int(m.group(2))

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    return game, amount


# ============================================================
# TELEGRAM DICE
# ============================================================

GAME_EMOJI = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯"
}


async def send_roll(bot, chat_id, game):

    return await bot.send_dice(
        chat_id=chat_id,
        emoji=GAME_EMOJI[game]
    )


# ============================================================
# SINGLE GAME
# ============================================================

async def play_game(update, context, game, amount):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    if amount < MIN_GAME:
        await update.message.reply_text(
            "❌ مقدار نامعتبر است."
        )
        return

    if amount > MAX_GAME:
        await update.message.reply_text(
            "❌ مقدار بیش از حد مجاز است."
        )
        return

    balance_now = get_balance(user.id)

    if balance_now < amount:

        await update.message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {balance_now:,} TRX"
        )

        return

    if not change_balance(user.id, -amount):

        await update.message.reply_text(
            "❌ خطا در موجودی."
        )

        return

    try:

        result = await send_roll(
            context.bot,
            update.effective_chat.id,
            game
        )

        value = result.dice.value

        if game == "dice":
            max_value = 6

        elif game == "bowling":
            max_value = 6

        elif game == "basketball":
            max_value = 5

        else:
            max_value = 6

        if value == max_value:

            reward = amount * 2

        elif value >= max_value - 1:

            reward = amount

        else:

            reward = 0

        if reward:
            change_balance(user.id, reward)

        new_balance = get_balance(user.id)

        if reward:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"🎉 نتیجه: {value}\n\n"
                    f"➕ برد: {reward:,} TRX\n"
                    f"💰 موجودی: {new_balance:,} TRX"
                )
            )

        else:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    f"🎯 نتیجه: {value}\n\n"
                    f"➖ مبلغ بازی: {amount:,} TRX\n"
                    f"💰 موجودی: {new_balance:,} TRX"
                )
            )

    except Exception:

        change_balance(user.id, amount)

        logger.exception("GAME ERROR")

        await update.message.reply_text(
            "❌ بازی اجرا نشد؛ موجودی برگشت داده شد."
        )


# ============================================================
# FRIEND GAME
# ============================================================

async def friends(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await update.message.reply_text(
            "👥 این بخش فقط در گپ قابل استفاده است."
        )

        return

    await update.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "یکی از این پیام‌ها را بفرست:\n\n"
        "🎲 1 تاس 100\n"
        "🎳 1 بولینگ 100\n"
        "🏀 1 بسکتبال 100\n"
        "🎯 1 دارت 100\n\n"
        "بازیکن دوم می‌تواند روی همان پیام Reply کند."
    )


# ============================================================
# FRIEND REPLY GAME
# ============================================================

async def friend_reply_game(
    update,
    context,
    game,
    amount
):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:
        return False

    original = message.reply_to_message

    if not original.from_user:
        return False

    creator = original.from_user

    if creator.id == user.id:
        return False

    ensure_user(creator)
    ensure_user(user)

    if is_virtual_user(creator.id):
        return True

    if is_virtual_user(user.id):
        return True

    if is_blocked(creator.id) or is_blocked(user.id):
        return True

    if get_balance(creator.id) < amount:

        await message.reply_text(
            "❌ موجودی بازیکن اول کافی نیست."
        )

        return True

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی بازیکن دوم کافی نیست."
        )

        return True

    if not change_balance(creator.id, -amount):
        return True

    if not change_balance(user.id, -amount):

        change_balance(creator.id, amount)

        return True

    try:

        roll1 = await send_roll(
            context.bot,
            message.chat_id,
            game
        )

        await asyncio.sleep(1)

        roll2 = await send_roll(
            context.bot,
            message.chat_id,
            game
        )

        value1 = roll1.dice.value
        value2 = roll2.dice.value

        if value1 == value2:

            change_balance(
                creator.id,
                amount
            )

            change_balance(
                user.id,
                amount
            )

            await message.reply_text(
                "🤝 مساوی شد!\n\n"
                f"هر دو نفر {amount:,} TRX خود را پس گرفتند."
            )

            return True

        if value1 > value2:

            winner = creator
            loser = user

        else:

            winner = user
            loser = creator

        change_balance(
            winner.id,
            amount * 2
        )

        await message.reply_text(
            "🏆 بازی تمام شد!\n\n"
            f"👑 برنده: {name_of(winner)}\n"
            f"🎯 نتیجه برنده: "
            f"{value1 if winner.id == creator.id else value2}\n"
            f"🎯 نتیجه بازنده: "
            f"{value2 if winner.id == creator.id else value1}\n\n"
            f"💰 جایزه: {amount * 2:,} TRX"
        )

        return True

    except Exception:

        change_balance(
            creator.id,
            amount
        )

        change_balance(
            user.id,
            amount
        )

        logger.exception("FRIEND GAME ERROR")

        await message.reply_text(
            "❌ خطا؛ موجودی هر دو نفر برگشت داده شد."
        )

        return True


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    message = update.message

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال باید روی پیام کاربر Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 3"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ مقصد پیدا نشد."
        )

        return

    if target.id == user.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if target.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    amount = parse_amount(message.text)

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر است.\n"
            "مثال: انتقال 3"
        )

        return

    ensure_user(target)

    if is_virtual_user(target.id):
        await message.reply_text(
            "❌ انتقال به این کاربر مجاز نیست."
        )
        return

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(user.id, -amount):
        return

    if not change_balance(target.id, amount):

        change_balance(user.id, amount)

        await message.reply_text(
            "❌ انتقال انجام نشد."
        )

        return

    with closing(db()) as con:

        con.execute("""
        INSERT INTO transfers
        (sender_id, receiver_id, amount)
        VALUES (?, ?, ?)
        """, (
            user.id,
            target.id,
            amount
        ))

        con.commit()

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {amount:,} TRX"
    )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    await update.message.reply_text(
        "📤 درخواست\n\n"
        "در این نسخه درخواست‌ها فقط داخل سیستم ثبت می‌شوند.\n"
        "انتقال واقعی TRX انجام نمی‌شود.\n\n"
        "برای ثبت درخواست:\n"
        "درخواست 3\n\n"
        "سپس یک متن/شناسه برای درخواست بفرست."
    )

    context.user_data["request_mode"] = True


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی‌ها در گپ:\n"
        "1 تاس 3\n"
        "1 بولینگ 3\n"
        "1 بسکتبال 3\n"
        "1 دارت 3\n\n"
        "💰 موجودی\n"
        "💸 انتقال 3 ← با Reply\n"
        "👥 بازی با دوستان\n"
        "📤 درخواست\n\n"
        "TRX نمایش‌داده‌شده در این ربات "
        "اعتبار داخلی سیستم است و انتقال واقعی "
        "روی شبکه TRON انجام نمی‌شود."
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):

        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if not is_admin(user.id):

        await query.edit_message_text(
            "⛔ دسترسی ندارید."
        )

        return

    data = query.data

    # -------------------------
    # USERS
    # -------------------------

    if data == "admin_users":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT user_id, first_name, username,
                   balance, blocked
            FROM users
            ORDER BY balance DESC
            LIMIT 20
            """).fetchall()

        text = "👥 کاربران\n\n"

        for i, row in enumerate(rows, 1):

            name = (
                row["first_name"]
                or row["username"]
                or str(row["user_id"])
            )

            status = "🚫" if row["blocked"] else "✅"

            text += (
                f"{i}. {status} {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {row['balance']:,} TRX\n\n"
            )

        await query.edit_message_text(text)

        return

    # -------------------------
    # STATS
    # -------------------------

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = con.execute(
                "SELECT COALESCE(SUM(balance),0) FROM users"
            ).fetchone()[0]

            requests = con.execute(
                "SELECT COUNT(*) FROM requests WHERE status='pending'"
            ).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: {total:,} TRX\n"
            f"📤 درخواست‌های در انتظار: {requests:,}"
        )

        return

    # -------------------------
    # ADD
    # -------------------------

    if data == "admin_add":

        context.user_data["admin_action"] = "add"

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "دستور زیر را در پیوی بفرست:\n\n"
            "/addbalance USER_ID AMOUNT"
        )

        return

    # -------------------------
    # REMOVE
    # -------------------------

    if data == "admin_remove":

        context.user_data["admin_action"] = "remove"

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "دستور زیر را در پیوی بفرست:\n\n"
            "/removebalance USER_ID AMOUNT"
        )

        return

    # -------------------------
    # REQUESTS
    # -------------------------

    if data == "admin_requests":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT *
            FROM requests
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT 20
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "📋 درخواست در انتظار وجود ندارد."
            )

            return

        text = "📋 درخواست‌های در انتظار\n\n"

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 کاربر: {row['user_id']}\n"
                f"💰 مقدار: {row['amount']:,} TRX\n"
                f"📝 اطلاعات: {row['wallet']}\n"
                f"📅 {row['created_at']}\n\n"
            )

        await query.edit_message_text(text)

        return


# ============================================================
# ADMIN ADD BALANCE
# ============================================================

async def add_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "فرمت صحیح:\n"
            "/addbalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(context.args[0])
        )

        amount = int(
            normalize_digits(context.args[1])
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    change_balance(target_id, amount)

    await update.message.reply_text(
        f"✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {amount:,} TRX\n"
        f"💰 موجودی جدید: {get_balance(target_id):,} TRX"
    )


# ============================================================
# ADMIN REMOVE BALANCE
# ============================================================

async def remove_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "فرمت صحیح:\n"
            "/removebalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(context.args[0])
        )

        amount = int(
            normalize_digits(context.args[1])
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if amount <= 0:
        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if not change_balance(target_id, -amount):

        await update.message.reply_text(
            "❌ موجودی کاربر کافی نیست."
        )

        return

    await update.message.reply_text(
        f"✅ موجودی کاهش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➖ {amount:,} TRX\n"
        f"💰 موجودی جدید: {get_balance(target_id):,} TRX"
    )


# ============================================================
# BLOCK
# ============================================================

async def block(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 1:
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        return

    with closing(db()) as con:

        con.execute(
            "UPDATE users SET blocked=1 WHERE user_id=?",
            (target_id,)
        )

        con.commit()

    await update.message.reply_text(
        f"🚫 کاربر {target_id} مسدود شد."
    )


async def unblock(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 1:
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        return

    with closing(db()) as con:

        con.execute(
            "UPDATE users SET blocked=0 WHERE user_id=?",
            (target_id,)
        )

        con.commit()

    await update.message.reply_text(
        f"✅ کاربر {target_id} رفع مسدودی شد."
    )


# ============================================================
# GAME CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    game = query.data.replace(
        "game_",
        ""
    )

    names = {
        "dice": "🎲 تاس",
        "bowling": "🎳 بولینگ",
        "basketball": "🏀 بسکتبال",
        "darts": "🎯 دارت"
    }

    if game not in names:
        return

    short_name = {
        "dice": "تاس",
        "bowling": "بولینگ",
        "basketball": "بسکتبال",
        "darts": "دارت"
    }[game]

    await query.message.reply_text(
        f"{names[game]}\n\n"
        f"مثال:\n"
        f"1 {short_name} 3"
    )


# ============================================================
# REQUEST CREATOR
# ============================================================

async def create_request(
    user_id,
    amount,
    wallet
):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO requests
        (user_id, amount, wallet)
        VALUES (?, ?, ?)
        """, (
            user_id,
            amount,
            wallet
        ))

        con.commit()


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    text = message.text.strip()
    normalized = normalize_digits(text)

    # --------------------------------------------------------
    # ADMIN REQUEST MODE
    # --------------------------------------------------------

    if context.user_data.get("request_mode"):

        if is_virtual_user(user.id):
            return

        amount = parse_amount(normalized)

        if amount:

            context.user_data["request_amount"] = amount
            context.user_data["request_mode"] = "wallet"

            await message.reply_text(
                "📝 مقدار ثبت شد.\n\n"
                "حالا اطلاعات درخواست را بفرست."
            )

            return

        if context.user_data.get("request_mode") == "wallet":

            amount = context.user_data.get(
                "request_amount"
            )

            if amount:

                await create_request(
                    user.id,
                    amount,
                    text
                )

                context.user_data.clear()

                await message.reply_text(
                    "✅ درخواست ثبت شد."
                )

                return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    parsed = parse_game(normalized)

    if parsed:

        game, amount = parsed

        # If this is a Reply to an existing game
        if message.reply_to_message:

            original_text = (
                message.reply_to_message.text or ""
            )

            original_game = parse_game(
                original_text
            )

            if original_game:

                original_type, original_amount = original_game

                if (
                    original_type == game
                    and original_amount == amount
                ):

                    await friend_reply_game(
                        update,
                        context,
                        game,
                        amount
                    )

                    return

        await play_game(
            update,
            context,
            game,
            amount
        )

        return

    # --------------------------------------------------------
    # MENU
    # --------------------------------------------------------

    if text == "💰 موجودی":

        if not is_virtual_user(user.id):
            await balance(update, context)

        return

    if text == "🎮 بازی":

        if not is_virtual_user(user.id):
            await game_menu(update, context)

        return

    if text == "👥 بازی با دوستان":

        if not is_virtual_user(user.id):
            await friends(update, context)

        return

    if text == "💸 انتقال":

        if not is_virtual_user(user.id):
            await transfer(update, context)

        return

    if text == "📤 درخواست":

        if not is_virtual_user(user.id):
            await request_menu(update, context)

        return

    if text == "📖 راهنما":

        await help_command(update, context)

        return

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if re.match(
        r"^(انتقال|transfer)\s+\d+$",
        normalized,
        re.IGNORECASE
    ):

        if not is_virtual_user(user.id):
            await transfer(update, context)

        return

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

    request_match = re.match(
        r"^(درخواست|request)\s+(\d+)$",
        normalized,
        re.IGNORECASE
    )

    if request_match:

        amount = int(
            request_match.group(2)
        )

        if amount < 1:
            return

        context.user_data["request_amount"] = amount
        context.user_data["request_mode"] = "wallet"

        await message.reply_text(
            "📝 مقدار ثبت شد.\n\n"
            "حالا اطلاعات درخواست را بفرست."
        )

        return


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):

    logger.error(
        "BOT ERROR",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance
        )
    )

    application.add_handler(
        CommandHandler(
            "game",
            game_menu
        )
    )

    application.add_handler(
        CommandHandler(
            "friends",
            friends
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    application.add_handler(
        CommandHandler(
            "addbalance",
            add_balance
        )
    )

    application.add_handler(
        CommandHandler(
            "removebalance",
            remove_balance
        )
    )

    application.add_handler(
        CommandHandler(
            "block",
            block
        )
    )

    application.add_handler(
        CommandHandler(
            "unblock",
            unblock
        )
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # Text messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "🚀 BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
