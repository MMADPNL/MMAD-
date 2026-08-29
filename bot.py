import os
import sqlite3
import secrets
import logging
import asyncio
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 8552447077

REFERRAL_REWARD = 0.05
WINNER_PAYOUT = 0.185

DB_FILE = "BET_BT.db"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")

# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    with closing(get_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referral_reward REAL NOT NULL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                draws INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER DEFAULT NULL,
                game_type TEXT NOT NULL,
                emoji TEXT NOT NULL,
                amount REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                creator_result INTEGER DEFAULT NULL,
                opponent_result INTEGER DEFAULT NULL,
                winner_id INTEGER DEFAULT NULL,
                robot_game INTEGER NOT NULL DEFAULT 0,
                settled INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL,
                reference TEXT UNIQUE NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES('enabled', '1')
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES('owner_profit', '0')
        """)

        conn.commit()


# ============================================================
# HELPERS
# ============================================================

def ensure_user(user):
    if not user:
        return

    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO users(
                user_id,
                name,
                username
            )
            VALUES (?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                name=excluded.name,
                username=excluded.username
        """, (
            user.id,
            user.full_name or "",
            user.username or "",
        ))

        conn.commit()


def get_user(user_id):
    with closing(get_db()) as conn:
        return conn.execute("""
            SELECT *
            FROM users
            WHERE user_id=?
        """, (user_id,)).fetchone()


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0.0

    return float(row["balance"])


def money(value):
    value = round(float(value), 8)
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def normalize_number(value):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return (
        str(value)
        .translate(table)
        .replace(",", ".")
        .strip()
    )


def is_owner(user_id):
    return int(user_id) == OWNER_ID


def bot_enabled():
    with closing(get_db()) as conn:
        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key='enabled'
        """).fetchone()

    return not row or row["value"] == "1"


# ============================================================
# SAFE BALANCE
# ============================================================

def change_balance(
    user_id,
    amount,
    transaction_type,
    reference=None,
):
    amount = round(float(amount), 8)

    if reference is None:
        reference = secrets.token_hex(20)

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            user = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (user_id,)).fetchone()

            if not user:
                conn.rollback()
                return False

            old_balance = float(user["balance"])
            new_balance = round(old_balance + amount, 8)

            if new_balance < 0:
                conn.rollback()
                return False

            exists = conn.execute("""
                SELECT id
                FROM transactions
                WHERE reference=?
            """, (reference,)).fetchone()

            if exists:
                conn.rollback()
                return False

            conn.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
            """, (
                new_balance,
                user_id,
            ))

            conn.execute("""
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES (?, ?, ?, ?)
            """, (
                user_id,
                amount,
                transaction_type,
                reference,
            ))

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            logger.exception("balance error")
            return False


def add_owner_profit(amount):
    amount = round(float(amount), 8)

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute("""
                SELECT value
                FROM settings
                WHERE key='owner_profit'
            """).fetchone()

            old = float(row["value"]) if row else 0
            new = round(old + amount, 8)

            conn.execute("""
                INSERT INTO settings(key, value)
                VALUES('owner_profit', ?)

                ON CONFLICT(key)
                DO UPDATE SET value=excluded.value
            """, (str(new),))

            conn.commit()

        except Exception:
            conn.rollback()


# ============================================================
# REFERRAL
# ============================================================

def process_referral(new_user_id, referrer_id):
    if not referrer_id:
        return False

    if int(new_user_id) == int(referrer_id):
        return False

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            new_user = conn.execute("""
                SELECT referred_by
                FROM users
                WHERE user_id=?
            """, (new_user_id,)).fetchone()

            referrer = conn.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (referrer_id,)).fetchone()

            if not new_user or not referrer:
                conn.rollback()
                return False

            if new_user["referred_by"] is not None:
                conn.rollback()
                return False

            reference = (
                f"referral_{new_user_id}_{referrer_id}"
            )

            exists = conn.execute("""
                SELECT id
                FROM transactions
                WHERE reference=?
            """, (reference,)).fetchone()

            if exists:
                conn.rollback()
                return False

            conn.execute("""
                UPDATE users
                SET referred_by=?
                WHERE user_id=?
            """, (
                referrer_id,
                new_user_id,
            ))

            conn.execute("""
                UPDATE users
                SET
                    referrals=referrals+1,
                    referral_reward=referral_reward+?,
                    balance=balance+?
                WHERE user_id=?
            """, (
                REFERRAL_REWARD,
                REFERRAL_REWARD,
                referrer_id,
            ))

            conn.execute("""
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES (?, ?, ?, ?)
            """, (
                referrer_id,
                REFERRAL_REWARD,
                "referral",
                reference,
            ))

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            return False


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="referrals",
            )
        ],
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance",
            ),
            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎮 مثال بازی",
                callback_data="examples",
            )
        ],
    ]

    if is_owner(user_id):
        buttons.append([
            InlineKeyboardButton(
                "👑 پنل مدیریت",
                callback_data="admin",
            )
        ])

    return InlineKeyboardMarkup(buttons)


