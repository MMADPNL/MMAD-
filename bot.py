# ============================================================
# BOT.PY
# Telegram Group Games Bot
# Python 3.10+
# python-telegram-bot 20+
#
# TRX = موجودی داخلی دیتابیس؛ انتقال واقعی شبکه TRON ندارد.
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_DOWN

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
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {8552447077}

DB_FILE = "bot.db"

START_BALANCE = Decimal("10")
MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000")
MIN_WITHDRAW = Decimal("3")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance TEXT DEFAULT '10',
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
            amount TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount TEXT,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            creator_id INTEGER,
            opponent_id INTEGER,
            game_type TEXT,
            amount TEXT,
            status TEXT DEFAULT 'pending',
            winner_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        con.execute("""
        INSERT OR IGNORE INTO settings(key,value)
        VALUES ('withdraw_enabled','1')
        """)

        con.commit()


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
                str(START_BALANCE)
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

    try:
        return Decimal(str(row["balance"]))
    except Exception:
        return Decimal("0")


def fmt(amount):
    amount = Decimal(str(amount)).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )

    return f"{amount:,.2f}"


def change_balance(user_id, amount):
    amount = Decimal(str(amount))

    with closing(db()) as con:
        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        try:
            old = Decimal(str(row["balance"]))
        except Exception:
            return False

        new = old + amount

        if new < 0:
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (str(new), user_id)
        )

        con.commit()
        return True


def set_balance(user_id, amount):
    amount = Decimal(str(amount))

    if amount < 0:
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
            (str(amount), user_id)
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
# SETTINGS
# ============================================================

def withdraw_enabled():
    with closing(db()) as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key='withdraw_enabled'"
        ).fetchone()

    if not row:
        return True

    return row["value"] == "1"


def set_withdraw_enabled(enabled):
    with closing(db()) as con:
        con.execute("""
        INSERT OR REPLACE INTO settings(key,value)
        VALUES ('withdraw_enabled',?)
        """, ("1" if enabled else "0",))

        con.commit()


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


def parse_money(text):
    text = normalize_digits(text or "").strip()

    text = text.replace(",", "")
    text = text.replace("٬", "")
    text = text.replace("٫", ".")
    text = text.replace("/", ".")

    m = re.search(r"(\d+(?:\.\d+)?)", text)

    if not m:
        return None

    try:
        amount = Decimal(m.group(1))
    except InvalidOperation:
        return None

    amount = amount.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )

    if amount <= 0:
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
            ["📤 برداشت", "📖 راهنما"]
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


def game_create_buttons(game, amount):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"botgame:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="cancel_game"
            )
        ]
    ])


# ============================================================
# GAME DATA
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

GAME_EMOJI = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯"
}

GAME_TITLE = {
    "dice": "تاس",
    "bowling": "بولینگ",
    "basketball": "بسکتبال",
    "darts": "دارت"
}


