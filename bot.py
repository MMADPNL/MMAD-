# ============================================================
# BET_BT - Telegram Group Game Bot
# Python 3.10+
# python-telegram-bot 20+
#
# Virtual TRX only - NO real blockchain / wallet / payment
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
import secrets
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

# مالک را از Environment بخوان؛ در GitHub Secrets مقدار OWNER_ID بگذار
OWNER_ID = int(os.getenv("OWNER_ID", "8552447077"))

CHANNEL_USERNAME = "@zobxt"

DB_FILE = "bet_bt.db"

REFERRAL_REWARD = 0.05

MIN_BET = 0.1

# جایزه برد:
# 0.1 bet -> 0.19 winner
# یعنی اصل شرط + 0.09 سود
PAYOUT_MULTIPLIER = 1.9

GAME_NAMES = {
    "dice": "🎲 تاس",
    "bowling": "🎳 بولینگ",
    "darts": "🎯 دارت",
    "basketball": "🏀 بسکتبال",
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")

# ============================================================
# DATABASE
# ============================================================

db_lock = asyncio.Lock()


def db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            referrer_id INTEGER DEFAULT NULL,
            referral_paid INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            message_id INTEGER,
            creator_id INTEGER,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT,
            bet REAL,
            mode TEXT,
            status TEXT,
            creator_roll INTEGER DEFAULT NULL,
            opponent_roll INTEGER DEFAULT NULL,
            creator_done INTEGER DEFAULT 0,
            opponent_done INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            reward REAL DEFAULT 0.05,
            paid INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('enabled','1')"
    )

    conn.commit()
    conn.close()


def ensure_user(tg_user):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (tg_user.id,),
    )

    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO users(user_id,username,first_name,balance)
            VALUES(?,?,?,0)
            """,
            (
                tg_user.id,
                tg_user.username or "",
                tg_user.first_name or "",
            ),
        )
    else:
        cur.execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                tg_user.username or "",
                tg_user.first_name or "",
                tg_user.id,
            ),
        )

    conn.commit()
    conn.close()


def get_balance(user_id):
    conn = db()
    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()

    if not row:
        return 0.0

    return float(row["balance"])


def set_balance(user_id, amount):
    conn = db()
    conn.execute(
        "UPDATE users SET balance=? WHERE user_id=?",
        (round(amount, 8), user_id),
    )
    conn.commit()
    conn.close()


def add_balance(user_id, amount, tx_type, description):
    conn = db()

    conn.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (round(amount, 8), user_id),
    )

    conn.execute(
        """
        INSERT INTO transactions(user_id,amount,type,description)
        VALUES(?,?,?,?)
        """,
        (
            user_id,
            amount,
            tx_type,
            description,
        ),
    )

    conn.commit()
    conn.close()


def subtract_balance(user_id, amount, description):
    conn = db()

    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()

    if not row:
        conn.close()
        return False

    balance = float(row["balance"])

    if balance + 1e-9 < amount:
        conn.close()
        return False

    conn.execute(
        "UPDATE users SET balance=balance-? WHERE user_id=?",
        (round(amount, 8), user_id),
    )

    conn.execute(
        """
        INSERT INTO transactions(user_id,amount,type,description)
        VALUES(?,?,?,?)
        """,
        (
            user_id,
            -amount,
            "game",
            description,
        ),
    )

    conn.commit()
    conn.close()

    return True


# ============================================================
# SETTINGS
# ============================================================

def bot_enabled():
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key='enabled'"
    ).fetchone()
    conn.close()

    return not row or row["value"] == "1"


def set_bot_enabled(value):
    conn = db()
    conn.execute(
        """
        INSERT INTO settings(key,value)
        VALUES('enabled',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        ("1" if value else "0",),
    )
    conn.commit()
    conn.close()


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return text.translate(table)


def parse_amount(text):
    text = normalize_digits(text)
    text = text.replace(",", ".").replace("٫", ".")

    match = re.search(r"(\d+(?:\.\d+)?)", text)

    if not match:
        return None

    try:
        amount = float(match.group(1))
        return round(amount, 8)
    except Exception:
        return None