# ============================================================
# START - ONLY COMMAND
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if context.args:
        try:
            referrer_id = int(
                normalize_number(
                    context.args[0]
                )
            )

            process_referral(
                user.id,
                referrer_id,
            )

        except Exception:
            pass

    await update.message.reply_text(
        "🎮 به BET_BT خوش آمدی.\n\n"
        "💰 موجودی اولیه: 0 TRX\n\n"
        "🎲 تاس\n"
        "🏀 بسکتبال\n"
        "🎯 دارت\n"
        "🎳 بولینگ\n\n"
        "از دکمه‌های زیر استفاده کن.",
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# BALANCE BUTTON
# ============================================================

async def balance_button(update, context):
    query = update.callback_query
    user = update.effective_user

    ensure_user(user)

    await query.answer()

    await query.message.reply_text(
        "💰 موجودی شما:\n\n"
        f"🪙 {money(get_balance(user.id))} TRX"
    )


# ============================================================
# REFERRALS BUTTON
# ============================================================

async def referrals_button(update, context):
    query = update.callback_query
    user = update.effective_user

    ensure_user(user)

    row = get_user(user.id)

    try:
        bot = await context.bot.get_me()

        link = (
            f"https://t.me/{bot.username}"
            f"?start={user.id}"
        )

    except Exception:
        link = "خطا در ساخت لینک"

    await query.answer()

    await query.message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"👤 تعداد رفرال: {row['referrals']}\n"
        f"🎁 هر رفرال: {money(REFERRAL_REWARD)} TRX\n"
        f"💰 دریافتی: "
        f"{money(row['referral_reward'])} TRX\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# ============================================================
# EXAMPLES BUTTON
# ============================================================

async def examples_button(update, context):
    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🎮 مثال بازی\n\n"

        "🎲 1 تاس 0.1\n"
        "🏀 1 بسکتبال 0.1\n"
        "🎯 1 دارت 0.1\n"
        "🎳 1 بولینگ 0.1\n\n"

        "🏆 شرط 0.1 TRX\n"
        "💰 جایزه برنده: 0.185 TRX\n\n"

        "⚠️ ابتدا خود بازیکن ایموجی بازی را می‌فرستد؛ "
        "بعد نوبت ربات یا بازیکن دوم می‌شود."
    )


# ============================================================
# TRANSFER BUTTON
# ============================================================

