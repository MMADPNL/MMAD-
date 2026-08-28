# ============================================================
# BOT.PY
# Telegram Group Game Bot
# Python 3.10+
# python-telegram-bot 20+
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
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    8552447077
}

DB_FILE = "bot.db"

MIN_BET = 1
MAX_BET = 1_000_000
MAX_ROLLS = 100

# سهم برنده و سهم سیستم
WINNER_SHARE = 0.92
HOUSE_SHARE = 0.08

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
        isolation_level=None
    )
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            virtual_user INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER,
            game_type TEXT NOT NULL,
            rolls INTEGER NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            creator_values TEXT DEFAULT '',
            opponent_values TEXT DEFAULT '',
            winner_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        row = con.execute("""
            SELECT value FROM settings
            WHERE key='withdraw_enabled'
        """).fetchone()

        if not row:
            con.execute("""
                INSERT INTO settings(key,value)
                VALUES('withdraw_enabled','0')
            """)


# ============================================================
# USERS
# ============================================================

def ensure_user(user):
    if not user:
        return

    with closing(db()) as con:

        row = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
        """, (user.id,)).fetchone()

        if row:
            con.execute("""
                UPDATE users
                SET username=?,
                    first_name=?
                WHERE user_id=?
            """, (
                user.username or "",
                user.first_name or "",
                user.id
            ))
        else:
            # موجودی اولیه عمداً صفر است
            con.execute("""
                INSERT INTO users
                (user_id, username, first_name, balance)
                VALUES (?, ?, ?, 0)
            """, (
                user.id,
                user.username or "",
                user.first_name or ""
            ))


def get_user(user_id):
    with closing(db()) as con:
        return con.execute("""
            SELECT *
            FROM users
            WHERE user_id=?
        """, (user_id,)).fetchone()


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0.0

    return float(row["balance"])


def is_blocked(user_id):
    row = get_user(user_id)
    return bool(row and row["blocked"])


def is_virtual_user(user_id):
    row = get_user(user_id)
    return bool(row and row["virtual_user"])


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# BALANCE SAFE OPERATIONS
# ============================================================

def change_balance(user_id, amount):
    """
    تغییر موجودی اتمیک.
    برای کم کردن موجودی، اجازه منفی شدن نمی‌دهد.
    """

    amount = float(amount)

    with closing(db()) as con:

        con.execute("BEGIN IMMEDIATE")

        row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            con.rollback()
            return False

        old_balance = float(row["balance"])
        new_balance = old_balance + amount

        if new_balance < 0:
            con.rollback()
            return False

        con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
        """, (
            new_balance,
            user_id
        ))

        con.commit()

        return True


def transfer_balance(sender_id, receiver_id, amount):
    """
    انتقال اتمیک بین دو کاربر.
    """

    amount = float(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            sender = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (sender_id,)).fetchone()

            receiver = con.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (receiver_id,)).fetchone()

            if not sender or not receiver:
                con.rollback()
                return False

            sender_balance = float(sender["balance"])

            if sender_balance < amount:
                con.rollback()
                return False

            con.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
            """, (
                amount,
                sender_id
            ))

            con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                amount,
                receiver_id
            ))

            con.execute("""
                INSERT INTO transfers
                (sender_id, receiver_id, amount)
                VALUES (?, ?, ?)
            """, (
                sender_id,
                receiver_id,
                amount
            ))

            con.commit()

            return True

        except Exception:
            con.rollback()
            logger.exception("TRANSFER ERROR")
            return False


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default="0"):
    with closing(db()) as con:

        row = con.execute("""
            SELECT value
            FROM settings
            WHERE key=?
        """, (key,)).fetchone()

        if not row:
            return default

        return row["value"]


def set_setting(key, value):
    with closing(db()) as con:

        con.execute("""
            INSERT INTO settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (
            key,
            str(value)
        ))