def money(amount):
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def game_type_from_text(text):
    text = normalize_digits(text.lower())

    if "تاس" in text or "dice" in text or "🎲" in text:
        return "dice"

    if "بولینگ" in text or "bowling" in text or "🎳" in text:
        return "bowling"

    if "دارت" in text or "darts" in text or "🎯" in text:
        return "darts"

    if "بسکتبال" in text or "basketball" in text or "🏀" in text:
        return "basketball"

    return None


def parse_game_command(text):
    """
    قبول می‌کند:

    1 تاس 0.1
    1 بولینگ 0.1
    1 دارت 0.1
    1 بسکتبال 0.1

    و همچنین:

    تاس 0.1
    dice 0.1
    """

    original = text.strip()
    normalized = normalize_digits(original.lower())

    gtype = game_type_from_text(normalized)

    if not gtype:
        return None, None

    amount = parse_amount(normalized)

    if amount is None:
        return None, None

    return gtype, amount


def new_game_id():
    return secrets.token_hex(5)


# ============================================================
# FORCE JOIN
# ============================================================

async def is_member(bot, user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:
        logger.warning("Force join check failed: %s", e)

        # اگر کانال برای ربات قابل بررسی نبود،
        # بازی را متوقف نمی‌کنیم.
        return True


async def force_join(update, context):
    user = update.effective_user

    if await is_member(context.bot, user.id):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 عضویت در کانال",
                url="https://t.me/zobxt",
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_join",
            )
        ],
    ])

    if update.callback_query:
        try:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(
                "🔒 برای استفاده از بازی باید ابتدا در کانال عضو شوید.",
                reply_markup=keyboard,
            )
        except Exception:
            pass
    else:
        await update.effective_message.reply_text(
            "🔒 برای استفاده از بازی باید ابتدا در کانال عضو شوید.",
            reply_markup=keyboard,
        )

    return False


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    # referral
    if context.args:
        try:
            referrer = int(context.args[0])

            if referrer != user.id:
                conn = db()

                row = conn.execute(
                    "SELECT referrer_id FROM users WHERE user_id=?",
                    (user.id,),
                ).fetchone()

                if row and row["referrer_id"] is None:
                    conn.execute(
                        """
                        UPDATE users
                        SET referrer_id=?
                        WHERE user_id=?
                        """,
                        (referrer, user.id),
                    )

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO referrals
                        (referrer_id,referred_id,reward,paid)
                        VALUES(?,?,?,0)
                        """,
                        (
                            referrer,
                            user.id,
                            REFERRAL_REWARD,
                        ),
                    )

                    conn.commit()
                conn.close()

        except Exception:
            pass

    text = (
        "🎮 BET_BT آماده است.\n\n"
        "🎲 بازی‌های گپ:\n"
        "1 تاس 0.1\n"
        "1 بولینگ 0.1\n"
        "1 دارت 0.1\n"
        "1 بسکتبال 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر رول می‌کند، سپس ربات رول می‌کند.\n\n"
        "👥 بازی با دوستان:\n"
        "سازنده اول رول می‌کند، سپس حریف رول می‌کند.\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "💸 انتقال با Reply:\n"
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "انتقال 0.1\n\n"
        "💎 TRX این ربات کاملاً مجازی است و هیچ تراکنش واقعی ندارد."
    )

    await update.effective_message.reply_text(text)


# ============================================================
# BALANCE
# ============================================================

async def balance_handler(update, context):
    ensure_user(update.effective_user)

    if not await force_join(update, context):
        return

    bal = get_balance(update.effective_user.id)

    await update.effective_message.reply_text(
        f"💰 موجودی مجازی شما:\n\n"
        f"💎 {money(bal)} TRX"
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral_handler(update, context):
    user = update.effective_user
    ensure_user(user)

    bot_username = context.bot.username

    link = f"https://t.me/{bot_username}?start={user.id}"

    conn = db()
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
        (user.id,),
    ).fetchone()["c"]
    conn.close()

    await update.effective_message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"🎁 پاداش هر زیرمجموعه: {money(REFERRAL_REWARD)} TRX\n"
        f"👤 تعداد زیرمجموعه: {count}"
    )


# ============================================================
# TRANSFER BY REPLY
# ============================================================

async def transfer_handler(update, context, amount):
    user = update.effective_user
    message = update.effective_message

    if not message.reply_to_message:
        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )
        return

    target = message.reply_to_message.from_user

    if not target:
        return

    if target.is_bot:
        await message.reply_text("❌ انتقال به ربات امکان‌پذیر نیست.")
        return

    if target.id == user.id:
        await message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return

    if amount is None or amount <= 0:
        await message.reply_text("❌ مبلغ نامعتبر است.")
        return

    if amount < MIN_BET:
        await message.reply_text(
            f"❌ حداقل انتقال {money(MIN_BET)} TRX است."
        )
        return

    ensure_user(user)
    ensure_user(target)

    # قفل منطقی تراکنش با بررسی و کسر اتمیک
    ok = subtract_balance(
        user.id,
        amount,
        f"انتقال به {target.id}",
    )

    if not ok:
        await message.reply_text("❌ موجودی کافی نیست.")
        return

    add_balance(
        target.id,
        amount,
        "transfer",
        f"انتقال از {user.id}",
    )

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {target.first_name}\n"
        f"💎 مبلغ: {money(amount)} TRX"
    )


# ============================================================
# GAME CREATION
# ============================================================

async def create_game(update, context, game_type, amount):
    user = update.effective_user
    message = update.effective_message

    ensure_user(user)

    if not bot_enabled() and user.id != OWNER_ID:
        await message.reply_text("🚫 ربات موقتاً خاموش است.")
        return

    if not await force_join(update, context):
        return

    if amount < MIN_BET:
        await message.reply_text(
            f"❌ حداقل شرط {money(MIN_BET)} TRX است."
        )
        return

    if amount <= 0:
        await message.reply_text("❌ مبلغ نامعتبر است.")
        return

    # کسر اولیه سازنده
    ok = subtract_balance(
        user.id,
        amount,
        f"رزرو شرط {GAME_NAMES[game_type]}",
    )

    if not ok:
        await message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return

    game_id = new_game_id()

    conn = db()

    conn.execute(
        """
        INSERT INTO games(
            game_id,chat_id,message_id,creator_id,
            game_type,bet,mode,status
        )
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            game_id,
            message.chat_id,
            0,
            user.id,
            game_type,
            amount,
            "waiting",
            "waiting",
        ),
    )

    conn.commit()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"gamebot:{game_id}",
            ),
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friends:{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}",
            )
        ],
    ])

    sent = await message.reply_text(
        f"🎮 بازی جدید\n\n"
        f"{GAME_NAMES[game_type]}\n"
        f"💎 شرط: {money(amount)} TRX\n"
        f"👤 سازنده: {user.first_name}\n\n"
        f"یکی از حالت‌ها را انتخاب کنید:",
        reply_markup=keyboard,
    )

    conn.execute(
        "UPDATE games SET message_id=? WHERE game_id=?",
        (sent.message_id, game_id),
    )

    conn.commit()
    conn.close()