async def transfer_button(update, context):
    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🔄 انتقال موجودی\n\n"
        "روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 0.1\n\n"
        "مثال:\n"
        "انتقال 10"
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_handler(update, context):
    message = update.message
    sender = update.effective_user

    if not message.reply_to_message:
        return

    if not message.text:
        return

    if not message.text.strip().startswith("انتقال"):
        return

    target = message.reply_to_message.from_user

    if not target:
        await message.reply_text(
            "❌ کاربر مقصد پیدا نشد."
        )
        return

    if target.is_bot:
        await message.reply_text(
            "❌ نمی‌توانی به ربات انتقال بدهی."
        )
        return

    if target.id == sender.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return

    ensure_user(sender)
    ensure_user(target)

    parts = message.text.strip().split()

    if len(parts) != 2:
        await message.reply_text(
            "❌ فرمت صحیح:\nانتقال 0.1"
        )
        return

    try:
        amount = float(
            normalize_number(parts[1])
        )
    except Exception:
        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )
        return

    amount = round(amount, 8)

    if amount <= 0:
        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    reference = "transfer_" + secrets.token_hex(16)

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            sender_row = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (sender.id,)).fetchone()

            target_row = conn.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (target.id,)).fetchone()

            if not sender_row or not target_row:
                conn.rollback()
                await message.reply_text(
                    "❌ کاربر پیدا نشد."
                )
                return

            sender_balance = float(
                sender_row["balance"]
            )

            if sender_balance < amount:
                conn.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )
                return

            conn.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
            """, (
                amount,
                sender.id,
            ))

            conn.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                amount,
                target.id,
            ))

            conn.execute("""
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES (?, ?, ?, ?)
            """, (
                sender.id,
                -amount,
                "transfer_send",
                reference + "_send",
            ))

            conn.execute("""
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES (?, ?, ?, ?)
            """, (
                target.id,
                amount,
                "transfer_receive",
                reference + "_receive",
            ))

            conn.commit()

        except Exception:
            conn.rollback()

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )
            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {target.full_name}\n"
        f"🪙 مبلغ: {money(amount)} TRX"
    )


# ============================================================
# GAME CONFIG
# ============================================================

GAME_EMOJIS = {
    "تاس": "🎲",
    "بسکتبال": "🏀",
    "دارت": "🎯",
    "بولینگ": "🎳",
}

EMOJI_TO_GAME = {
    "🎲": "تاس",
    "🏀": "بسکتبال",
    "🎯": "دارت",
    "🎳": "بولینگ",
}


# ============================================================
# GAME COMMAND
# ============================================================

def parse_game_command(text):
    parts = text.strip().split()

    if len(parts) != 3:
        return None

    try:
        player_count = int(
            normalize_number(parts[0])
        )
    except Exception:
        return None

    if player_count != 1:
        return None

    game_type = parts[1]

    if game_type not in GAME_EMOJIS:
        return None

    try:
        amount = float(
            normalize_number(parts[2])
        )
    except Exception:
        return None

    amount = round(amount, 8)

    if amount <= 0:
        return None

    return (
        game_type,
        GAME_EMOJIS[game_type],
        amount,
    )


# ============================================================
# ACTIVE GAME
# ============================================================

def get_active_game(user_id):
    with closing(get_db()) as conn:
        return conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting',
                'playing',
                'waiting_creator',
                'robot_turn'
            )
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY created_at DESC
            LIMIT 1
        """, (
            user_id,
            user_id,
        )).fetchone()


def get_game(game_id):
    with closing(get_db()) as conn:
        return conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
        """, (game_id,)).fetchone()


# ============================================================
# GAME KEYBOARD
# ============================================================

def game_keyboard(game_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join:{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"robot:{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel:{game_id}",
            )
        ],
    ])


# ============================================================
# CREATE GAME
# ============================================================

async def game_command(update, context):
    message = update.message
    user = update.effective_user

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    parsed = parse_game_command(
        message.text
    )

    if not parsed:
        return

    if not bot_enabled():
        return

    ensure_user(user)

    active = get_active_game(user.id)

    if active:
        await message.reply_text(
            "❌ شما در حال بازی هستید.\n\n"
            f"🎮 بازی فعلی: {active['game_type']}\n"
            f"🪙 شرط: {money(active['amount'])} TRX\n\n"
            "موجودی بازی فعال دوباره خرج نمی‌شود."
        )
        return

    game_type, emoji, amount = parsed

    reserve_reference = (
        "game_reserve_" + secrets.token_hex(16)
    )

    if not change_balance(
        user.id,
        -amount,
        "game_reserve",
        reserve_reference,
    ):
        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{money(get_balance(user.id))} TRX\n"
            f"🪙 شرط: {money(amount)} TRX"
        )
        return

    game_id = secrets.token_hex(12)

    try:
        with closing(get_db()) as conn:
            conn.execute("""
                INSERT INTO games(
                    game_id,
                    chat_id,
                    creator_id,
                    game_type,
                    emoji,
                    amount,
                    status,
                    robot_game,
                    settled
                )
                VALUES(
                    ?, ?, ?, ?, ?, ?, 'waiting', 0, 0
                )
            """, (
                game_id,
                message.chat.id,
                user.id,
                game_type,
                emoji,
                amount,
            ))

            conn.commit()

    except Exception:
        change_balance(
            user.id,
            amount,
            "game_create_refund",
            "create_refund_" + game_id,
        )

        await message.reply_text(
            "❌ ساخت بازی انجام نشد.\n"
            "💰 مبلغ برگشت داده شد."
        )
        return

    await message.reply_text(
        f"{emoji} بازی ساخته شد.\n\n"
        f"🎮 بازی: {game_type}\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🪙 شرط: {money(amount)} TRX\n\n"
        "👥 برای بازی با دوستان، یک نفر وارد شود.\n"
        "🤖 برای بازی با ربات، دکمه ربات را بزن.\n\n"
        f"⚠️ ابتدا خود بازیکن {emoji} را می‌فرستد.",
        reply_markup=game_keyboard(game_id),
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game(update, context):
    query = update.callback_query
    user = update.effective_user

    game_id = query.data.split(":", 1)[1]
    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )
        return

    if game["status"] != "waiting":
        await query.answer(
            "❌ بازی دیگر قابل ورود نیست.",
            show_alert=True,
        )
        return

    if game["creator_id"] == user.id:
        await query.answer(
            "❌ نمی‌توانی وارد بازی خودت شوی.",
            show_alert=True,
        )
        return

    active = get_active_game(user.id)

    if active:
        await query.answer(
            "❌ شما در حال بازی هستید.",
            show_alert=True,
        )
        return

    ensure_user(user)

    amount = float(game["amount"])

    reserve_reference = (
        "game_reserve_" + secrets.token_hex(16)
    )

    if not change_balance(
        user.id,
        -amount,
        "game_reserve",
        reserve_reference,
    ):
        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )
        return

    success = False

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
                SELECT *
                FROM games
                WHERE game_id=?
            """, (game_id,)).fetchone()

            if (
                current
                and current["status"] == "waiting"
                and not current["settled"]
            ):
                conn.execute("""
                    UPDATE games
                    SET
                        opponent_id=?,
                        status='playing'
                    WHERE game_id=?
                    AND status='waiting'
                    AND settled=0
                """, (
                    user.id,
                    game_id,
                ))

                success = True

            conn.commit()

        except Exception:
            conn.rollback()

    if not success:
        change_balance(
            user.id,
            amount,
            "join_refund",
            "join_refund_" + game_id + "_" + secrets.token_hex(8),
        )

        await query.answer(
            "❌ بازی پر شد یا لغو شد.",
            show_alert=True,
        )
        return

    await query.answer("✅ وارد بازی شدی.")

    await query.edit_message_text(
        f"{game['emoji']} بازی شروع شد.\n\n"
        f"👤 بازیکن اول: {game['creator_id']}\n"
        f"👤 بازیکن دوم: {user.full_name}\n"
        f"🪙 شرط هر نفر: {money(amount)} TRX\n\n"
        f"هر بازیکن خودش {game['emoji']} را می‌فرستد.\n"
        "⏳ منتظر پرتاب هر دو بازیکن..."
    )


