# ============================================================
# BOT.PY - Telegram Games Bot
# Python 3.10+
# python-telegram-bot 20+
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
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

ADMIN_IDS = {8552447077}

DB_FILE = "bot.db"

# موجودی اولیه = صفر
START_BALANCE = Decimal("0")

# کارمزد مالک
OWNER_FEE = Decimal("0.08")

# سهم برنده
WINNER_SHARE = Decimal("0.92")

# برداشت به صورت پیش فرض روشن
WITHDRAW_ENABLED_DEFAULT = 1

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
        timeout=30
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
            balance INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
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
            count INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            winner_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        row = con.execute(
            "SELECT value FROM settings WHERE key='withdraw_enabled'"
        ).fetchone()

        if not row:
            con.execute(
                "INSERT INTO settings(key,value) VALUES('withdraw_enabled',?)",
                (str(WITHDRAW_ENABLED_DEFAULT),)
            )

        con.commit()


# ============================================================
# MONEY
# ============================================================

SCALE = 1_000_000


def money_to_int(value):
    value = Decimal(str(value))
    return int(value * SCALE)


def int_to_money(value):
    return Decimal(int(value)) / SCALE


def fmt_money(value):

    value = Decimal(str(value))

    if value == value.to_integral():
        return f"{int(value):,}"

    text = f"{value:.6f}".rstrip("0").rstrip(".")

    return text


# ============================================================
# USERS
# ============================================================

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
                0
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
        return Decimal("0")

    return int_to_money(row["balance"])


def change_balance(user_id, amount):

    delta = money_to_int(amount)

    with closing(db()) as con:

        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        old_balance = int(row["balance"])
        new_balance = old_balance + delta

        if new_balance < 0:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, user_id)
        )

        con.commit()

        return True


def set_balance(user_id, amount):

    amount_int = money_to_int(amount)

    if amount_int < 0:
        return False

    with closing(db()) as con:

        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (amount_int, user_id)
        )

        con.commit()

        return True


def is_blocked(user_id):

    row = get_user(user_id)

    return bool(row and row["blocked"])


def is_admin(user_id):

    return user_id in ADMIN_IDS


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=None):

    with closing(db()) as con:

        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

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

        con.commit()


def withdraw_enabled():

    return get_setting(
        "withdraw_enabled",
        "1"
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


def parse_amount(value):

    value = normalize_digits(value)

    value = value.replace("٫", ".")
    value = value.replace(",", ".")

    try:

        amount = Decimal(value)

    except (InvalidOperation, ValueError):

        return None

    if amount <= 0:
        return None

    return amount


def name_of(user):

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


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

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    match = re.match(
        r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:[.,٫][0-9]+)?)$",
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    try:

        count = int(match.group(1))

    except ValueError:

        return None

    if count <= 0:
        return None

    game_name = match.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_amount(
        match.group(3)
    )

    if amount is None:
        return None

    return {
        "game": game,
        "count": count,
        "amount": amount
    }


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["👥 بازی با دوستان", "🤖 بازی با ربات"],
            ["💸 انتقال", "📤 برداشت"],
            ["📖 راهنما"]
        ],
        resize_keyboard=True
    )


def game_buttons():

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


def game_start_buttons(game, count, amount):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game}:{count}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"botgame:{game}:{count}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="game_cancel"
            )
        ]
    ])