# ============================================================
# LOAD GAME
# ============================================================

def get_game(game_id):
    conn = db()

    row = conn.execute(
        "SELECT * FROM games WHERE game_id=?",
        (game_id,),
    ).fetchone()

    conn.close()

    return row


def update_game(game_id, **kwargs):
    if not kwargs:
        return

    conn = db()

    parts = []
    values = []

    for key, value in kwargs.items():
        parts.append(f"{key}=?")
        values.append(value)

    values.append(game_id)

    conn.execute(
        f"""
        UPDATE games
        SET {",".join(parts)}
        WHERE game_id=?
        """,
        values,
    )

    conn.commit()
    conn.close()


# ============================================================
# CANCEL / REFUND
# ============================================================

def refund_creator_once(game_id):
    conn = db()

    row = conn.execute(
        "SELECT * FROM games WHERE game_id=?",
        (game_id,),
    ).fetchone()

    if not row:
        conn.close()
        return False

    if row["status"] in ("refunded", "finished"):
        conn.close()
        return False

    creator_id = row["creator_id"]
    amount = float(row["bet"])

    conn.execute(
        "UPDATE users SET balance=balance+? WHERE user_id=?",
        (amount, creator_id),
    )

    conn.execute(
        """
        INSERT INTO transactions
        (user_id,amount,type,description)
        VALUES(?,?,?,?,?)
        """.replace(
            "VALUES(?,?,?,?,?)",
            "VALUES(?,?,?,?)"
        ),
        (
            creator_id,
            amount,
            "refund",
            f"بازگشت شرط بازی {game_id}",
        ),
    )

    conn.execute(
        """
        UPDATE games
        SET status='refunded'
        WHERE game_id=?
        """,
        (game_id,),
    )

    conn.commit()
    conn.close()

    return True