def parse_game(text):
    text = normalize_digits(text or "").strip()

    m = re.match(
        r"^1\s+([^\s]+)\s+([0-9]+(?:[.,][0-9]+)?)$",
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    game_name = m.group(1).lower()
    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_money(m.group(2))

    if amount is None:
        return None

    return game, amount


# ============================================================
# TELEGRAM ROLL
# ============================================================

async def send_roll(bot, chat_id, game):
    return await bot.send_dice(
        chat_id=chat_id,
        emoji=GAME_EMOJI[game]
    )


# ============================================================
# PAYOUT
# ============================================================

# از شرط 0.50:
# برنده 0.92 می‌گیرد
# 0.08 کارمزد سیستم است.
#
# نسبت پرداخت = 1.84 برابر شرط

PAYOUT_RATE = Decimal("1.84")


def payout(amount):
    return (
        amount * PAYOUT_RATE
    ).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


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
            "👋 برای استفاده از منوی کاربری، ربات را در خصوصی باز کن."
        )
        return

    await update.message.reply_text(
        "👋 سلام!\n\n"
        "یکی از گزینه‌های زیر را انتخاب کن.",
        reply_markup=user_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"💎 {fmt(get_balance(user.id))} TRX"
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
        "🎮 بازی را انتخاب کن:",
        reply_markup=game_buttons()
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, game, amount):

    user = update.effective_user
    message = update.message

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    if is_virtual_user(user.id):
        return

    if amount < MIN_GAME:
        await message.reply_text(
            "❌ حداقل مبلغ بازی 0.01 TRX است."
        )
        return

    if get_balance(user.id) < amount:
        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {fmt(get_balance(user.id))} TRX"
        )
        return

    # مبلغ همان لحظه از سازنده رزرو می‌شود.
    if not change_balance(user.id, -amount):
        await message.reply_text(
            "❌ خطا در رزرو موجودی."
        )
        return

    try:
        sent = await message.reply_text(
            f"🎮 بازی {GAME_TITLE[game]}\n\n"
            f"💰 شرط: {fmt(amount)} TRX\n"
            f"👤 سازنده: {name_of(user)}\n\n"
            "یک گزینه را انتخاب کنید:",
            reply_markup=game_create_buttons(game, amount)
        )

        with closing(db()) as con:
            con.execute("""
            INSERT INTO games
            (chat_id, message_id, creator_id, game_type, amount)
            VALUES (?, ?, ?, ?, ?)
            """, (
                message.chat_id,
                sent.message_id,
                user.id,
                game,
                str(amount)
            ))
            con.commit()

    except Exception:
        change_balance(user.id, amount)
        logger.exception("CREATE GAME ERROR")

        await message.reply_text(
            "❌ خطا؛ مبلغ بازی برگشت داده شد."
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

    game = query.data.replace("game_", "")

    if game not in GAME_NAMES.values():
        return

    await query.message.reply_text(
        f"{GAME_EMOJI[game]} {GAME_TITLE[game]}\n\n"
        f"مثال:\n"
        f"1 {GAME_TITLE[game]} 0.5"
    )


# ============================================================
# FRIEND GAME
# ============================================================

async def friend_join(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    if is_blocked(user.id) or is_virtual_user(user.id):
        return

    try:
        _, game, amount_text = query.data.split(":", 2)
        amount = Decimal(amount_text)
    except Exception:
        return

    with closing(db()) as con:

        game_row = con.execute("""
        SELECT *
        FROM games
        WHERE message_id=?
        AND chat_id=?
        AND status='pending'
        ORDER BY id DESC
        LIMIT 1
        """, (
            query.message.message_id,
            query.message.chat_id
        )).fetchone()

    if not game_row:
        await query.message.reply_text(
            "❌ این بازی دیگر فعال نیست."
        )
        return

    creator_id = game_row["creator_id"]

    if creator_id == user.id:
        await query.answer(
            "❌ خودت نمی‌توانی وارد بازی خودت شوی.",
            show_alert=True
        )
        return

    creator = get_user(creator_id)

    if not creator:
        return

    if is_virtual_user(creator_id):
        return

    if get_balance(user.id) < amount:
        await query.message.reply_text(
            "❌ موجودی شما برای ورود به بازی کافی نیست."
        )
        return

    if not change_balance(user.id, -amount):
        return

    # بازی را قفل می‌کنیم تا نفر دوم همزمان نتواند وارد شود.
    with closing(db()) as con:
        result = con.execute("""
        UPDATE games
        SET opponent_id=?, status='playing'
        WHERE id=? AND status='pending'
        """, (
            user.id,
            game_row["id"]
        ))

        con.commit()

        if result.rowcount != 1:
            change_balance(user.id, amount)

            await query.message.reply_text(
                "❌ این بازی توسط کاربر دیگری گرفته شد.\n"
                "💰 مبلغ شما برگشت داده شد."
            )
            return

    try:

        await query.message.reply_text(
            f"👥 بازی شروع شد!\n\n"
            f"👤 بازیکن اول: {name_of_from_row(creator)}\n"
            f"👤 بازیکن دوم: {name_of(user)}\n"
            f"💰 شرط هر نفر: {fmt(amount)} TRX"
        )

        # بازیکن اول
        roll1 = await send_roll(
            context.bot,
            query.message.chat_id,
            game
        )

        await asyncio.sleep(1)

        # بازیکن دوم
        roll2 = await send_roll(
            context.bot,
            query.message.chat_id,
            game
        )

        value1 = roll1.dice.value
        value2 = roll2.dice.value

        total = amount * 2

        if value1 == value2:

            change_balance(creator_id, amount)
            change_balance(user.id, amount)

            with closing(db()) as con:
                con.execute("""
                UPDATE games
                SET status='finished'
                WHERE id=?
                """, (game_row["id"],))
                con.commit()

            await query.message.reply_text(
                "🤝 مساوی شد!\n\n"
                f"💰 {fmt(amount)} TRX به هر دو نفر برگشت داده شد."
            )

            return

        if value1 > value2:
            winner_id = creator_id
            winner_name = name_of_from_row(creator)
        else:
            winner_id = user.id
            winner_name = name_of(user)

        reward = payout(amount)

        change_balance(winner_id, reward)

        with closing(db()) as con:
            con.execute("""
            UPDATE games
            SET status='finished', winner_id=?
            WHERE id=?
            """, (
                winner_id,
                game_row["id"]
            ))
            con.commit()

        await query.message.reply_text(
            f"🏆 برنده: {winner_name}\n\n"
            f"💰 جایزه: {fmt(reward)} TRX"
        )

    except Exception:

        change_balance(creator_id, amount)
        change_balance(user.id, amount)

        with closing(db()) as con:
            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            """, (game_row["id"],))
            con.commit()

        logger.exception("FRIEND GAME ERROR")

        await query.message.reply_text(
            "❌ خطا در بازی.\n"
            "💰 موجودی هر دو بازیکن برگشت داده شد."
        )


def name_of_from_row(row):
    return (
        row["first_name"]
        or ("@" + row["username"] if row["username"] else str(row["user_id"]))
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    if is_blocked(user.id) or is_virtual_user(user.id):
        return

    try:
        _, game, amount_text = query.data.split(":", 2)
        amount = Decimal(amount_text)
    except Exception:
        return

    if get_balance(user.id) < amount:

        await query.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(user.id, -amount):
        return

    # در هر لحظه فقط یک بازی ربات فعال است.
    active = context.application.bot_data.get(
        "bot_game_active",
        False
    )

    if active:

        change_balance(user.id, amount)

        await query.message.reply_text(
            "🤖 ربات در حال بازی است.\n"
            "موجودی شما برگشت داده شد ❌️"
        )

        return

    context.application.bot_data["bot_game_active"] = True

    try:

        await query.message.reply_text(
            f"🤖 بازی با ربات\n\n"
            f"💰 شرط: {fmt(amount)} TRX\n\n"
            "بازی شروع شد..."
        )

        # کاربر اول
        user_roll = await send_roll(
            context.bot,
            query.message.chat_id,
            game
        )

        await asyncio.sleep(1)

        # ربات
        bot_roll = await send_roll(
            context.bot,
            query.message.chat_id,
            game
        )

        user_value = user_roll.dice.value
        bot_value = bot_roll.dice.value

        if user_value == bot_value:

            change_balance(user.id, amount)

            await query.message.reply_text(
                "🤝 مساوی شد!\n\n"
                f"💰 {fmt(amount)} TRX برگشت داده شد."
            )

            return

        if user_value > bot_value:

            reward = payout(amount)

            change_balance(user.id, reward)

            await query.message.reply_text(
                "🏆 شما برنده شدید!\n\n"
                f"💰 جایزه: {fmt(reward)} TRX"
            )

        else:

            await query.message.reply_text(
                "❌ شما باختید."
            )

    except Exception:

        change_balance(user.id, amount)

        logger.exception("BOT GAME ERROR")

        await query.message.reply_text(
            "❌ خطا؛ مبلغ شما برگشت داده شد."
        )

    finally:
        context.application.bot_data["bot_game_active"] = False


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    with closing(db()) as con:

        row = con.execute("""
        SELECT *
        FROM games
        WHERE message_id=?
        AND chat_id=?
        AND status='pending'
        LIMIT 1
        """, (
            query.message.message_id,
            query.message.chat_id
        )).fetchone()

        if not row:
            await query.message.reply_text(
                "❌ بازی دیگر فعال نیست."
            )
            return

        if row["creator_id"] != user.id:

            await query.answer(
                "❌ فقط سازنده بازی می‌تواند آن را لغو کند.",
                show_alert=True
            )
            return

        con.execute("""
        UPDATE games
        SET status='cancelled'
        WHERE id=?
        """, (row["id"],))

        con.commit()

    amount = Decimal(row["amount"])

    change_balance(user.id, amount)

    await query.message.reply_text(
        f"❌ بازی لغو شد.\n"
        f"💰 {fmt(amount)} TRX برگشت داده شد."
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    user = update.effective_user
    message = update.message

    ensure_user(user)

    if is_blocked(user.id) or is_virtual_user(user.id):
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

    amount = parse_money(message.text)

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر است.\n"
            "مثال: انتقال 3"
        )
        return

    ensure_user(target)

    if is_virtual_user(target.id):
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
            str(amount)
        ))

        con.commit()

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {fmt(amount)} TRX"
    )


# ============================================================
# WITHDRAW
# ============================================================

async def withdraw_menu(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id) or is_virtual_user(user.id):
        return

    if not withdraw_enabled():

        await update.message.reply_text(
            "📤 برداشت در حال حاضر خاموش است."
        )
        return

    if get_balance(user.id) < MIN_WITHDRAW:

        await update.message.reply_text(
            f"❌ حداقل موجودی برای برداشت "
            f"{fmt(MIN_WITHDRAW)} TRX است.\n\n"
            f"💰 موجودی شما: {fmt(get_balance(user.id))} TRX"
        )
        return

    context.user_data["withdraw_mode"] = "amount"

    await update.message.reply_text(
        "📤 برداشت\n\n"
        f"حداقل برداشت: {fmt(MIN_WITHDRAW)} TRX\n\n"
        "مقدار برداشت را بفرست.\n"
        "مثال:\n"
        "3"
    )


# ============================================================
# REQUEST
# ============================================================

async def create_request(user_id, amount, wallet):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO requests
        (user_id, amount, wallet)
        VALUES (?, ?, ?)
        """, (
            user_id,
            str(amount),
            wallet
        ))

        con.commit()


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    text = message.text.strip()
    normalized = normalize_digits(text)

    # ========================================================
    # WITHDRAW AMOUNT
    # ========================================================

    if context.user_data.get("withdraw_mode") == "amount":

        amount = parse_money(normalized)

        if amount is None or amount < MIN_WITHDRAW:

            await message.reply_text(
                f"❌ حداقل برداشت {fmt(MIN_WITHDRAW)} TRX است."
            )
            return

        if amount > get_balance(user.id):

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        context.user_data["withdraw_amount"] = str(amount)
        context.user_data["withdraw_mode"] = "wallet"

        await message.reply_text(
            "💼 حالا آدرس کیف پول TRON را بفرست."
        )

        return

    # ========================================================
    # WITHDRAW WALLET
    # ========================================================

    if context.user_data.get("withdraw_mode") == "wallet":

        amount = Decimal(
            context.user_data.get(
                "withdraw_amount",
                "0"
            )
        )

        wallet = text.strip()

        if len(wallet) < 20:

            await message.reply_text(
                "❌ آدرس کیف پول نامعتبر است."
            )
            return

        if amount > get_balance(user.id):

            context.user_data.clear()

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        # مبلغ هنگام ثبت درخواست رزرو می‌شود.
        if not change_balance(user.id, -amount):

            await message.reply_text(
                "❌ خطا در برداشت."
            )
            return

        await create_request(
            user.id,
            amount,
            wallet
        )

        context.user_data.clear()

        await message.reply_text(
            "✅ درخواست برداشت ثبت شد.\n\n"
            f"💰 مقدار: {fmt(amount)} TRX\n"
            "💼 کیف پول ثبت شد.\n"
            "⏳ درخواست برای بررسی مدیریت ارسال شد."
        )

        return

    # ========================================================
    # GAME
    # ========================================================

    parsed = parse_game(normalized)

    if parsed:

        game, amount = parsed

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "❌ دستور بازی را داخل گپ استفاده کن."
            )
            return

        await create_game(
            update,
            context,
            game,
            amount
        )

        return

    # ========================================================
    # MENU
    # ========================================================

    if text == "💰 موجودی" or normalized.lower() in (
        "موجودی",
        "موجودی ترون",
        "trx"
    ):

        await balance(update, context)
        return

    if text == "🎮 بازی":

        await game_menu(update, context)
        return

    if text == "👥 بازی با دوستان":

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "👥 این بخش فقط در گپ قابل استفاده است."
            )
            return

        await message.reply_text(
            "👥 بازی با دوستان\n\n"
            "دستور بازی را بفرست:\n\n"
            "1 تاس 0.5\n"
            "1 بولینگ 0.5\n"
            "1 بسکتبال 0.5\n"
            "1 دارت 0.5\n\n"
            "بعد از ساخته شدن بازی، "
            "بازیکن دیگر روی دکمه «بازی با دوستان» می‌زند."
        )
        return

    if text == "💸 انتقال":

        await transfer(update, context)
        return

    if text == "📤 برداشت":

        await withdraw_menu(update, context)
        return

    if text == "📖 راهنما":

        await help_command(update, context)
        return

    # ========================================================
    # TRANSFER COMMAND
    # ========================================================

    if re.match(
        r"^(انتقال|transfer)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        await transfer(update, context)
        return

    # ========================================================
    # WITHDRAW COMMAND
    # ========================================================

    if re.match(
        r"^(برداشت|withdraw)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        if not withdraw_enabled():

            await message.reply_text(
                "📤 برداشت در حال حاضر خاموش است."
            )
            return

        amount = parse_money(
            normalized
        )

        if amount < MIN_WITHDRAW:

            await message.reply_text(
                f"❌ حداقل برداشت {fmt(MIN_WITHDRAW)} TRX است."
            )
            return

        if amount > get_balance(user.id):

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        context.user_data["withdraw_amount"] = str(amount)
        context.user_data["withdraw_mode"] = "wallet"

        await message.reply_text(
            "💼 آدرس کیف پول TRON را بفرست."
        )
        return


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی در گپ:\n\n"
        "1 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "1 بسکتبال 0.5\n"
        "1 دارت 0.5\n\n"
        "بعد از ساخت بازی:\n"
        "👥 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "❌ لغو\n\n"
        "💰 موجودی\n"
        "💸 انتقال 3 ← با Reply\n"
        "📤 برداشت 3\n\n"
        "مبالغ TRX در این ربات در سیستم داخلی "
        "مدیریت می‌شوند و تراکنش بلاکچینی خودکار انجام نمی‌شود."
    )


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_keyboard():

    status = (
        "🟢 برداشت روشن"
        if withdraw_enabled()
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
                "📋 درخواست‌ها",
                callback_data="admin_requests"
            )
        ],
        [
            InlineKeyboardButton(
                status,
                callback_data="admin_withdraw_toggle"
            )
        ]
    ])


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
            SELECT user_id, first_name, username,
                   balance, blocked, virtual_user
            FROM users
            ORDER BY CAST(balance AS REAL) DESC
            LIMIT 30
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
                f"💰 {fmt(Decimal(row['balance']))} TRX\n\n"
            )

        await query.edit_message_text(text or "کاربری وجود ندارد.")
        return

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = con.execute(
                "SELECT COALESCE(SUM(CAST(balance AS REAL)),0) FROM users"
            ).fetchone()[0]

            requests = con.execute(
                "SELECT COUNT(*) FROM requests WHERE status='pending'"
            ).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: {fmt(Decimal(str(total)))} TRX\n"
            f"📤 درخواست‌های در انتظار: {requests:,}\n\n"
            f"📤 وضعیت برداشت: "
            f"{'🟢 روشن' if withdraw_enabled() else '🔴 خاموش'}"
        )
        return

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "در پیوی بفرست:\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 10.50"
        )
        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "در پیوی بفرست:\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 5.50"
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
                "📋 درخواست در انتظار وجود ندارد."
            )
            return

        text = "📋 درخواست‌های برداشت\n\n"

        buttons = []

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 {row['user_id']}\n"
                f"💰 {fmt(Decimal(row['amount']))} TRX\n"
                f"💼 {row['wallet']}\n\n"
            )

            buttons.append([
                InlineKeyboardButton(
                    f"✅ انجام #{row['id']}",
                    callback_data=f"req_done:{row['id']}"
                ),
                InlineKeyboardButton(
                    f"❌ رد #{row['id']}",
                    callback_data=f"req_reject:{row['id']}"
                )
            ])

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    # --------------------------------------------------------
    # WITHDRAW TOGGLE
    # --------------------------------------------------------

    if data == "admin_withdraw_toggle":

        new_state = not withdraw_enabled()

        set_withdraw_enabled(new_state)

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            f"📤 برداشت: "
            f"{'🟢 روشن' if new_state else '🔴 خاموش'}",
            reply_markup=admin_keyboard()
        )
        return

    # --------------------------------------------------------
    # REQUEST DONE
    # --------------------------------------------------------

    if data.startswith("req_done:"):

        try:
            request_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        with closing(db()) as con:

            row = con.execute("""
            SELECT *
            FROM requests
            WHERE id=? AND status='pending'
            """, (request_id,)).fetchone()

            if not row:

                await query.answer(
                    "درخواست پیدا نشد.",
                    show_alert=True
                )
                return

            con.execute("""
            UPDATE requests
            SET status='completed'
            WHERE id=?
            """, (request_id,))

            con.commit()

        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "✅ درخواست برداشت شما توسط مدیریت "
                    "تأیید شد.\n\n"
                    f"💰 مقدار: {fmt(Decimal(row['amount']))} TRX"
                )
            )
        except Exception:
            pass

        await query.edit_message_text(
            f"✅ درخواست #{request_id} انجام شد."
        )
        return

    # --------------------------------------------------------
    # REQUEST REJECT
    # --------------------------------------------------------

    if data.startswith("req_reject:"):

        try:
            request_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        with closing(db()) as con:

            row = con.execute("""
            SELECT *
            FROM requests
            WHERE id=? AND status='pending'
            """, (request_id,)).fetchone()

            if not row:
                return

            con.execute("""
            UPDATE requests
            SET status='rejected'
            WHERE id=?
            """, (request_id,))

            con.commit()

        # مبلغ درخواست ردشده دوباره به موجودی کاربر برمی‌گردد.
        change_balance(
            row["user_id"],
            Decimal(row["amount"])
        )

        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "❌ درخواست برداشت شما رد شد.\n\n"
                    f"💰 مبلغ {fmt(Decimal(row['amount']))} TRX "
                    "به موجودی برگشت داده شد."
                )
            )
        except Exception:
            pass

        await query.edit_message_text(
            f"❌ درخواست #{request_id} رد شد و مبلغ برگشت داده شد."
        )
        return