def admin_keyboard():

    status = "🟢 برداشت روشن" if withdraw_enabled() else "🔴 برداشت خاموش"

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
        ],
        [
            InlineKeyboardButton(
                status,
                callback_data="admin_withdraw"
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
            "⛔ دسترسی شما مسدود است."
        )

        return

    await update.message.reply_text(
        "👋 سلام!\n\n"
        "به ربات خوش آمدی.",
        reply_markup=main_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    amount = get_balance(user.id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"💎 {fmt_money(amount)} TRX\n\n"
        f"ℹ️ موجودی داخل سیستم ربات مدیریت می‌شود."
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    await update.message.reply_text(
        "🎮 نوع بازی را انتخاب کن:",
        reply_markup=game_buttons()
    )


# ============================================================
# SEND DICE
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
# CREATE GAME MESSAGE
# ============================================================

async def create_game_message(
    update,
    context,
    game,
    count,
    amount
):

    user = update.effective_user

    total = amount * count

    balance = get_balance(user.id)

    if balance < total:

        await update.effective_message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {fmt_money(balance)} TRX\n"
            f"💸 نیاز: {fmt_money(total)} TRX"
        )

        return

    # مبلغ را از همان لحظه رزرو می‌کنیم
    if not change_balance(
        user.id,
        -total
    ):

        await update.effective_message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    try:

        message = await update.effective_message.reply_text(
            f"🎮 بازی آماده شد!\n\n"
            f"{GAME_EMOJI[game]} تعداد: {count}\n"
            f"💰 مبلغ هر بازی: {fmt_money(amount)} TRX\n"
            f"💸 مبلغ کل: {fmt_money(total)} TRX\n\n"
            f"یک گزینه را انتخاب کن:",
            reply_markup=game_start_buttons(
                game,
                count,
                amount
            )
        )

        with closing(db()) as con:

            con.execute("""
            INSERT INTO games
            (chat_id,message_id,creator_id,game_type,count,amount)
            VALUES(?,?,?,?,?,?)
            """, (
                message.chat_id,
                message.message_id,
                user.id,
                game,
                count,
                money_to_int(amount)
            ))

            con.commit()

    except Exception:

        change_balance(
            user.id,
            total
        )

        logger.exception(
            "CREATE GAME ERROR"
        )

        await update.effective_message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ برگشت داده شد."
        )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(
    query,
    context,
    game,
    count,
    amount
):

    user = query.from_user

    total = amount * count

    balance = get_balance(user.id)

    if balance < total:

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(
        user.id,
        -total
    ):

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    try:

        await query.message.edit_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "🤖 بازی با ربات شروع شد.\n\n"
            "اول خودت بازی کن."
        )

        user_results = []

        for _ in range(count):

            roll = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            user_results.append(
                roll.dice.value
            )

            await asyncio.sleep(0.7)

        await query.message.reply_text(
            "🤖 حالا ربات بازی می‌کند..."
        )

        bot_results = []

        for _ in range(count):

            roll = await send_roll(
                context.bot,
                query.message.chat_id,
                game
            )

            bot_results.append(
                roll.dice.value
            )

            await asyncio.sleep(0.7)

        user_score = sum(user_results)
        bot_score = sum(bot_results)

        if user_score > bot_score:

            payout = total * WINNER_SHARE

            change_balance(
                user.id,
                payout
            )

            owner_fee = total * OWNER_FEE

            change_balance(
                next(iter(ADMIN_IDS)),
                owner_fee
            )

            result = (
                "🏆 شما برنده شدید!\n\n"
                f"🎯 امتیاز شما: {user_score}\n"
                f"🤖 امتیاز ربات: {bot_score}\n"
                f"💰 جایزه: {fmt_money(payout)} TRX"
            )

        elif bot_score > user_score:

            owner_fee = total * OWNER_FEE

            change_balance(
                next(iter(ADMIN_IDS)),
                owner_fee
            )

            result = (
                "❌ شما باختید.\n\n"
                f"🎯 امتیاز شما: {user_score}\n"
                f"🤖 امتیاز ربات: {bot_score}"
            )

        else:

            change_balance(
                user.id,
                total
            )

            result = (
                "🤝 مساوی شد.\n\n"
                f"🎯 امتیاز شما: {user_score}\n"
                f"🤖 امتیاز ربات: {bot_score}\n"
                f"💰 مبلغ شما برگشت داده شد."
            )

        await query.message.reply_text(
            result
        )

    except Exception:

        change_balance(
            user.id,
            total
        )

        logger.exception(
            "BOT GAME ERROR"
        )

        await query.message.reply_text(
            "❌ بازی لغو شد؛ مبلغ برگشت داده شد."
        )


# ============================================================
# FRIEND GAME
# ============================================================