def withdrawal_enabled():
    return get_setting(
        "withdraw_enabled",
        "0"
    ) == "1"


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


def parse_number(text):
    text = normalize_digits(text or "")

    text = text.replace(",", ".")
    text = text.replace("٬", ".")
    text = text.strip()

    try:
        return float(text)
    except Exception:
        return None


def format_amount(value):
    value = float(value)

    if value.is_integer():
        return f"{int(value):,}"

    return f"{value:,.2f}".rstrip("0").rstrip(".")


def valid_bet(amount):
    return (
        amount is not None
        and amount >= MIN_BET
        and amount <= MAX_BET
    )


def name_of(user):
    if not user:
        return "کاربر"

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


# ============================================================
# GAME CONFIG
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

GAME_LABELS = {
    "dice": "🎲 تاس",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت",
}

GAME_EMOJI = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯",
}


# ============================================================
# GAME PARSER
# ============================================================

def parse_game(text):
    """
    پشتیبانی:
    1 تاس 0.1
    2 تاس 0.1
    ۲ تاس ۰.۱
    2 dice 0.1
    """

    text = normalize_digits(text or "").strip()

    pattern = r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:[.,][0-9]+)?)$"

    m = re.match(
        pattern,
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    rolls = int(m.group(1))

    game_name = m.group(2).lower()

    amount_text = m.group(3).replace(",", ".")

    try:
        amount = float(amount_text)
    except Exception:
        return None

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    if rolls < 1 or rolls > MAX_ROLLS:
        return None

    if not valid_bet(amount):
        return None

    return game, rolls, amount


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


def game_buttons(game, rolls, amount):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game}:{rolls}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot:{game}:{rolls}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="cancel_game"
            )
        ]
    ])


def admin_keyboard():
    withdraw_text = (
        "🟢 برداشت روشن"
        if withdrawal_enabled()
        else
        "🔴 برداشت خاموش"
    )

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
                withdraw_text,
                callback_data="admin_withdraw_toggle"
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

async def start(update, context):

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
            "👋 برای استفاده از منو، ربات را در خصوصی باز کن."
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
        f"💎 {format_amount(amount)} TRX"
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
        "🎮 بازی را انتخاب کن:\n\n"
        "مثال:\n"
        "۱ تاس ۰.۱\n"
        "۲ تاس ۰.۱\n"
        "۱ بولینگ ۰.۱\n"
        "۱ بسکتبال ۰.۱\n"
        "۱ دارت ۰.۱"
    )


# ============================================================
# FRIEND MENU
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
            "👥 بازی با دوستان فقط داخل گپ قابل استفاده است."
        )
        return

    await update.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "۱ تاس ۰.۱\n"
        "۲ تاس ۰.۱\n"
        "۱ بولینگ ۰.۱\n"
        "۱ بسکتبال ۰.۱\n"
        "۱ دارت ۰.۱\n\n"
        "بعد از ساخت بازی، نفر دیگر روی همان پیام "
        "Reply کند."
    )


# ============================================================
# SEND DICE
# ============================================================

async def send_roll(bot, chat_id, game):

    return await bot.send_dice(
        chat_id=chat_id,
        emoji=GAME_EMOJI[game]
    )


# ============================================================
# CREATE GAME MESSAGE
# ============================================================