# ============================================================
# ROBOT GAME
# ============================================================

async def robot_game(update, context):
    query = update.callback_query
    user = update.effective_user

    game_id = query.data.split(":", 1)[1]
    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )
        return

    if game["creator_id"] != user.id:
        await query.answer(
            "❌ فقط سازنده می‌تواند.",
            show_alert=True,
        )
        return

    if game["status"] != "waiting":
        await query.answer(
            "❌ بازی دیگر قابل شروع نیست.",
            show_alert=True,
        )
        return

    with closing(get_db()) as conn:
        conn.execute("""
            UPDATE games
            SET
                opponent_id=0,
                robot_game=1,
                status='waiting_creator'
            WHERE game_id=?
            AND status='waiting'
            AND settled=0
        """, (game_id,))
        conn.commit()

    await query.answer()

    await query.edit_message_text(
        f"🤖 بازی با ربات\n\n"
        f"🎮 {game['game_type']}\n"
        f"🪙 شرط: {money(game['amount'])} TRX\n\n"
        f"1️⃣ اول خودت {game['emoji']} را بفرست.\n"
        f"2️⃣ بعد ربات {game['emoji']} را می‌اندازد."
    )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):
    query = update.callback_query
    user = update.effective_user

    game_id = query.data.split(":", 1)[1]
    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )
        return

    if game["creator_id"] != user.id:
        await query.answer(
            "❌ فقط سازنده می‌تواند لغو کند.",
            show_alert=True,
        )
        return

    if game["status"] not in (
        "waiting",
        "waiting_creator",
    ):
        await query.answer(
            "❌ این بازی قابل لغو نیست.",
            show_alert=True,
        )
        return

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
                SELECT settled
                FROM games
                WHERE game_id=?
            """, (game_id,)).fetchone()

            if not current or current["settled"]:
                conn.rollback()

                await query.answer(
                    "❌ بازی قبلاً تسویه شده.",
                    show_alert=True,
                )
                return

            conn.execute("""
                UPDATE games
                SET
                    status='cancelled',
                    settled=1
                WHERE game_id=?
                AND settled=0
            """, (game_id,))

            conn.commit()

        except Exception:
            conn.rollback()

            await query.answer(
                "❌ خطا در لغو.",
                show_alert=True,
            )
            return

    refunded = change_balance(
        user.id,
        float(game["amount"]),
        "game_refund",
        "refund_" + game_id,
    )

    await query.answer("❌ بازی لغو شد.")

    if refunded:
        await query.edit_message_text(
            "❌ بازی لغو شد.\n\n"
            f"💰 {money(game['amount'])} TRX "
            "به موجودی شما برگشت داده شد."
        )
    else:
        await query.edit_message_text(
            "❌ بازی لغو شد."
        )


# ============================================================
# SETTLE GAME
# ============================================================

async def settle_game(
    game_id,
    context,
    creator_value,
    opponent_value,
    robot=False,
):
    game = get_game(game_id)

    if not game or game["settled"]:
        return

    creator_id = int(game["creator_id"])

    opponent_id = (
        int(game["opponent_id"])
        if game["opponent_id"] is not None
        else None
    )

    amount = float(game["amount"])

    # ----------------------------
    # DRAW
    # ----------------------------

    if creator_value == opponent_value:

        with closing(get_db()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                current = conn.execute("""
                    SELECT settled
                    FROM games
                    WHERE game_id=?
                """, (game_id,)).fetchone()

                if not current or current["settled"]:
                    conn.rollback()
                    return

                conn.execute("""
                    UPDATE games
                    SET
                        creator_result=?,
                        opponent_result=?,
                        status='finished',
                        settled=1
                    WHERE game_id=?
                    AND settled=0
                """, (
                    creator_value,
                    opponent_value,
                    game_id,
                ))

                conn.commit()

            except Exception:
                conn.rollback()
                return

        change_balance(
            creator_id,
            amount,
            "draw_refund_creator",
            "draw_creator_" + game_id,
        )

        if not robot and opponent_id:
            change_balance(
                opponent_id,
                amount,
                "draw_refund_opponent",
                "draw_opponent_" + game_id,
            )

        with closing(get_db()) as conn:
            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    draws=draws+1
                WHERE user_id=?
            """, (creator_id,))

            if not robot and opponent_id:
                conn.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        draws=draws+1
                    WHERE user_id=?
                """, (opponent_id,))

            conn.commit()

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"{game['emoji']} نتیجه بازی\n\n"
                f"👤 بازیکن اول: {creator_value}\n"
                f"{'🤖 ربات' if robot else '👤 بازیکن دوم'}: "
                f"{opponent_value}\n\n"
                "🤝 مساوی شد.\n"
                "💰 مبلغ شرط برگشت داده شد."
            ),
        )

        return

    # ----------------------------
    # WINNER
    # ----------------------------

    if creator_value > opponent_value:
        winner_id = creator_id
    else:
        winner_id = opponent_id

    if robot and winner_id == 0:
        winner_payout = 0
        owner_profit = amount * 2
    else:
        winner_payout = WINNER_PAYOUT
        owner_profit = (
            amount * 2
        ) - winner_payout

    # ----------------------------
    # LOCK GAME
    # ----------------------------

    with closing(get_db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
                SELECT settled
                FROM games
                WHERE game_id=?
            """, (game_id,)).fetchone()

            if not current or current["settled"]:
                conn.rollback()
                return

            conn.execute("""
                UPDATE games
                SET
                    creator_result=?,
                    opponent_result=?,
                    winner_id=?,
                    status='finished',
                    settled=1
                WHERE game_id=?
                AND settled=0
            """, (
                creator_value,
                opponent_value,
                winner_id,
                game_id,
            ))

            conn.commit()

        except Exception:
            conn.rollback()
            return

    # ----------------------------
    # PAY WINNER
    # ----------------------------

    if winner_id and winner_id != 0:
        change_balance(
            winner_id,
            winner_payout,
            "game_win",
            "win_" + game_id,
        )

    add_owner_profit(
        owner_profit
    )

    # ----------------------------
    # STATS
    # ----------------------------

    with closing(get_db()) as conn:

        if winner_id and winner_id != 0:
            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (winner_id,))

        if not robot and opponent_id:
            loser_id = (
                opponent_id
                if winner_id == creator_id
                else creator_id
            )

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    losses=losses+1
                WHERE user_id=?
            """, (loser_id,))

        conn.commit()

    # ----------------------------
    # RESULT MESSAGE
    # ----------------------------

    if robot:

        if winner_id == creator_id:

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"
                f"👤 تو: {creator_value}\n"
                f"🤖 ربات: {opponent_value}\n\n"
                "🏆 تو برنده شدی!\n"
                f"💰 جایزه: {money(winner_payout)} TRX"
            )

        else:

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"
                f"👤 تو: {creator_value}\n"
                f"🤖 ربات: {opponent_value}\n\n"
                "🤖 ربات برنده شد."
            )

    else:

        text = (
            f"{game['emoji']} نتیجه بازی\n\n"
            f"👤 بازیکن اول: {creator_value}\n"
            f"👤 بازیکن دوم: {opponent_value}\n\n"
            "🏆 بازی تمام شد.\n"
            f"💰 جایزه برنده: "
            f"{money(winner_payout)} TRX"
        )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text,
    )


# ============================================================
# TELEGRAM GAME EMOJI
# ============================================================

async def game_emoji_handler(update, context):
    message = update.message

    if not message:
        return

    if not message.dice:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    user = update.effective_user

    if not user:
        return

    emoji = message.dice.emoji

    if emoji not in EMOJI_TO_GAME:
        return

    game_type = EMOJI_TO_GAME[emoji]

    value = int(message.dice.value)

    ensure_user(user)

    with closing(get_db()) as conn:
        game = conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting_creator',
                'playing',
                'robot_turn'
            )
            AND game_type=?
            AND emoji=?
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY created_at DESC
            LIMIT 1
        """, (
            game_type,
            emoji,
            user.id,
            user.id,
        )).fetchone()

    if not game:
        return

    game_id = game["game_id"]

    # ========================================================
    # ROBOT
    # ========================================================

    if game["robot_game"] == 1:

        if game["creator_id"] != user.id:
            return

        if game["status"] != "waiting_creator":
            return

        if game["creator_result"] is not None:
            return

        with closing(get_db()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                current = conn.execute("""
                    SELECT *
                    FROM games
                    WHERE game_id=?
                """, (game_id,)).fetchone()

                if (
                    not current
                    or current["settled"]
                    or current["creator_result"] is not None
                    or current["status"] != "waiting_creator"
                ):
                    conn.rollback()
                    return

                conn.execute("""
                    UPDATE games
                    SET
                        creator_result=?,
                        status='robot_turn'
                    WHERE game_id=?
                    AND settled=0
                """, (
                    value,
                    game_id,
                ))

                conn.commit()

            except Exception:
                conn.rollback()
                return

        await message.reply_text(
            f"✅ پرتاب تو ثبت شد: {value}\n\n"
            f"🤖 حالا ربات {emoji} می‌اندازد..."
        )

        await asyncio.sleep(1)

        try:
            robot_message = await context.bot.send_dice(
                chat_id=message.chat.id,
                emoji=emoji,
            )

            robot_value = int(
                robot_message.dice.value
            )

        except Exception:

            with closing(get_db()) as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")

                    current = conn.execute("""
                        SELECT settled
                        FROM games
                        WHERE game_id=?
                    """, (game_id,)).fetchone()

                    if current and not current["settled"]:

                        conn.execute("""
                            UPDATE games
                            SET
                                status='cancelled',
                                settled=1
                            WHERE game_id=?
                            AND settled=0
                        """, (game_id,))

                        conn.commit()

                    else:
                        conn.rollback()

                except Exception:
                    conn.rollback()

            change_balance(
                user.id,
                float(game["amount"]),
                "robot_error_refund",
                "robot_error_refund_" + game_id,
            )

            await message.reply_text(
                "❌ پرتاب ربات انجام نشد.\n"
                "💰 مبلغ بازی برگشت داده شد."
            )

            return

        await settle_game(
            game_id,
            context,
            creator_value=value,
            opponent_value=robot_value,
            robot=True,
        )

        return

    # ========================================================
    # FRIEND
    # ========================================================

    if game["status"] != "playing":
        return

    creator_id = int(
        game["creator_id"]
    )

    opponent_id = int(
        game["opponent_id"]
    )

    if user.id == creator_id:

        if game["creator_result"] is not None:
            return

        with closing(get_db()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                current = conn.execute("""
                    SELECT *
                    FROM games
                    WHERE game_id=?
                """, (game_id,)).fetchone()

                if (
                    not current
                    or current["settled"]
                    or current["creator_result"] is not None
                ):
                    conn.rollback()
                    return

                conn.execute("""
                    UPDATE games
                    SET creator_result=?
                    WHERE game_id=?
                    AND settled=0
                    AND creator_result IS NULL
                """, (
                    value,
                    game_id,
                ))

                conn.commit()

            except Exception:
                conn.rollback()
                return

        await message.reply_text(
            f"✅ پرتاب بازیکن اول ثبت شد: {value}\n"
            f"⏳ منتظر بازیکن دوم {emoji}..."
        )

    elif user.id == opponent_id:

        if game["opponent_result"] is not None:
            return

        with closing(get_db()) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")

                current = conn.execute("""
                    SELECT *
                    FROM games
                    WHERE game_id=?
                """, (game_id,)).fetchone()

                if (
                    not current
                    or current["settled"]
                    or current["opponent_result"] is not None
                ):
                    conn.rollback()
                    return

                conn.execute("""
                    UPDATE games
                    SET opponent_result=?
                    WHERE game_id=?
                    AND settled=0
                    AND opponent_result IS NULL
                """, (
                    value,
                    game_id,
                ))

                conn.commit()

            except Exception:
                conn.rollback()
                return

        await message.reply_text(
            f"✅ پرتاب بازیکن دوم ثبت شد: {value}"
        )

    else:
        return

    updated = get_game(game_id)

    if not updated:
        return

    creator_result = updated["creator_result"]
    opponent_result = updated["opponent_result"]

    if (
        creator_result is not None
        and opponent_result is not None
    ):
        await settle_game(
            game_id,
            context,
            creator_value=int(creator_result),
            opponent_value=int(opponent_result),
            robot=False,
        )


# ============================================================
# ADMIN
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add",
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove",
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 موجودی کاربر",
                callback_data="admin_balance",
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            )
        ],
        [
            InlineKeyboardButton(
                "🟢 روشن",
                callback_data="admin_enable",
            ),
            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_disable",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ],
    ])