# ============================================================
# DICE / GAME VALUE
# ============================================================

def result_value(game_type, dice_result):
    # Telegram dice:
    # dice: 1-6
    # bowling: 1-6
    # darts: 1-6
    # basketball: 1-5
    return dice_result


async def send_game_roll(bot, chat_id, game_type):
    emoji = {
        "dice": "🎲",
        "bowling": "🎳",
        "darts": "🎯",
        "basketball": "🏀",
    }[game_type]

    msg = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji,
    )

    return result_value(game_type, msg.dice.value)


# ============================================================
# BOT GAME
# ============================================================

async def start_bot_game(query, context, game):
    user = query.from_user

    if user.id != game["creator_id"]:
        await query.answer(
            "❌ فقط سازنده بازی می‌تواند این گزینه را بزند.",
            show_alert=True,
        )
        return

    if game["status"] != "waiting":
        await query.answer(
            "❌ این بازی دیگر فعال نیست.",
            show_alert=True,
        )
        return

    update_game(
        game["game_id"],
        mode="bot",
        status="bot_running",
    )

    await query.answer()

    await query.edit_message_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"{GAME_NAMES[game['game_type']]}\n"
        f"👤 {user.first_name} نوبت شماست..."
    )

    try:
        # کاربر باید خودش دکمه/رول را بزند
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎲 رول من",
                    callback_data=f"userroll:{game['game_id']}",
                )
            ]
        ])

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=f"👤 {user.first_name}، نوبت شماست:",
            reply_markup=keyboard,
        )

    except Exception:
        refund_creator_once(game["game_id"])


# ============================================================
# USER ROLL AGAINST BOT
# ============================================================

async def user_roll_bot(query, context, game):
    user = query.from_user

    if user.id != game["creator_id"]:
        await query.answer("❌ این بازی برای شما نیست.", show_alert=True)
        return

    if game["status"] != "bot_running":
        await query.answer("❌ بازی فعال نیست.", show_alert=True)
        return

    await query.answer()

    try:
        user_result = await send_game_roll(
            context.bot,
            game["chat_id"],
            game["game_type"],
        )

        update_game(
            game["game_id"],
            creator_roll=user_result,
            creator_done=1,
            status="bot_waiting",
        )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=f"👤 {user.first_name} رول کرد: {user_result}\n\n"
                 f"🤖 حالا ربات رول می‌کند..."
        )

        # فاصله کوتاه برای طبیعی شدن بازی
        await asyncio.sleep(1)

        bot_result = await send_game_roll(
            context.bot,
            game["chat_id"],
            game["game_type"],
        )

        update_game(
            game["game_id"],
            opponent_roll=bot_result,
            opponent_done=1,
            status="finished",
        )

        if user_result > bot_result:
            winner_name = user.first_name
            winner_id = user.id

        elif bot_result > user_result:
            winner_name = "🤖 ربات"
            winner_id = None

        else:
            winner_name = None
            winner_id = None

        amount = float(game["bet"])

        if winner_id:
            payout = round(amount * PAYOUT_MULTIPLIER, 8)

            add_balance(
                winner_id,
                payout,
                "win",
                f"برد بازی {game['game_id']}",
            )

            result_text = (
                f"🏆 برنده: {winner_name}\n"
                f"💰 جایزه: {money(payout)} TRX"
            )
        elif winner_name == "🤖 ربات":
            result_text = (
                f"🏆 برنده: 🤖 ربات\n"
                f"💰 جایزه‌ای به کاربر تعلق نگرفت."
            )
        else:
            # مساوی: شرط برمی‌گردد
            add_balance(
                user.id,
                amount,
                "draw_refund",
                f"بازگشت مساوی {game['game_id']}",
            )

            result_text = (
                "🤝 مساوی شد.\n"
                f"💰 {money(amount)} TRX برگشت داده شد."
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🎮 نتیجه بازی\n\n"
                f"👤 {user.first_name}: {user_result}\n"
                f"🤖 ربات: {bot_result}\n\n"
                f"{result_text}"
            ),
        )

    except Exception as e:
        logger.exception("BOT GAME ERROR: %s", e)

        update_game(
            game["game_id"],
            status="error",
        )

        if refund_creator_once(game["game_id"]):
            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "🛡️ بازی با خطا مواجه شد.\n"
                    "💰 مبلغ شرط به موجودی کاربر برگشت داده شد."
                ),
            )