async def create_game_message(
    update,
    context,
    game,
    rolls,
    amount
):

    user = update.effective_user

    balance_now = get_balance(user.id)

    if balance_now < amount:

        await update.message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {format_amount(balance_now)} TRX"
        )

        return

    # فقط سازنده مبلغ را قفل می‌کند
    if not change_balance(
        user.id,
        -amount
    ):
        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return

    try:

        sent = await update.message.reply_text(
            f"🎮 {GAME_LABELS[game]}\n\n"
            f"🎯 تعداد پرتاب: {rolls}\n"
            f"💰 مبلغ: {format_amount(amount)} TRX\n\n"
            f"یکی از گزینه‌ها را انتخاب کن:",
            reply_markup=game_buttons(
                game,
                rolls,
                amount
            )
        )

        with closing(db()) as con:

            con.execute("""
                INSERT INTO games
                (
                    chat_id,
                    message_id,
                    creator_id,
                    game_type,
                    rolls,
                    amount,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'waiting')
            """, (
                update.effective_chat.id,
                sent.message_id,
                user.id,
                game,
                rolls,
                amount
            ))

    except Exception:

        change_balance(
            user.id,
            amount
        )

        logger.exception(
            "CREATE GAME ERROR"
        )

        await update.message.reply_text(
            "❌ بازی ساخته نشد و مبلغ برگشت داده شد."
        )


# ============================================================
# BOT GAME
# ============================================================

async def start_bot_game(
    query,
    context,
    game,
    rolls,
    amount
):

    user = query.from_user

    ensure_user(user)

    if is_blocked(user.id) or is_virtual_user(user.id):
        return

    if get_balance(user.id) < amount:

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    # دکمه حذف شود
    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    if not change_balance(
        user.id,
        -amount
    ):

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    try:

        user_values = []

        # اول خود کاربر پرتاب می‌کند
        for _ in range(rolls):

            result = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            user_values.append(
                result.dice.value
            )

            await asyncio.sleep(0.8)

        # بعد ربات پرتاب می‌کند
        bot_values = []

        for _ in range(rolls):

            result = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            bot_values.append(
                result.dice.value
            )

            await asyncio.sleep(0.8)

        user_score = sum(user_values)
        bot_score = sum(bot_values)

        if user_score > bot_score:

            winner = "user"

        elif bot_score > user_score:

            winner = "bot"

        else:

            winner = "draw"

        if winner == "user":

            prize = round(
                amount * 2 * WINNER_SHARE,
                2
            )

            change_balance(
                user.id,
                prize
            )

            await query.message.reply_text(
                "🏆 شما برنده شدید!\n\n"
                f"👤 امتیاز شما: {user_score}\n"
                f"🤖 امتیاز ربات: {bot_score}\n\n"
                f"💰 دریافتی: {format_amount(prize)} TRX"
            )

        elif winner == "bot":

            await query.message.reply_text(
                "❌ شما باختید.\n\n"
                f"👤 امتیاز شما: {user_score}\n"
                f"🤖 امتیاز ربات: {bot_score}\n\n"
                f"💰 موجودی: "
                f"{format_amount(get_balance(user.id))} TRX"
            )

        else:

            change_balance(
                user.id,
                amount
            )

            await query.message.reply_text(
                "🤝 مساوی شد.\n\n"
                f"👤 {user_score}\n"
                f"🤖 {bot_score}\n\n"
                f"💰 مبلغ بازی برگشت داده شد."
            )

    except Exception:

        change_balance(
            user.id,
            amount
        )

        logger.exception(
            "BOT GAME ERROR"
        )

        await query.message.reply_text(
            "❌ خطا در بازی؛ مبلغ برگشت داده شد."
        )


# ============================================================
# FRIEND GAME
# ============================================================