async def admin_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        "👑 پنل مدیریت BET_BT",
        reply_markup=admin_keyboard(),
    )


async def admin_add_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()
    context.user_data["admin_action"] = "add"

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_remove_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()
    context.user_data["admin_action"] = "remove"

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_balance_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()
    context.user_data["admin_action"] = "balance"

    await query.answer()

    await query.message.reply_text(
        "💰 آیدی عددی کاربر را بفرست."
    )


async def admin_stats_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    with closing(get_db()) as conn:

        users = conn.execute("""
            SELECT COUNT(*) c
            FROM users
        """).fetchone()["c"]

        games = conn.execute("""
            SELECT COUNT(*) c
            FROM games
        """).fetchone()["c"]

        balances = conn.execute("""
            SELECT COALESCE(SUM(balance), 0) b
            FROM users
        """).fetchone()["b"]

        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key='owner_profit'
        """).fetchone()

        profit = (
            float(row["value"])
            if row
            else 0
        )

    await query.answer()

    await query.message.reply_text(
        "📊 آمار BET_BT\n\n"
        f"👥 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"💰 مجموع موجودی کاربران: "
        f"{money(balances)} TRX\n"
        f"📈 سود داخلی: {money(profit)} TRX\n\n"
        f"🔌 وضعیت: "
        f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}"
    )


async def admin_enable_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO settings(key, value)
            VALUES('enabled', '1')

            ON CONFLICT(key)
            DO UPDATE SET value='1'
        """)
        conn.commit()

    await query.answer("🟢 روشن شد.")

    await query.message.reply_text(
        "🟢 ربات روشن شد.",
        reply_markup=admin_keyboard(),
    )