# ============================================================
# FRIEND GAME
# ============================================================

async def start_friend_game(query, context, game):
    user = query.from_user

    if user.id != game["creator_id"]:
        await query.answer(
            "❌ فقط سازنده بازی می‌تواند بازی دوستان را فعال کند.",
            show_alert=True,
        )
        return

    if game["status"] != "waiting":
        await query.answer(
            "❌ این بازی دیگر فعال نیست.",
            show_alert=True,
        )
        return

    update_game(
        game["game_id"],
        mode="friends",
        status="waiting_opponent",
    )

    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 پیوستن به بازی",
                callback_data=f"join:{game['game_id']}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو سازنده",
                callback_data=f"cancel:{game['game_id']}",
            )
        ]
    ])

    await query.edit_message_text(
        f"👥 بازی با دوستان\n\n"
        f"{GAME_NAMES[game['game_type']]}\n"
        f"💎 شرط: {money(game['bet'])} TRX\n"
        f"👤 سازنده: {user.first_name}\n\n"
        "یک نفر می‌تواند به عنوان حریف وارد شود.",
        reply_markup=keyboard,
    )


# ============================================================
# JOIN FRIEND GAME
# ============================================================

async def join_friend_game(query, context, game):
    user = query.from_user

    if game["status"] != "waiting_opponent":
        await query.answer(
            "❌ بازی پیدا نشد یا قبلاً شروع شده است.",
            show_alert=True,
        )
        return

    if user.id == game["creator_id"]:
        await query.answer(
            "❌ سازنده نمی‌تواند حریف خودش باشد.",
            show_alert=True,
        )
        return

    if not await is_member(context.bot, user.id):
        await query.answer(
            "🔒 ابتدا در کانال @zobxt عضو شوید.",
            show_alert=True,
        )
        return

    ensure_user(user)

    amount = float(game["bet"])

    # کسر حریف
    ok = subtract_balance(
        user.id,
        amount,
        f"رزرو شرط حریف {game['game_id']}",
    )

    if not ok:
        await query.answer(
            "❌ موجودی کافی ندارید.",
            show_alert=True,
        )
        return

    # فقط یک حریف
    update_game(
        game["game_id"],
        opponent_id=user.id,
        status="friends_creator_turn",
    )

    await query.answer()

    await query.edit_message_text(
        f"👥 بازی شروع شد\n\n"
        f"{GAME_NAMES[game['game_type']]}\n"
        f"👤 سازنده: در انتظار رول\n"
        f"👤 حریف: {user.first_name}\n\n"
        "اول سازنده رول می‌کند."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎲 رول سازنده",
                callback_data=f"friendcreator:{game['game_id']}",
            )
        ]
    ])

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text="👤 نوبت سازنده است:",
        reply_markup=keyboard,
    )


# ============================================================
# FRIEND CREATOR ROLL
# ============================================================

async def friend_creator_roll(query, context, game):
    user = query.from_user

    if user.id != game["creator_id"]:
        await query.answer(
            "❌ فقط سازنده می‌تواند رول کند.",
            show_alert=True,
        )
        return

    if game["status"] != "friends_creator_turn":
        await query.answer(
            "❌ نوبت شما نیست.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        value = await send_game_roll(
            context.bot,
            game["chat_id"],
            game["game_type"],
        )

        update_game(
            game["game_id"],
            creator_roll=value,
            creator_done=1,
            status="friends_opponent_turn",
        )

        opponent_id = game["opponent_id"]

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"👤 {user.first_name} رول کرد: {value}\n\n"
                "👥 حالا نوبت حریف است."
            ),
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎲 رول حریف",
                    callback_data=f"friendopponent:{game['game_id']}",
                )
            ]
        ])

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text="👤 حریف، نوبت شماست:",
            reply_markup=keyboard,
        )

    except Exception as e:
        logger.exception("CREATOR ROLL ERROR: %s", e)

        update_game(
            game["game_id"],
            status="error",
        )

        refund_creator_once(game["game_id"])

        # بازگرداندن شرط حریف
        if game["opponent_id"]:
            add_balance(
                game["opponent_id"],
                float(game["bet"]),
                "refund",
                f"خطای بازی {game['game_id']}",
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text="🛡️ بازی خطا خورد؛ مبالغ به بازیکنان برگشت داده شد.",
        )