# ============================================================
# ADMIN BALANCE
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

        amount = parse_money(
            context.args[1]
        )

    except Exception:
        amount = None

    if amount is None:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
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
        f"✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {fmt(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{fmt(get_balance(target_id))} TRX"
    )


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

        amount = parse_money(
            context.args[1]
        )

    except Exception:
        amount = None

    if amount is None:
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
        f"✅ موجودی کاهش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➖ {fmt(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{fmt(get_balance(target_id))} TRX"
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
            normalize_digits(context.args[0])
        )
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
        target_id = int(
            normalize_digits(context.args[0])
        )
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
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("balance", balance)
    )

    application.add_handler(
        CommandHandler("game", game_menu)
    )

    application.add_handler(
        CommandHandler("admin", admin)
    )

    application.add_handler(
        CommandHandler("addbalance", add_balance)
    )

    application.add_handler(
        CommandHandler("removebalance", remove_balance)
    )

    application.add_handler(
        CommandHandler("block", block)
    )

    application.add_handler(
        CommandHandler("unblock", unblock)
    )

    # Game buttons
    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            friend_join,
            pattern=r"^friend:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game,
            pattern=r"^botgame:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_game$"
        )
    )

    # Admin buttons
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_|^req_"
        )
    )

    # Text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info("BOT STARTED")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):

    logger.error(
        "BOT ERROR",
        exc_info=context.error
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