async def admin_disable_button(update, context):
    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    with closing(get_db()) as conn:
        conn.execute("""
            INSERT INTO settings(key, value)
            VALUES('enabled', '0')

            ON CONFLICT(key)
            DO UPDATE SET value='0'
        """)
        conn.commit()

    await query.answer("🔴 خاموش شد.")

    await query.message.reply_text(
        "🔴 ربات خاموش شد.",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not is_owner(user.id):
        return

    action = context.user_data.get(
        "admin_action"
    )

    if not action:
        return

    text = message.text.strip()

    # ----------------------------
    # BALANCE
    # ----------------------------

    if action == "balance":

        try:
            target_id = int(
                normalize_number(text)
            )
        except Exception:
            await message.reply_text(
                "❌ آیدی نامعتبر است."
            )
            return

        row = get_user(target_id)

        if not row:
            await message.reply_text(
                "❌ کاربر پیدا نشد."
            )
            return

        await message.reply_text(
            "💰 اطلاعات کاربر\n\n"
            f"🆔 {target_id}\n"
            f"👤 {row['name']}\n"
            f"💰 موجودی: "
            f"{money(row['balance'])} TRX\n"
            f"👥 رفرال: {row['referrals']}\n"
            f"🎮 بازی: {row['games']}\n"
            f"🏆 برد: {row['wins']}\n"
            f"❌ باخت: {row['losses']}\n"
            f"🤝 مساوی: {row['draws']}"
        )

        context.user_data.clear()
        return

    # ----------------------------
    # ADD / REMOVE
    # ----------------------------

    if action in ("add", "remove"):

        parts = text.split()

        if len(parts) != 2:
            await message.reply_text(
                "❌ فرمت صحیح:\n"
                "آیدی مبلغ\n\n"
                "مثال:\n"
                "123456789 10"
            )
            return

        try:
            target_id = int(
                normalize_number(parts[0])
            )

            amount = float(
                normalize_number(parts[1])
            )

        except Exception:
            await message.reply_text(
                "❌ مقدار نامعتبر است."
            )
            return

        amount = round(amount, 8)

        if amount <= 0:
            await message.reply_text(
                "❌ مبلغ باید بیشتر از صفر باشد."
            )
            return

        target = get_user(target_id)

        if not target:
            await message.reply_text(
                "❌ کاربر پیدا نشد.\n"
                "کاربر باید ابتدا /start بزند."
            )
            return

        if action == "add":

            success = change_balance(
                target_id,
                amount,
                "admin_add",
                "admin_add_" + secrets.token_hex(16),
            )

            title = "افزایش"

        else:

            success = change_balance(
                target_id,
                -amount,
                "admin_remove",
                "admin_remove_" + secrets.token_hex(16),
            )

            title = "کسر"

        if not success:
            await message.reply_text(
                "❌ عملیات انجام نشد.\n"
                "ممکن است موجودی برای کسر کافی نباشد."
            )
            return

        await message.reply_text(
            f"✅ {title} موجودی انجام شد.\n\n"
            f"🆔 کاربر: {target_id}\n"
            f"🪙 مبلغ: {money(amount)} TRX\n"
            f"💰 موجودی جدید: "
            f"{money(get_balance(target_id))} TRX"
        )

        context.user_data.clear()


# ============================================================
# HOME BUTTON
# ============================================================

async def home_button(update, context):
    query = update.callback_query
    user = update.effective_user

    await query.answer()

    await query.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):
    query = update.callback_query
    data = query.data

    if data == "home":
        await home_button(update, context)

    elif data == "balance":
        await balance_button(update, context)

    elif data == "referrals":
        await referrals_button(update, context)

    elif data == "examples":
        await examples_button(update, context)

    elif data == "transfer":
        await transfer_button(update, context)

    elif data == "admin":
        await admin_button(update, context)

    elif data == "admin_add":
        await admin_add_button(update, context)

    elif data == "admin_remove":
        await admin_remove_button(update, context)

    elif data == "admin_balance":
        await admin_balance_button(update, context)

    elif data == "admin_stats":
        await admin_stats_button(update, context)

    elif data == "admin_enable":
        await admin_enable_button(update, context)

    elif data == "admin_disable":
        await admin_disable_button(update, context)

    elif data.startswith("join:"):
        await join_game(update, context)

    elif data.startswith("robot:"):
        await robot_game(update, context)

    elif data.startswith("cancel:"):
        await cancel_game(update, context)


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update, context):
    message = update.message

    if not message or not message.text:
        return

    text = message.text.strip()

    # ADMIN
    if (
        is_owner(update.effective_user.id)
        and context.user_data.get("admin_action")
    ):
        await admin_text_handler(
            update,
            context,
        )
        return

    # TRANSFER
    if text.startswith("انتقال"):
        await transfer_handler(
            update,
            context,
        )
        return

    # GAME
    parsed = parse_game_command(text)

    if parsed:
        await game_command(
            update,
            context,
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):
    logger.error(
        "Unhandled exception",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    # این دیتابیس را حذف نکن.
    # موجودی‌ها و اطلاعات کاربران اینجا ذخیره می‌شوند.
    init_database()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # فقط همین یک دستور فعال است.
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # تمام منوهای ربات با دکمه هستند.
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # ایموجی‌های بازی که خود کاربر می‌فرستد.
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_emoji_handler,
        )
    )

    # انتقال و دستورات بازی گروه.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