# ============================================================
# FRIEND OPPONENT ROLL
# ============================================================

async def friend_opponent_roll(query, context, game):
    user = query.from_user

    if user.id != game["opponent_id"]:
        await query.answer(
            "❌ فقط حریف می‌تواند رول کند.",
            show_alert=True,
        )
        return

    if game["status"] != "friends_opponent_turn":
        await query.answer(
            "❌ نوبت شما نیست.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        value = await send_game_roll(
            context.bot,
            game["chat_id"],
            game["game_type"],
        )

        update_game(
            game["game_id"],
            opponent_roll=value,
            opponent_done=1,
            status="finished",
        )

        creator_id = game["creator_id"]
        opponent_id = game["opponent_id"]

        creator_name = game_creator_name = "سازنده"
        opponent_name = user.first_name

        try:
            creator_member = await context.bot.get_chat_member(
                game["chat_id"],
                creator_id,
            )
            creator_name = creator_member.user.first_name
        except Exception:
            pass

        creator_roll = int(game["creator_roll"])
        opponent_roll = int(value)
        amount = float(game["bet"])

        if creator_roll > opponent_roll:
            winner_id = creator_id
            winner_name = creator_name

        elif opponent_roll > creator_roll:
            winner_id = opponent_id
            winner_name = opponent_name

        else:
            winner_id = None
            winner_name = None

        if winner_id:
            # مجموع دو شرط = 2 * bet
            # برنده طبق ضریب 1.9 دریافت می‌کند
            payout = round(amount * PAYOUT_MULTIPLIER, 8)

            add_balance(
                winner_id,
                payout,
                "win",
                f"برد بازی دوستان {game['game_id']}",
            )

            result = (
                f"🏆 برنده: {winner_name}\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        else:
            # مساوی: هر دو شرط خودشان را پس می‌گیرند
            add_balance(
                creator_id,
                amount,
                "draw_refund",
                f"مساوی بازی {game['game_id']}",
            )

            add_balance(
                opponent_id,
                amount,
                "draw_refund",
                f"مساوی بازی {game['game_id']}",
            )

            result = (
                "🤝 بازی مساوی شد.\n"
                f"💰 شرط هر دو بازیکن برگشت داده شد."
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🎮 نتیجه بازی دوستان\n\n"
                f"👤 {creator_name}: {creator_roll}\n"
                f"👤 {opponent_name}: {opponent_roll}\n\n"
                f"{result}"
            ),
        )

    except Exception as e:
        logger.exception("OPPONENT ROLL ERROR: %s", e)

        update_game(
            game["game_id"],
            status="error",
        )

        # هر دو مبلغ برگردد
        refund_creator_once(game["game_id"])

        if game["opponent_id"]:
            add_balance(
                game["opponent_id"],
                float(game["bet"]),
                "refund",
                f"خطای بازی {game['game_id']}",
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🛡️ بازی با خطا مواجه شد.\n"
                "💰 مبلغ هر دو بازیکن برگشت داده شد."
            ),
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(query, context, game):
    user = query.from_user

    if user.id != game["creator_id"] and user.id != OWNER_ID:
        await query.answer(
            "❌ اجازه لغو این بازی را ندارید.",
            show_alert=True,
        )
        return

    if game["status"] in ("finished", "refunded", "error"):
        await query.answer(
            "❌ بازی قبلاً تمام شده.",
            show_alert=True,
        )
        return

    amount = float(game["bet"])

    # بازگرداندن سازنده
    refund_creator_once(game["game_id"])

    # اگر حریف وارد شده، پول او هم برگردد
    if game["opponent_id"]:
        add_balance(
            game["opponent_id"],
            amount,
            "refund",
            f"لغو بازی {game['game_id']}",
        )

    await query.answer("✅ بازی لغو شد.")

    try:
        await query.edit_message_text(
            "❌ بازی لغو شد.\n"
            "💰 موجودی بازیکنان برگشت داده شد."
        )
    except Exception:
        pass


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""

    if data == "check_join":
        if await is_member(context.bot, query.from_user.id):
            await query.answer(
                "✅ عضویت شما تأیید شد.",
                show_alert=True,
            )
        else:
            await query.answer(
                "❌ هنوز در کانال عضو نشده‌اید.",
                show_alert=True,
            )
        return

    if ":" not in data:
        await query.answer()
        return

    action, game_id = data.split(":", 1)

    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )
        return

    if action == "gamebot":
        await start_bot_game(query, context, game)
        return

    if action == "friends":
        await start_friend_game(query, context, game)
        return

    if action == "join":
        await join_friend_game(query, context, game)
        return

    if action == "userroll":
        await user_roll_bot(query, context, game)
        return

    if action == "friendcreator":
        await friend_creator_roll(query, context, game)
        return

    if action == "friendopponent":
        await friend_opponent_roll(query, context, game)
        return

    if action == "cancel":
        await cancel_game(query, context, game)
        return

    await query.answer("❌ عملیات نامعتبر است.", show_alert=True)