async def start_friend_game(
    query,
    context,
    game,
    count,
    amount
):

    user = query.from_user

    message = query.message

    # پیام دکمه‌ها حذف می‌شود
    try:
        await message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    total = amount * count

    # بازی رزرو شده قبلاً از موجودی کسر شده
    with closing(db()) as con:

        row = con.execute("""
        SELECT *
        FROM games
        WHERE message_id=?
        AND creator_id=?
        AND status='pending'
        ORDER BY id DESC
        LIMIT 1
        """, (
            message.message_id,
            user.id
        )).fetchone()

        if not row:

            await message.reply_text(
                "❌ بازی پیدا نشد."
            )

            return

        game_id = row["id"]

    await message.reply_text(
        "👥 بازی با دوستان باز شد.\n\n"
        "برای ورود، یک نفر روی همین پیام Reply کند و همان شرط را بفرستد."
    )


# ============================================================
# FRIEND JOIN
# ============================================================

async def join_friend_game(
    update,
    context,
    parsed
):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:
        return False

    original = message.reply_to_message

    original_text = original.text or ""

    original_game = parse_game(
        original_text
    )

    if not original_game:
        return False

    if (
        original_game["game"] != parsed["game"]
        or original_game["count"] != parsed["count"]
        or original_game["amount"] != parsed["amount"]
    ):
        return False

    creator = original.from_user

    if not creator:
        return False

    if creator.id == user.id:
        return False

    ensure_user(creator)
    ensure_user(user)

    game = parsed["game"]
    count = parsed["count"]
    amount = parsed["amount"]

    total = amount * count

    # پیدا کردن بازی pending
    with closing(db()) as con:

        row = con.execute("""
        SELECT *
        FROM games
        WHERE message_id=?
        AND creator_id=?
        AND status='pending'
        ORDER BY id DESC
        LIMIT 1
        """, (
            original.message_id,
            creator.id
        )).fetchone()

        if not row:
            return False

        game_id = row["id"]

        # قفل منطقی بازی
        updated = con.execute("""
        UPDATE games
        SET opponent_id=?, status='playing'
        WHERE id=? AND status='pending'
        """, (
            user.id,
            game_id
        )).rowcount

        con.commit()

    if updated != 1:

        await message.reply_text(
            "❌ این بازی قبلاً توسط شخص دیگری گرفته شده است."
        )

        return True

    # کسر شرط نفر دوم
    if get_balance(user.id) < total:

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET opponent_id=NULL, status='pending'
            WHERE id=?
            """, (
                game_id,
            ))

            con.commit()

        await message.reply_text(
            "❌ موجودی شما کافی نیست."
        )

        return True

    if not change_balance(
        user.id,
        -total
    ):

        return True

    try:

        await message.reply_text(
            "🎮 بازی شروع شد!\n\n"
            f"👤 سازنده: {name_of(creator)}\n"
            f"👤 بازیکن دوم: {name_of(user)}\n\n"
            "اول سازنده بازی می‌کند."
        )

        creator_results = []

        for _ in range(count):

            roll = await send_roll(
                context.bot,
                message.chat_id,
                game
            )

            creator_results.append(
                roll.dice.value
            )

            await asyncio.sleep(0.7)

        await message.reply_text(
            "👤 حالا بازیکن دوم بازی می‌کند..."
        )

        opponent_results = []

        for _ in range(count):

            roll = await send_roll(
                context.bot,
                message.chat_id,
                game
            )

            opponent_results.append(
                roll.dice.value
            )

            await asyncio.sleep(0.7)

        score1 = sum(creator_results)
        score2 = sum(opponent_results)

        pool = total * 2
        owner_fee = pool * OWNER_FEE
        winner_payout = pool * WINNER_SHARE

        admin_id = next(iter(ADMIN_IDS))

        if score1 > score2:

            change_balance(
                creator.id,
                winner_payout
            )

            change_balance(
                admin_id,
                owner_fee
            )

            winner_name = name_of(creator)

        elif score2 > score1:

            change_balance(
                user.id,
                winner_payout
            )

            change_balance(
                admin_id,
                owner_fee
            )

            winner_name = name_of(user)

        else:

            change_balance(
                creator.id,
                total
            )

            change_balance(
                user.id,
                total
            )

            winner_name = None

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET status='finished',
                winner_id=?
            WHERE id=?
            """, (
                creator.id if score1 > score2
                else user.id if score2 > score1
                else None,
                game_id
            ))

            con.commit()

        if winner_name:

            await message.reply_text(
                "🏆 بازی تمام شد!\n\n"
                f"👑 برنده: {winner_name}\n"
                f"🎯 نتیجه نفر اول: {score1}\n"
                f"🎯 نتیجه نفر دوم: {score2}\n"
                f"💰 جایزه: {fmt_money(winner_payout)} TRX"
            )

        else:

            await message.reply_text(
                "🤝 بازی مساوی شد!\n\n"
                f"🎯 نتیجه نفر اول: {score1}\n"
                f"🎯 نتیجه نفر دوم: {score2}\n\n"
                "💰 شرط هر دو نفر برگشت داده شد."
            )

        return True

    except Exception:

        change_balance(
            creator.id,
            total
        )

        change_balance(
            user.id,
            total
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            """, (
                game_id,
            ))

            con.commit()

        logger.exception(
            "FRIEND GAME ERROR"
        )

        await message.reply_text(
            "❌ خطا در بازی؛ مبلغ هر دو نفر برگشت داده شد."
        )

        return True


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال روی پیام کاربر Reply کن.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )

        return

    target = message.reply_to_message.from_user

    if not target:
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

    parts = message.text.split()

    if len(parts) < 2:

        await message.reply_text(
            "❌ مثال: انتقال 0.5"
        )

        return

    amount = parse_amount(
        parts[-1]
    )

    if amount is None:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    ensure_user(target)

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(
        user.id,
        -amount
    ):
        return

    if not change_balance(
        target.id,
        amount
    ):

        change_balance(
            user.id,
            amount
        )

        await message.reply_text(
            "❌ انتقال انجام نشد."
        )

        return

    with closing(db()) as con:

        con.execute("""
        INSERT INTO transfers
        (sender_id,receiver_id,amount)
        VALUES(?,?,?)
        """, (
            user.id,
            target.id,
            money_to_int(amount)
        ))

        con.commit()

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {fmt_money(amount)} TRX"
    )


# ============================================================
# WITHDRAW
# ============================================================

async def withdraw_menu(update, context):

    if not withdraw_enabled():

        await update.message.reply_text(
            "🔴 برداشت در حال حاضر خاموش است."
        )

        return

    await update.message.reply_text(
        "📤 درخواست برداشت\n\n"
        "مثال:\n"
        "برداشت 10\n\n"
        "بعد از آن آدرس/اطلاعات برداشت را بفرست."
    )

    context.user_data["withdraw_mode"] = "amount"


async def create_withdraw_request(
    update,
    context,
    amount,
    wallet
):

    user = update.effective_user

    if not withdraw_enabled():

        await update.message.reply_text(
            "🔴 برداشت خاموش است."
        )

        context.user_data.clear()

        return

    if get_balance(user.id) < amount:

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        context.user_data.clear()

        return

    # رزرو/کسر موجودی تا دوباره خرج نشود
    if not change_balance(
        user.id,
        -amount
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        context.user_data.clear()

        return

    with closing(db()) as con:

        con.execute("""
        INSERT INTO requests
        (user_id,amount,wallet,status)
        VALUES(?,?,?,'pending')
        """, (
            user.id,
            money_to_int(amount),
            wallet
        ))

        con.commit()

    context.user_data.clear()

    await update.message.reply_text(
        "✅ درخواست برداشت ثبت شد.\n\n"
        f"💰 مبلغ: {fmt_money(amount)} TRX\n"
        "📋 وضعیت: در انتظار بررسی مدیریت"
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.1\n"
        "10 بولینگ 0.5\n"
        "3 بسکتبال 1\n"
        "5 دارت 2.5\n\n"
        "عدد اول = تعداد بازی\n"
        "عدد دوم = مبلغ هر بازی\n\n"
        "👥 بازی دوستان:\n"
        "بازی را بساز و نفر دوم با Reply وارد شود.\n\n"
        "🤖 بازی با ربات:\n"
        "ابتدا خود کاربر بازی می‌کند، سپس ربات.\n\n"
        "💸 انتقال:\n"
        "روی پیام کاربر Reply کن و بنویس:\n"
        "انتقال 0.5\n\n"
        "📤 برداشت:\n"
        "برداشت 10\n\n"
        "موجودی این ربات داخل سیستم مدیریت می‌شود "
        "و تراکنش بلاکچینی واقعی انجام نمی‌دهد."
    )


# ============================================================
# ADMIN
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

    # USERS
    if data == "admin_users":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT user_id,first_name,username,balance,blocked
            FROM users
            ORDER BY balance DESC
            LIMIT 50
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
                or row["username"]
                or str(row["user_id"])
            )

            text += (
                f"{i}. {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {fmt_money(int_to_money(row['balance']))} TRX\n"
                f"{'🚫' if row['blocked'] else '✅'}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )

        return

    # STATS
    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = con.execute(
                "SELECT COALESCE(SUM(balance),0) FROM users"
            ).fetchone()[0]

            pending = con.execute(
                "SELECT COUNT(*) FROM requests WHERE status='pending'"
            ).fetchone()[0]

            games = con.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: "
            f"{fmt_money(int_to_money(total))} TRX\n"
            f"📤 درخواست‌های در انتظار: {pending:,}\n"
            f"🎮 تعداد بازی‌ها: {games:,}"
        )

        return

    # ADD
    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "در پیوی بفرست:\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 10.5"
        )

        return

    # REMOVE
    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "در پیوی بفرست:\n"
            "/removebalance USER_ID AMOUNT"
        )

        return

    # REQUESTS
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

        text = "📋 درخواست‌ها\n\n"

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 {row['user_id']}\n"
                f"💰 {fmt_money(int_to_money(row['amount']))} TRX\n"
                f"📝 {row['wallet']}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )

        return

    # WITHDRAW TOGGLE
    if data == "admin_withdraw":

        new_value = not withdraw_enabled()

        set_setting(
            "withdraw_enabled",
            "1" if new_value else "0"
        )

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            f"📤 برداشت: "
            f"{'🟢 روشن' if new_value else '🔴 خاموش'}",
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
            normalize_digits(context.args[0])
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID نامعتبر."
        )

        return

    amount = parse_amount(
        context.args[1]
    )

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ نامعتبر."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    change_balance(
        target_id,
        amount
    )

    await update.message.reply_text(
        "✅ افزایش موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➕ {fmt_money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{fmt_money(get_balance(target_id))} TRX"
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
            normalize_digits(context.args[0])
        )

    except ValueError:

        await update.message.reply_text(
            "❌ USER_ID نامعتبر."
        )

        return

    amount = parse_amount(
        context.args[1]
    )

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ نامعتبر."
        )

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
        "✅ کاهش موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➖ {fmt_money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{fmt_money(get_balance(target_id))} TRX"
    )


# ============================================================
# BLOCK / UNBLOCK
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

    game = query.data.replace(
        "game_",
        ""
    )

    names = {
        "dice": "تاس",
        "bowling": "بولینگ",
        "basketball": "بسکتبال",
        "darts": "دارت"
    }

    if game not in names:
        return

    await query.message.reply_text(
        f"{GAME_EMOJI[game]} {names[game]}\n\n"
        f"مثال:\n"
        f"2 {names[game]} 0.1"
    )


# ============================================================
# GAME ACTION CALLBACK
# ============================================================

async def game_action_callback(update, context):

    query = update.callback_query

    await query.answer()

    data = query.data

    # CANCEL
    if data == "game_cancel":

        try:
            await query.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        # اگر بازی واقعی در DB بود، مبلغ را برگردان
        with closing(db()) as con:

            row = con.execute("""
            SELECT *
            FROM games
            WHERE message_id=?
            AND creator_id=?
            AND status='pending'
            ORDER BY id DESC
            LIMIT 1
            """, (
                query.message.message_id,
                query.from_user.id
            )).fetchone()

            if row:

                amount = int_to_money(
                    row["amount"]
                )

                total = amount * row["count"]

                change_balance(
                    row["creator_id"],
                    total
                )

                con.execute("""
                UPDATE games
                SET status='cancelled'
                WHERE id=?
                """, (
                    row["id"],
                ))

                con.commit()

                await query.message.reply_text(
                    "❌ بازی لغو شد.\n"
                    "💰 مبلغ برگشت داده شد."
                )

        return

    # FRIEND
    if data.startswith("friend:"):

        try:

            _, game, count_s, amount_s = data.split(
                ":",
                3
            )

            count = int(count_s)
            amount = Decimal(amount_s)

        except Exception:

            return

        await start_friend_game(
            query,
            context,
            game,
            count,
            amount
        )

        return

    # BOT
    if data.startswith("botgame:"):

        try:

            _, game, count_s, amount_s = data.split(
                ":",
                3
            )

            count = int(count_s)
            amount = Decimal(amount_s)

        except Exception:

            return

        await bot_game(
            query,
            context,
            game,
            count,
            amount
        )

        return


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

    normalized = normalize_digits(
        text
    )

    # --------------------------------------------------------
    # WITHDRAW FLOW
    # --------------------------------------------------------

    if context.user_data.get(
        "withdraw_mode"
    ) == "amount":

        amount = parse_amount(
            normalized
        )

        if amount is None:

            await message.reply_text(
                "❌ مبلغ نامعتبر."
            )

            return

        context.user_data[
            "withdraw_amount"
        ] = str(amount)

        context.user_data[
            "withdraw_mode"
        ] = "wallet"

        await message.reply_text(
            "📝 مبلغ ثبت شد.\n\n"
            "حالا آدرس/اطلاعات برداشت را بفرست."
        )

        return

    if context.user_data.get(
        "withdraw_mode"
    ) == "wallet":

        amount = Decimal(
            context.user_data[
                "withdraw_amount"
            ]
        )

        await create_withdraw_request(
            update,
            context,
            amount,
            text
        )

        return

    # --------------------------------------------------------
    # MENU
    # --------------------------------------------------------

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

        await message.reply_text(
            "👥 برای ساخت بازی، در گپ بنویس:\n\n"
            "2 تاس 0.1\n"
            "2 بولینگ 0.1\n"
            "2 بسکتبال 0.1\n"
            "2 دارت 0.1"
        )

        return

    if text == "🤖 بازی با ربات":

        await message.reply_text(
            "🤖 برای بازی با ربات، ابتدا بازی را انتخاب کن:\n\n"
            "مثال:\n"
            "2 تاس 0.1"
        )

        return

    if text == "💸 انتقال":

        await message.reply_text(
            "💸 روی پیام کاربر Reply کن و بنویس:\n\n"
            "انتقال 0.5"
        )

        return

    if text == "📤 برداشت":

        await withdraw_menu(
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

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    transfer_match = re.match(
        r"^(انتقال|transfer)\s+([0-9]+(?:[.,٫][0-9]+)?)$",
        normalized,
        re.IGNORECASE
    )

    if transfer_match:

        await transfer(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # WITHDRAW COMMAND
    # --------------------------------------------------------

    withdraw_match = re.match(
        r"^(برداشت|withdraw)\s+([0-9]+(?:[.,٫][0-9]+)?)$",
        normalized,
        re.IGNORECASE
    )

    if withdraw_match:

        if not withdraw_enabled():

            await message.reply_text(
                "🔴 برداشت خاموش است."
            )

            return

        amount = parse_amount(
            withdraw_match.group(2)
        )

        if amount is None:
            return

        context.user_data[
            "withdraw_amount"
        ] = str(amount)

        context.user_data[
            "withdraw_mode"
        ] = "wallet"

        await message.reply_text(
            "📝 مبلغ ثبت شد.\n\n"
            "حالا آدرس/اطلاعات برداشت را بفرست."
        )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    parsed = parse_game(
        normalized
    )

    if parsed:

        # اگر Reply به بازی قبلی است
        if message.reply_to_message:

            joined = await join_friend_game(
                update,
                context,
                parsed
            )

            if joined:
                return

        await create_game_message(
            update,
            context,
            parsed["game"],
            parsed["count"],
            parsed["amount"]
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

    application.add_handler(
        CallbackQueryHandler(
            game_action_callback,
            pattern=r"^(friend:|botgame:|game_cancel$)"
        )
    )

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