async def start_friend_game(
    query,
    context,
    game,
    rolls,
    amount
):

    user = query.from_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    with closing(db()) as con:

        game_row = con.execute("""
            SELECT *
            FROM games
            WHERE chat_id=?
              AND message_id=?
              AND status='waiting'
            ORDER BY id DESC
            LIMIT 1
        """, (
            query.message.chat_id,
            query.message.message_id
        )).fetchone()

        if not game_row:
            await query.answer(
                "❌ این بازی دیگر فعال نیست.",
                show_alert=True
            )
            return

        creator_id = int(
            game_row["creator_id"]
        )

        if creator_id == user.id:

            await query.answer(
                "❌ خودت نمی‌توانی وارد بازی خودت شوی.",
                show_alert=True
            )

            return

        if get_balance(user.id) < amount:

            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )

            return

        # وضعیت فوراً تغییر می‌کند
        updated = con.execute("""
            UPDATE games
            SET opponent_id=?,
                status='playing'
            WHERE id=?
              AND status='waiting'
        """, (
            user.id,
            game_row["id"]
        )).rowcount

        if updated != 1:

            await query.answer(
                "❌ یک نفر دیگر وارد بازی شد.",
                show_alert=True
            )

            return

    # حذف دکمه‌ها
    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    # کسر مبلغ نفر دوم
    if not change_balance(
        user.id,
        -amount
    ):

        with closing(db()) as con:
            con.execute("""
                UPDATE games
                SET status='waiting',
                    opponent_id=NULL
                WHERE id=?
            """, (game_row["id"],))

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    try:

        creator_values = []
        opponent_values = []

        # اول سازنده
        await query.message.reply_text(
            f"🎮 {GAME_LABELS[game]}\n"
            f"👤 {name_of(await get_chat_member_user(context, creator_id)) if False else 'بازیکن اول'}\n\n"
            f"🎯 نوبت بازیکن اول"
        )

        for _ in range(rolls):

            result = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            creator_values.append(
                result.dice.value
            )

            await asyncio.sleep(0.8)

        # بعد نفر دوم
        await query.message.reply_text(
            "🎯 نوبت بازیکن دوم"
        )

        for _ in range(rolls):

            result = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            opponent_values.append(
                result.dice.value
            )

            await asyncio.sleep(0.8)

        creator_score = sum(
            creator_values
        )

        opponent_score = sum(
            opponent_values
        )

        if creator_score > opponent_score:
            winner_id = creator_id
            winner_score = creator_score
            loser_score = opponent_score

        elif opponent_score > creator_score:
            winner_id = user.id
            winner_score = opponent_score
            loser_score = creator_score

        else:
            change_balance(
                creator_id,
                amount
            )

            change_balance(
                user.id,
                amount
            )

            with closing(db()) as con:
                con.execute("""
                    UPDATE games
                    SET status='finished',
                        creator_values=?,
                        opponent_values=?
                    WHERE id=?
                """, (
                    ",".join(
                        map(str, creator_values)
                    ),
                    ",".join(
                        map(str, opponent_values)
                    ),
                    game_row["id"]
                ))

            await query.message.reply_text(
                "🤝 مساوی شد!\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "💰 مبلغ هر دو نفر برگشت داده شد."
            )

            return

        prize = round(
            amount * 2 * WINNER_SHARE,
            2
        )

        house = round(
            amount * 2 * HOUSE_SHARE,
            2
        )

        if not change_balance(
            winner_id,
            prize
        ):
            raise RuntimeError(
                "Winner balance update failed"
            )

        with closing(db()) as con:

            con.execute("""
                UPDATE games
                SET status='finished',
                    creator_values=?,
                    opponent_values=?,
                    winner_id=?
                WHERE id=?
            """, (
                ",".join(
                    map(str, creator_values)
                ),
                ",".join(
                    map(str, opponent_values)
                ),
                winner_id,
                game_row["id"]
            ))

        await query.message.reply_text(
            "🏆 بازی تمام شد!\n\n"
            f"👑 برنده: "
            f"{'بازیکن اول' if winner_id == creator_id else 'بازیکن دوم'}\n"
            f"🎯 امتیاز برنده: {winner_score}\n"
            f"🎯 امتیاز بازنده: {loser_score}\n\n"
            f"💰 دریافتی برنده: "
            f"{format_amount(prize)} TRX"
        )

    except Exception:

        change_balance(
            creator_id,
            amount
        )

        change_balance(
            user.id,
            amount
        )

        with closing(db()) as con:
            con.execute("""
                UPDATE games
                SET status='cancelled'
                WHERE id=?
            """, (
                game_row["id"],
            ))

        logger.exception(
            "FRIEND GAME ERROR"
        )

        await query.message.reply_text(
            "❌ خطا در بازی؛ مبلغ هر دو نفر برگشت داده شد."
        )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(query):

    user = query.from_user

    with closing(db()) as con:

        row = con.execute("""
            SELECT *
            FROM games
            WHERE chat_id=?
              AND message_id=?
              AND status='waiting'
            LIMIT 1
        """, (
            query.message.chat_id,
            query.message.message_id
        )).fetchone()

        if not row:

            await query.answer(
                "❌ بازی قابل لغو نیست.",
                show_alert=True
            )

            return

        if int(row["creator_id"]) != user.id:

            await query.answer(
                "❌ فقط سازنده می‌تواند لغو کند.",
                show_alert=True
            )

            return

        updated = con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
              AND status='waiting'
        """, (
            row["id"],
        )).rowcount

        if updated != 1:

            await query.answer(
                "❌ بازی قبلاً شروع شده.",
                show_alert=True
            )

            return

    change_balance(
        user.id,
        float(row["amount"])
    )

    try:
        await query.edit_message_text(
            "❌ بازی لغو شد.\n\n"
            f"💰 {format_amount(row['amount'])} TRX برگشت داده شد."
        )
    except Exception:
        pass


# ============================================================
# CALLBACK GAME
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "cancel_game":
        await cancel_game(query)
        return

    parts = data.split(":")

    if len(parts) != 4:
        return

    mode = parts[0]
    game = parts[1]

    try:
        rolls = int(parts[2])
        amount = float(parts[3])
    except Exception:
        return

    if mode == "bot":

        # اگر ربات مشغول باشد
        # در این نسخه بازی‌های ربات همزمان محدود می‌شوند
        if context.application.bot_data.get(
            "bot_game_busy",
            False
        ):

            await query.message.reply_text(
                "🤖 ربات در حال بازی است؛ "
                "موجودی شما برگشت داده شد ❌️"
            )

            return

        context.application.bot_data[
            "bot_game_busy"
        ] = True

        try:
            await start_bot_game(
                query,
                context,
                game,
                rolls,
                amount
            )
        finally:
            context.application.bot_data[
                "bot_game_busy"
            ] = False

        return

    if mode == "friend":

        await start_friend_game(
            query,
            context,
            game,
            rolls,
            amount
        )

        return


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.message
    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

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

    amount = parse_transfer_amount(
        message.text
    )

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر.\n"
            "مثال: انتقال 3"
        )

        return

    ensure_user(target)

    if is_virtual_user(target.id):

        await message.reply_text(
            "❌ انتقال به این کاربر مجاز نیست."
        )

        return

    if not transfer_balance(
        user.id,
        target.id,
        amount
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست یا انتقال انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {format_amount(amount)} TRX"
    )


def parse_transfer_amount(text):

    text = normalize_digits(
        text or ""
    )

    m = re.match(
        r"^(انتقال|transfer)\s+([0-9]+(?:[.,][0-9]+)?)$",
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    try:
        amount = float(
            m.group(2).replace(",", ".")
        )
    except Exception:
        return None

    if amount <= 0 or amount > MAX_BET:
        return None

    return amount


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

    if not withdrawal_enabled():

        await update.message.reply_text(
            "📤 درخواست برداشت در حال حاضر خاموش است."
        )

        return

    await update.message.reply_text(
        "📤 درخواست برداشت\n\n"
        "مثال:\n"
        "درخواست 10\n\n"
        "بعد از آن اطلاعات لازم را ارسال کن."
    )

    context.user_data["request_mode"] = "amount"


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی:\n"
        "۱ تاس ۰.۱\n"
        "۲ تاس ۰.۱\n"
        "۱ بولینگ ۰.۱\n"
        "۱ بسکتبال ۰.۱\n"
        "۱ دارت ۰.۱\n\n"
        "💰 موجودی\n"
        "💸 انتقال 3 ← با Reply\n"
        "👥 بازی با دوستان\n"
        "📤 درخواست برداشت\n\n"
        "⚠️ مقادیر TRX در این ربات اعتبار بازی هستند "
        "و به شبکه TRON متصل نیستند."
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

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    if data == "admin_users":

        with closing(db()) as con:

            rows = con.execute("""
                SELECT user_id,
                       first_name,
                       username,
                       balance,
                       blocked
                FROM users
                ORDER BY balance DESC
                LIMIT 30
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "👥 کاربری وجود ندارد."
            )

            return

        text = "👥 کاربران\n\n"

        for i, row in enumerate(rows, 1):

            name = (
                row["first_name"]
                or (
                    "@" + row["username"]
                    if row["username"]
                    else str(row["user_id"])
                )
            )

            status = (
                "🚫"
                if row["blocked"]
                else
                "✅"
            )

            text += (
                f"{i}. {status} {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {format_amount(row['balance'])} TRX\n\n"
            )

        await query.edit_message_text(
            text
        )

        return

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute("""
                SELECT COUNT(*)
                FROM users
            """).fetchone()[0]

            total = con.execute("""
                SELECT COALESCE(SUM(balance),0)
                FROM users
            """).fetchone()[0]

            pending = con.execute("""
                SELECT COUNT(*)
                FROM requests
                WHERE status='pending'
            """).fetchone()[0]

            games = con.execute("""
                SELECT COUNT(*)
                FROM games
            """).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: "
            f"{format_amount(total)} TRX\n"
            f"🎮 تعداد بازی‌ها: {games:,}\n"
            f"📤 درخواست‌های در انتظار: {pending:,}\n\n"
            f"📤 برداشت: "
            f"{'روشن 🟢' if withdrawal_enabled() else 'خاموش 🔴'}"
        )

        return

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT"
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT"
        )

        return

    # --------------------------------------------------------
    # WITHDRAW TOGGLE
    # --------------------------------------------------------

    if data == "admin_withdraw_toggle":

        new_value = (
            "0"
            if withdrawal_enabled()
            else
            "1"
        )

        set_setting(
            "withdraw_enabled",
            new_value
        )

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            f"📤 برداشت اکنون "
            f"{'روشن 🟢' if new_value == '1' else 'خاموش 🔴'} است.",
            reply_markup=admin_keyboard()
        )

        return

    # --------------------------------------------------------
    # REQUESTS
    # --------------------------------------------------------

    if data == "admin_requests":

        with closing(db()) as con:

            rows = con.execute("""
                SELECT *
                FROM requests
                WHERE status='pending'
                ORDER BY id DESC
                LIMIT 30
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "📋 درخواست در انتظار وجود ندارد.",
                reply_markup=admin_keyboard()
            )

            return

        text = "📋 درخواست‌های در انتظار\n\n"

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 {row['user_id']}\n"
                f"💰 {format_amount(row['amount'])} TRX\n"
                f"📝 {row['wallet']}\n\n"
            )

        await query.edit_message_text(
            text,
            reply_markup=admin_keyboard()
        )

        return


# ============================================================
# ADMIN ADD
# ============================================================

async def add_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/addbalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

        amount = float(
            normalize_digits(
                context.args[1]
            ).replace(",", ".")
        )

    except Exception:

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

    if not change_balance(
        target_id,
        amount
    ):

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await update.message.reply_text(
        "✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {format_amount(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{format_amount(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN REMOVE
# ============================================================

async def remove_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/removebalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

        amount = float(
            normalize_digits(
                context.args[1]
            ).replace(",", ".")
        )

    except Exception:

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

    if not change_balance(
        target_id,
        -amount
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    await update.message.reply_text(
        "✅ موجودی کاهش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➖ {format_amount(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{format_amount(get_balance(target_id))} TRX"
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
        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )
    except Exception:
        return

    with closing(db()) as con:

        con.execute("""
            UPDATE users
            SET blocked=1
            WHERE user_id=?
        """, (
            target_id,
        ))

    await update.message.reply_text(
        f"🚫 کاربر {target_id} مسدود شد."
    )


# ============================================================
# UNBLOCK
# ============================================================

async def unblock(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 1:
        return

    try:
        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )
    except Exception:
        return

    with closing(db()) as con:

        con.execute("""
            UPDATE users
            SET blocked=0
            WHERE user_id=?
        """, (
            target_id,
        ))

    await update.message.reply_text(
        f"✅ کاربر {target_id} رفع مسدودی شد."
    )


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

    # ========================================================
    # REQUEST MODE
    # ========================================================

    request_mode = context.user_data.get(
        "request_mode"
    )

    if request_mode == "amount":

        m = re.match(
            r"^(درخواست|request)\s+([0-9]+(?:[.,][0-9]+)?)$",
            normalized,
            re.IGNORECASE
        )

        if m:

            amount = float(
                m.group(2).replace(",", ".")
            )

            if amount <= 0:
                return

            if amount > get_balance(user.id):

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            context.user_data[
                "request_amount"
            ] = amount

            context.user_data[
                "request_mode"
            ] = "wallet"

            await message.reply_text(
                "📝 مقدار ثبت شد.\n\n"
                "حالا اطلاعات درخواست را بفرست."
            )

            return

    if request_mode == "wallet":

        amount = context.user_data.get(
            "request_amount"
        )

        if amount:

            with closing(db()) as con:

                con.execute("""
                    INSERT INTO requests
                    (user_id, amount, wallet)
                    VALUES (?, ?, ?)
                """, (
                    user.id,
                    amount,
                    text
                ))

            context.user_data.clear()

            await message.reply_text(
                "✅ درخواست ثبت شد."
            )

            return

    # ========================================================
    # GAME
    # ========================================================

    parsed = parse_game(
        normalized
    )

    if parsed:

        game, rolls, amount = parsed

        # فقط در گروه بازی ساخته شود
        if update.effective_chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "🎮 بازی را داخل گپ ایجاد کن."
            )

            return

        await create_game_message(
            update,
            context,
            game,
            rolls,
            amount
        )

        return

    # ========================================================
    # MENU
    # ========================================================

    if text == "💰 موجودی":

        await balance(
            update,
            context
        )

        return

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )

        return

    if text == "👥 بازی با دوستان":

        await friends(
            update,
            context
        )

        return

    if text == "💸 انتقال":

        await transfer(
            update,
            context
        )

        return

    if text == "📤 درخواست":

        await request_menu(
            update,
            context
        )

        return

    if text == "📖 راهنما":

        await help_command(
            update,
            context
        )

        return

    # ========================================================
    # TRANSFER
    # ========================================================

    if re.match(
        r"^(انتقال|transfer)\s+[0-9]+(?:[.,][0-9]+)?$",
        normalized,
        re.IGNORECASE
    ):

        await transfer(
            update,
            context
        )

        return

    # ========================================================
    # REQUEST
    # ========================================================

    if re.match(
        r"^(درخواست|request)\s+[0-9]+(?:[.,][0-9]+)?$",
        normalized,
        re.IGNORECASE
    ):

        await request_menu(
            update,
            context
        )

        # دوباره همان پیام را پردازش کن
        m = re.match(
            r"^(درخواست|request)\s+([0-9]+(?:[.,][0-9]+)?)$",
            normalized,
            re.IGNORECASE
        )

        if m:

            amount = float(
                m.group(2).replace(",", ".")
            )

            if amount > get_balance(user.id):

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            context.user_data[
                "request_amount"
            ] = amount

            context.user_data[
                "request_mode"
            ] = "wallet"

            await message.reply_text(
                "📝 حالا اطلاعات درخواست را بفرست."
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

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^(friend:|bot:|cancel_game)"
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

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
        "BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