# ============================================================
# TRANSFER / TEXT COMMANDS
# ============================================================

async def text_handler(update, context):
    message = update.effective_message

    if not message or not message.text:
        return

    text = message.text.strip()

    ensure_user(update.effective_user)

    normalized = normalize_digits(text.lower())

    # --------------------------
    # موجودی
    # --------------------------

    if normalized in (
        "موجودی",
        "موجودی 💰",
        "balance",
        "bal",
    ):
        await balance_handler(update, context)
        return

    # --------------------------
    # زیرمجموعه
    # --------------------------

    if normalized in (
        "زیرمجموعه",
        "زیر مجموعه",
        "ref",
        "referral",
    ):
        await referral_handler(update, context)
        return

    # --------------------------
    # انتقال
    # --------------------------

    if normalized.startswith("انتقال") or normalized.startswith("transfer"):
        amount = parse_amount(normalized)

        if amount is None:
            await message.reply_text(
                "❌ مبلغ را بنویسید.\n\n"
                "مثال:\n"
                "انتقال 0.1"
            )
            return

        await transfer_handler(
            update,
            context,
            amount,
        )
        return

    # --------------------------
    # بازی
    # --------------------------

    game_type, amount = parse_game_command(text)

    if game_type and amount is not None:
        await create_game(
            update,
            context,
            game_type,
            amount,
        )
        return


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            ),
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin_users",
            ),
        ],
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
                "🎮 بازی‌های فعال",
                callback_data="admin_games",
            ),
        ],
        [
            InlineKeyboardButton(
                "🟢 روشن / 🔴 خاموش",
                callback_data="admin_toggle",
            ),
        ],
    ])


async def admin_command(update, context):
    user = update.effective_user

    if user.id != OWNER_ID:
        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    await update.effective_message.reply_text(
        "👑 پنل مدیریت BET_BT\n\n"
        "مدیریت آمار، کاربران، موجودی و بازی‌ها:",
        reply_markup=admin_keyboard(),
    )


async def admin_callback(update, context):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return

    data = query.data

    if data == "admin_stats":
        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        total_balance = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS s FROM users"
        ).fetchone()["s"]

        games = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM games
            WHERE status NOT IN ('finished','refunded','error')
            """
        ).fetchone()["c"]

        conn.close()

        await query.answer()

        await query.message.reply_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users}\n"
            f"💎 مجموع موجودی مجازی: {money(total_balance)} TRX\n"
            f"🎮 بازی‌های فعال: {games}"
        )
        return

    if data == "admin_users":
        conn = db()

        rows = conn.execute(
            """
            SELECT user_id,first_name,username,balance
            FROM users
            ORDER BY balance DESC
            LIMIT 20
            """
        ).fetchall()

        conn.close()

        text = "👥 کاربران:\n\n"

        if not rows:
            text += "کاربری وجود ندارد."
        else:
            for r in rows:
                text += (
                    f"👤 {r['first_name'] or '-'}\n"
                    f"🆔 {r['user_id']}\n"
                    f"💎 {money(float(r['balance']))} TRX\n\n"
                )

        await query.answer()
        await query.message.reply_text(text)
        return

    if data == "admin_games":
        conn = db()

        rows = conn.execute(
            """
            SELECT game_id,game_type,bet,creator_id,
                   opponent_id,status
            FROM games
            WHERE status NOT IN ('finished','refunded','error')
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        conn.close()

        text = "🎮 بازی‌های فعال:\n\n"

        if not rows:
            text += "بازی فعالی نیست."
        else:
            for r in rows:
                text += (
                    f"🆔 {r['game_id']}\n"
                    f"{GAME_NAMES.get(r['game_type'],r['game_type'])}\n"
                    f"💎 {money(float(r['bet']))} TRX\n"
                    f"📌 {r['status']}\n\n"
                )

        await query.answer()
        await query.message.reply_text(text)
        return

    if data == "admin_toggle":
        enabled = bot_enabled()
        set_bot_enabled(not enabled)

        await query.answer(
            "تغییر کرد.",
            show_alert=True,
        )

        await query.message.reply_text(
            "🟢 ربات روشن شد."
            if not enabled
            else
            "🔴 ربات خاموش شد."
        )
        return

    if data == "admin_add":
        await query.answer()

        await query.message.reply_text(
            "➕ افزایش موجودی\n\n"
            "برای افزایش موجودی از دستور زیر استفاده کن:\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 100"
        )
        return

    if data == "admin_remove":
        await query.answer()

        await query.message.reply_text(
            "➖ کسر موجودی\n\n"
            "دستور:\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 100"
        )
        return


# ============================================================
# ADMIN BALANCE COMMANDS
# ============================================================

async def addbalance_command(update, context):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "فرمت:\n"
            "/addbalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = parse_amount(context.args[1])

        if amount is None or amount <= 0:
            raise ValueError

        ensure_user_by_id(target_id)

        add_balance(
            target_id,
            amount,
            "admin_add",
            f"افزایش توسط مالک {OWNER_ID}",
        )

        await update.effective_message.reply_text(
            "✅ افزایش موجودی انجام شد.\n\n"
            f"🆔 {target_id}\n"
            f"➕ {money(amount)} TRX\n"
            f"💰 موجودی جدید: {money(get_balance(target_id))} TRX"
        )

    except Exception:
        await update.effective_message.reply_text(
            "❌ اطلاعات نامعتبر است."
        )


async def removebalance_command(update, context):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "فرمت:\n"
            "/removebalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = parse_amount(context.args[1])

        if amount is None or amount <= 0:
            raise ValueError

        ensure_user_by_id(target_id)

        ok = subtract_balance(
            target_id,
            amount,
            "کسر توسط مالک",
        )

        if not ok:
            await update.effective_message.reply_text(
                "❌ موجودی کاربر کافی نیست."
            )
            return

        await update.effective_message.reply_text(
            "✅ کسر موجودی انجام شد.\n\n"
            f"🆔 {target_id}\n"
            f"➖ {money(amount)} TRX\n"
            f"💰 موجودی جدید: {money(get_balance(target_id))} TRX"
        )

    except Exception:
        await update.effective_message.reply_text(
            "❌ اطلاعات نامعتبر است."
        )


def ensure_user_by_id(user_id):
    conn = db()

    conn.execute(
        """
        INSERT OR IGNORE INTO users
        (user_id,username,first_name,balance)
        VALUES(?,?,?,0)
        """,
        (
            user_id,
            "",
            str(user_id),
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )

    # تلاش برای برگشت وجه اگر خطای مربوط به callback بازی بود
    try:
        if isinstance(update, Update):
            query = update.callback_query

            if query and query.data:
                data = query.data

                if ":" in data:
                    action, game_id = data.split(":", 1)

                    if action in (
                        "userroll",
                        "friendcreator",
                        "friendopponent",
                        "join",
                    ):
                        game = get_game(game_id)

                        if game and game["status"] not in (
                            "finished",
                            "refunded",
                        ):
                            refund_creator_once(game_id)

    except Exception:
        logger.exception("Refund error")


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN در Environment تنظیم نشده است."
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
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("addbalance", addbalance_command)
    )

    application.add_handler(
        CommandHandler("removebalance", removebalance_command)
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
            pattern=r"^(gamebot|friends|join|userroll|friendcreator|friendopponent|cancel|check_join):?"
        )
    )

    # Persian / group text commands
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("BET_BT starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
