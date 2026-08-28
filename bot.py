# =========================================================
# BET_BT BOT
# Python 3.10+
# python-telegram-bot 20+
# Virtual TRX balance
# =========================================================

import os
import re
import sqlite3
import asyncio
import logging
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

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL_USERNAME = "@zobxt"

DB_FILE = "bet_bt.db"

MIN_BET = 0.1
PAYOUT = 0.19
REFERRAL_REWARD = 0.05

GAME_TIMEOUT = 300

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =========================================================
# DATABASE
# =========================================================

db_lock = asyncio.Lock()


def db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            referrer INTEGER DEFAULT NULL,
            referral_paid INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            chat_id INTEGER,
            creator_id INTEGER,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT,
            amount REAL,
            status TEXT,
            creator_roll INTEGER DEFAULT NULL,
            opponent_roll INTEGER DEFAULT NULL,
            message_id INTEGER DEFAULT NULL,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            type TEXT,
            game_id TEXT DEFAULT '',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    """)

    con.commit()
    con.close()


# =========================================================
# HELPERS
# =========================================================

def normalize_digits(text):
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return text.translate(table)


def now():
    return datetime.utcnow().isoformat()


def money(x):
    return f"{float(x):.2f}".rstrip("0").rstrip(".")


def get_user(user):
    con = db()
    row = con.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()
    con.close()
    return row


def ensure_user(user, referrer=None):
    con = db()
    cur = con.cursor()

    row = cur.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if not row:
        cur.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance, referrer, created_at)
            VALUES (?, ?, ?, 0, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            referrer if referrer and referrer != user.id else None,
            now()
        ))

    else:
        cur.execute("""
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
        """, (
            user.username or "",
            user.first_name or "",
            user.id
        ))

    con.commit()
    con.close()


def get_balance(user_id):
    con = db()
    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    con.close()

    if not row:
        return 0.0

    return float(row["balance"])


async def change_balance(
    user_id,
    amount,
    tx_type="admin",
    game_id=""
):
    async with db_lock:
        con = db()

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not row:
                con.rollback()
                return False, 0.0

            old_balance = float(row["balance"])
            new_balance = old_balance + float(amount)

            if new_balance < -0.000001:
                con.rollback()
                return False, old_balance

            con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
            """, (
                round(new_balance, 8),
                user_id
            ))

            con.execute("""
                INSERT INTO transactions
                (user_id, amount, type, game_id, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                user_id,
                float(amount),
                tx_type,
                game_id,
                now()
            ))

            con.commit()

            return True, new_balance

        except Exception:
            con.rollback()
            logger.exception("balance error")
            return False, old_balance if "old_balance" in locals() else 0

        finally:
            con.close()


def is_admin(user_id):
    if user_id == OWNER_ID:
        return True

    con = db()
    row = con.execute(
        "SELECT user_id FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()
    con.close()

    return bool(row)


def add_admin(user_id):
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
        (user_id,)
    )
    con.commit()
    con.close()


def remove_admin(user_id):
    con = db()
    con.execute(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,)
    )
    con.commit()
    con.close()


# =========================================================
# FORCE JOIN
# =========================================================

async def is_joined(bot, user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception as e:
        logger.warning("join check: %s", e)

        # اگر کانال از طرف تلگرام قابل بررسی نبود
        # کاربر را بی‌جهت قفل نمی‌کنیم
        return True


async def join_required(update, context):
    user = update.effective_user

    if await is_joined(context.bot, user.id):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 عضویت در کانال",
                url="https://t.me/zobxt"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_join"
            )
        ]
    ])

    text = (
        "🔒 برای استفاده از ربات ابتدا باید در کانال عضو شوید.\n\n"
        "بعد از عضویت روی «بررسی عضویت» بزن."
    )

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(
            text,
            reply_markup=keyboard
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=keyboard
        )

    return False


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    referrer = None

    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer = int(arg.replace("ref_", ""))
            except Exception:
                referrer = None

    ensure_user(user, referrer)

    if not await join_required(update, context):
        return

    text = (
        "🎮 BET_BT آماده است.\n\n"
        "🎲 بازی‌های گپ:\n"
        "1 تاس 0.1\n"
        "1 بولینگ 0.1\n"
        "1 دارت 0.1\n"
        "1 بسکتبال 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر رول می‌کند، سپس ربات.\n\n"
        "👥 بازی با دوستان:\n"
        "سازنده رول می‌کند، سپس حریف.\n\n"
        "💰 موجودی: دستور «موجودی»\n"
        "💸 انتقال: روی پیام کاربر Reply کنید و بنویسید:\n"
        "انتقال 0.1\n\n"
        "🔗 کانال: @zobxt"
    )

    await update.effective_message.reply_text(text)


# =========================================================
# BALANCE
# =========================================================

async def balance_handler(update, context):
    user = update.effective_user

    ensure_user(user)

    if not await join_required(update, context):
        return

    bal = get_balance(user.id)

    await update.effective_message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"💎 {money(bal)} TRX"
    )


# =========================================================
# REFERRAL
# =========================================================

async def referral(update, context):
    user = update.effective_user

    ensure_user(user)

    if not await join_required(update, context):
        return

    link = f"https://t.me/BET_BTBOT?start=ref_{user.id}"

    con = db()

    count_row = con.execute("""
        SELECT COUNT(*) AS c
        FROM users
        WHERE referrer=?
    """, (user.id,)).fetchone()

    con.close()

    count = count_row["c"]

    await update.effective_message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👥 تعداد زیرمجموعه: {count}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)}"
    )


# =========================================================
# TRANSFER BY REPLY
# =========================================================

async def transfer_handler(update, context):
    msg = update.effective_message
    user = update.effective_user

    if not msg:
        return

    ensure_user(user)

    if not await join_required(update, context):
        return

    if not msg.reply_to_message:
        await msg.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )
        return

    text = normalize_digits(msg.text or "")

    m = re.search(
        r"انتقال\s+([0-9]+(?:[.,][0-9]+)?)",
        text,
        re.IGNORECASE
    )

    if not m:
        return

    try:
        amount = float(m.group(1).replace(",", "."))
    except Exception:
        await msg.reply_text("❌ مبلغ نامعتبر است.")
        return

    if amount <= 0:
        await msg.reply_text("❌ مبلغ باید بیشتر از صفر باشد.")
        return

    target = msg.reply_to_message.from_user

    if not target:
        await msg.reply_text("❌ کاربر مقصد پیدا نشد.")
        return

    if target.id == user.id:
        await msg.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return

    ensure_user(target)

    # قفل تراکنش
    async with db_lock:
        con = db()

        try:
            con.execute("BEGIN IMMEDIATE")

            sender = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user.id,)
            ).fetchone()

            receiver = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (target.id,)
            ).fetchone()

            if not sender or not receiver:
                con.rollback()
                await msg.reply_text("❌ خطا در اطلاعات کاربران.")
                return

            sender_balance = float(sender["balance"])

            if sender_balance < amount:
                con.rollback()
                await msg.reply_text(
                    f"❌ موجودی کافی نیست.\n"
                    f"موجودی: {money(sender_balance)} TRX"
                )
                return

            con.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=?",
                (amount, user.id)
            )

            con.execute(
                "UPDATE users SET balance=balance+? WHERE user_id=?",
                (amount, target.id)
            )

            con.execute("""
                INSERT INTO transactions
                (user_id, amount, type, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                user.id,
                -amount,
                "transfer_out",
                now()
            ))

            con.execute("""
                INSERT INTO transactions
                (user_id, amount, type, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                target.id,
                amount,
                "transfer_in",
                now()
            ))

            con.commit()

        except Exception:
            con.rollback()
            logger.exception("transfer error")

            await msg.reply_text(
                "❌ انتقال انجام نشد."
            )
            return

        finally:
            con.close()

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {target.first_name}\n"
        f"💸 مبلغ: {money(amount)} TRX"
    )


# =========================================================
# GAME PARSER
# =========================================================

GAME_MAP = {
    "تاس": "🎲",
    "بولینگ": "🎳",
    "دارت": "🎯",
    "بسکتبال": "🏀",

    "dice": "🎲",
    "bowling": "🎳",
    "darts": "🎯",
    "dart": "🎯",
    "basketball": "🏀",
}


def parse_game(text):
    text = normalize_digits(text or "").strip().lower()

    # مثال:
    # 1 تاس 0.1
    # 1 بولینگ 0.1
    # 1 دارت 0.1
    # 1 بسکتبال 0.1

    pattern = (
        r"^1\s+"
        r"(تاس|بولینگ|دارت|بسکتبال|dice|bowling|darts|dart|basketball)"
        r"\s+"
        r"([0-9]+(?:[.,][0-9]+)?)$"
    )

    m = re.match(pattern, text)

    if not m:
        return None

    game_name = m.group(1)
    amount_text = m.group(2).replace(",", ".")

    try:
        amount = float(amount_text)
    except Exception:
        return None

    emoji = GAME_MAP.get(game_name)

    if not emoji:
        return None

    return emoji, amount


# =========================================================
# GAME CREATION
# =========================================================

async def create_game(update, context, emoji, amount):
    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if amount < MIN_BET:
        await update.effective_message.reply_text(
            f"❌ حداقل مبلغ بازی {money(MIN_BET)} TRX است."
        )
        return

    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "🎮 بازی را داخل گپ انجام دهید."
        )
        return

    if not await join_required(update, context):
        return

    # قفل کسر موجودی
    async with db_lock:
        con = db()

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user.id,)
            ).fetchone()

            if not row:
                con.rollback()
                await update.effective_message.reply_text(
                    "❌ کاربر ثبت نشده است."
                )
                return

            balance = float(row["balance"])

            if balance < amount:
                con.rollback()

                await update.effective_message.reply_text(
                    f"❌ موجودی کافی نیست.\n"
                    f"موجودی: {money(balance)} TRX"
                )
                return

            game_id = secrets.token_hex(8)

            con.execute(
                "UPDATE users SET balance=balance-? WHERE user_id=?",
                (amount, user.id)
            )

            con.execute("""
                INSERT INTO games
                (game_id, chat_id, creator_id, game_type,
                 amount, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'waiting', ?)
            """, (
                game_id,
                chat.id,
                user.id,
                emoji,
                amount,
                now()
            ))

            con.execute("""
                INSERT INTO transactions
                (user_id, amount, type, game_id, created_at)
                VALUES (?, ?, 'game_lock', ?, ?)
            """, (
                user.id,
                -amount,
                game_id,
                now()
            ))

            con.commit()

        except Exception:
            con.rollback()

            logger.exception("create game error")

            await update.effective_message.reply_text(
                "❌ بازی ساخته نشد و موجودی شما کسر نشد."
            )
            return

        finally:
            con.close()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"botgame:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friendgame:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]
    ])

    try:
        sent = await update.effective_message.reply_text(
            f"{emoji} بازی جدید\n\n"
            f"👤 سازنده: {user.first_name}\n"
            f"💰 مبلغ: {money(amount)} TRX\n\n"
            f"🤖 بازی با ربات: خودت اول رول می‌کنی، بعد ربات.\n"
            f"👥 بازی با دوستان: سازنده اول رول می‌کند، بعد حریف.\n\n"
            f"🏆 برنده: {money(PAYOUT)} TRX",
            reply_markup=keyboard
        )

        con = db()
        con.execute(
            "UPDATE games SET message_id=? WHERE game_id=?",
            (sent.message_id, game_id)
        )
        con.commit()
        con.close()

    except Exception:
        await refund_game(game_id)


# =========================================================
# REFUND
# =========================================================

async def refund_game(game_id):
    async with db_lock:
        con = db()

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute(
                "SELECT * FROM games WHERE game_id=?",
                (game_id,)
            ).fetchone()

            if not game:
                con.rollback()
                return False

            if game["status"] not in (
                "waiting",
                "bot_running",
                "friend_waiting",
                "friend_running"
            ):
                con.rollback()
                return False

            creator_id = game["creator_id"]
            amount = float(game["amount"])

            con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                amount,
                creator_id
            ))

            con.execute("""
                INSERT INTO transactions
                (user_id, amount, type, game_id, created_at)
                VALUES (?, ?, 'game_refund', ?, ?)
            """, (
                creator_id,
                amount,
                game_id,
                now()
            ))

            con.execute("""
                UPDATE games
                SET status='refunded'
                WHERE game_id=?
            """, (game_id,))

            con.commit()
            return True

        except Exception:
            con.rollback()
            logger.exception("refund error")
            return False

        finally:
            con.close()


# =========================================================
# TELEGRAM GAME ROLL
# =========================================================

async def send_roll(bot, chat_id, emoji):
    msg = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji
    )

    return int(msg.dice.value)


# =========================================================
# CALLBACK
# =========================================================

async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data or ""

    if data == "check_join":
        if await is_joined(context.bot, user.id):
            await query.message.reply_text(
                "✅ عضویت تأیید شد.\n/start"
            )
        else:
            await query.message.reply_text(
                "❌ هنوز عضو کانال نیستید."
            )
        return

    # -------------------------------
    # BOT GAME
    # -------------------------------

    if data.startswith("botgame:"):
        game_id = data.split(":", 1)[1]

        con = db()
        game = con.execute(
            "SELECT * FROM games WHERE game_id=?",
            (game_id,)
        ).fetchone()
        con.close()

        if not game:
            await query.message.reply_text(
                "❌ بازی پیدا نشد."
            )
            return

        if game["status"] != "waiting":
            await query.message.reply_text(
                "❌ این بازی قبلاً اجرا شده است."
            )
            return

        if game["creator_id"] != user.id:
            await query.answer(
                "فقط سازنده می‌تواند بازی با ربات را شروع کند.",
                show_alert=True
            )
            return

        con = db()
        con.execute("""
            UPDATE games
            SET status='bot_running'
            WHERE game_id=? AND status='waiting'
        """, (game_id,))
        con.commit()
        con.close()

        try:
            await query.message.edit_reply_markup(None)
        except Exception:
            pass

        try:
            await query.message.reply_text(
                f"{game['game_type']} بازی با ربات شروع شد.\n\n"
                "🎮 اول سازنده رول می‌کند..."
            )

            user_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()
            con.execute("""
                UPDATE games
                SET creator_roll=?
                WHERE game_id=?
            """, (user_roll, game_id))
            con.commit()
            con.close()

            await asyncio.sleep(1)

            await query.message.reply_text(
                "🤖 حالا ربات رول می‌کند..."
            )

            bot_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()
            con.execute("""
                UPDATE games
                SET opponent_roll=?, status='finished'
                WHERE game_id=?
            """, (
                bot_roll,
                game_id
            ))
            con.commit()
            con.close()

            if user_roll > bot_roll:
                ok, new_balance = await change_balance(
                    user.id,
                    PAYOUT,
                    "game_win",
                    game_id
                )

                if not ok:
                    await refund_game(game_id)
                    await query.message.reply_text(
                        "🛡️ خطا در پرداخت؛ مبلغ بازی برگشت داده شد."
                    )
                    return

                result = (
                    f"🏆 برنده: {user.first_name}\n"
                    f"🎲 نتیجه شما: {user_roll}\n"
                    f"🤖 نتیجه ربات: {bot_roll}\n\n"
                    f"💰 جایزه: {money(PAYOUT)} TRX"
                )

            elif user_roll < bot_roll:

                result = (
                    f"🏆 برنده: ربات\n"
                    f"👤 {user.first_name}: {user_roll}\n"
                    f"🤖 ربات: {bot_roll}"
                )

            else:

                await change_balance(
                    user.id,
                    game["amount"],
                    "game_draw_refund",
                    game_id
                )

                result = (
                    "🤝 مساوی شد.\n\n"
                    f"👤 {user.first_name}: {user_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    f"💰 مبلغ بازی برگشت داده شد."
                )

            await query.message.reply_text(result)

        except Exception:
            logger.exception("bot game failed")

            await refund_game(game_id)

            await query.message.reply_text(
                "🛡️ بازی با خطا مواجه شد.\n"
                "💰 مبلغ بازی به موجودی شما برگشت داده شد."
            )

        return

    # -------------------------------
    # FRIEND GAME
    # -------------------------------

    if data.startswith("friendgame:"):
        game_id = data.split(":", 1)[1]

        con = db()
        game = con.execute(
            "SELECT * FROM games WHERE game_id=?",
            (game_id,)
        ).fetchone()
        con.close()

        if not game:
            return

        if game["creator_id"] != user.id:
            await query.answer(
                "فقط سازنده می‌تواند بازی دوستان را باز کند.",
                show_alert=True
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "این بازی دیگر قابل ورود نیست.",
                show_alert=True
            )
            return

        con = db()
        con.execute("""
            UPDATE games
            SET status='friend_waiting'
            WHERE game_id=? AND status='waiting'
        """, (game_id,))
        con.commit()
        con.close()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👥 پیوستن به بازی",
                    callback_data=f"join:{game_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو بازی",
                    callback_data=f"cancel:{game_id}"
                )
            ]
        ])

        try:
            await query.message.edit_reply_markup(
                reply_markup=keyboard
            )
        except Exception:
            pass

        await query.message.reply_text(
            f"👥 بازی دوستان باز شد.\n\n"
            f"💰 مبلغ ورود: {money(game['amount'])} TRX\n"
            f"👤 سازنده: {user.first_name}\n\n"
            "هر کاربری که می‌خواهد بازی کند روی "
            "«پیوستن به بازی» بزند."
        )

        return

    # -------------------------------
    # JOIN FRIEND
    # -------------------------------

    if data.startswith("join:"):
        game_id = data.split(":", 1)[1]

        con = db()
        game = con.execute(
            "SELECT * FROM games WHERE game_id=?",
            (game_id,)
        ).fetchone()
        con.close()

        if not game:
            await query.answer(
                "بازی پیدا نشد.",
                show_alert=True
            )
            return

        if game["status"] != "friend_waiting":
            await query.answer(
                "این بازی دیگر قابل ورود نیست.",
                show_alert=True
            )
            return

        if game["creator_id"] == user.id:
            await query.answer(
                "سازنده نمی‌تواند حریف خودش باشد.",
                show_alert=True
            )
            return

        if not await is_joined(context.bot, user.id):
            await query.answer(
                "ابتدا در کانال عضو شوید.",
                show_alert=True
            )
            return

        ensure_user(user)

        amount = float(game["amount"])

        async with db_lock:
            con = db()

            try:
                con.execute("BEGIN IMMEDIATE")

                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user.id,)
                ).fetchone()

                if not row or float(row["balance"]) < amount:
                    con.rollback()

                    await query.answer(
                        "موجودی کافی ندارید.",
                        show_alert=True
                    )
                    return

                updated = con.execute("""
                    UPDATE games
                    SET opponent_id=?,
                        status='friend_running'
                    WHERE game_id=?
                      AND status='friend_waiting'
                      AND opponent_id IS NULL
                """, (
                    user.id,
                    game_id
                ))

                if updated.rowcount != 1:
                    con.rollback()

                    await query.answer(
                        "یک نفر دیگر وارد بازی شده است.",
                        show_alert=True
                    )
                    return

                con.execute(
                    "UPDATE users SET balance=balance-? WHERE user_id=?",
                    (amount, user.id)
                )

                con.execute("""
                    INSERT INTO transactions
                    (user_id, amount, type, game_id, created_at)
                    VALUES (?, ?, 'game_lock', ?, ?)
                """, (
                    user.id,
                    -amount,
                    game_id,
                    now()
                ))

                con.commit()

            except Exception:
                con.rollback()
                logger.exception("join error")

                await query.answer(
                    "خطا در ورود به بازی.",
                    show_alert=True
                )
                return

            finally:
                con.close()

        try:
            await query.message.reply_text(
                f"👥 بازی شروع شد.\n\n"
                f"👤 سازنده: {game['creator_id']}\n"
                f"👤 حریف: {user.first_name}\n\n"
                "🎮 اول سازنده رول می‌کند..."
            )

            creator_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            await asyncio.sleep(1)

            await query.message.reply_text(
                f"👤 {user.first_name} حالا نوبت حریف است..."
            )

            opponent_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()
            con.execute("""
                UPDATE games
                SET creator_roll=?,
                    opponent_roll=?,
                    status='finished'
                WHERE game_id=?
            """, (
                creator_roll,
                opponent_roll,
                game_id
            ))
            con.commit()
            con.close()

            creator_id = int(game["creator_id"])

            creator_info = await context.bot.get_chat_member(
                game["chat_id"],
                creator_id
            )

            creator_name = (
                creator_info.user.first_name
                if creator_info and creator_info.user
                else "سازنده"
            )

            if creator_roll > opponent_roll:

                await change_balance(
                    creator_id,
                    PAYOUT,
                    "game_win",
                    game_id
                )

                winner_name = creator_name

            elif opponent_roll > creator_roll:

                await change_balance(
                    user.id,
                    PAYOUT,
                    "game_win",
                    game_id
                )

                winner_name = user.first_name

            else:

                await change_balance(
                    creator_id,
                    amount,
                    "game_draw_refund",
                    game_id
                )

                await change_balance(
                    user.id,
                    amount,
                    "game_draw_refund",
                    game_id
                )

                winner_name = "🤝 مساوی"

            if winner_name == "🤝 مساوی":
                text = (
                    "🤝 بازی مساوی شد.\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {user.first_name}: {opponent_roll}\n\n"
                    "💰 مبلغ هر دو نفر برگشت داده شد."
                )
            else:
                text = (
                    "🏆 نتیجه بازی\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {user.first_name}: {opponent_roll}\n\n"
                    f"🏆 برنده: {winner_name}\n"
                    f"💰 جایزه: {money(PAYOUT)} TRX"
                )

            await query.message.reply_text(text)

        except Exception:
            logger.exception("friend game error")

            # برگرداندن پول هر دو نفر در صورت خطا
            async with db_lock:
                con = db()

                try:
                    con.execute("BEGIN IMMEDIATE")

                    # فقط اگر بازی هنوز finished نشده
                    current = con.execute(
                        "SELECT status, opponent_id FROM games WHERE game_id=?",
                        (game_id,)
                    ).fetchone()

                    if current and current["status"] != "finished":
                        con.execute("""
                            UPDATE users
                            SET balance=balance+?
                            WHERE user_id=?
                        """, (
                            amount,
                            creator_id
                        ))

                        con.execute("""
                            UPDATE users
                            SET balance=balance+?
                            WHERE user_id=?
                        """, (
                            amount,
                            user.id
                        ))

                        con.execute("""
                            UPDATE games
                            SET status='refunded'
                            WHERE game_id=?
                        """, (game_id,))

                    con.commit()

                except Exception:
                    con.rollback()

                finally:
                    con.close()

            await query.message.reply_text(
                "🛡️ بازی با خطا مواجه شد.\n"
                "💰 مبالغ بازی برگشت داده شد."
            )

        return

    # -------------------------------
    # CANCEL
    # -------------------------------

    if data.startswith("cancel:"):
        game_id = data.split(":", 1)[1]

        con = db()
        game = con.execute(
            "SELECT * FROM games WHERE game_id=?",
            (game_id,)
        ).fetchone()
        con.close()

        if not game:
            await query.answer(
                "بازی پیدا نشد.",
                show_alert=True
            )
            return

        if game["creator_id"] != user.id and user.id != OWNER_ID:
            await query.answer(
                "فقط سازنده یا مالک می‌تواند لغو کند.",
                show_alert=True
            )
            return

        ok = await refund_game(game_id)

        if ok:
            try:
                await query.message.edit_reply_markup(None)
            except Exception:
                pass

            await query.message.reply_text(
                "❌ بازی لغو شد.\n"
                f"💰 {money(game['amount'])} TRX به سازنده برگشت."
            )
        else:
            await query.answer(
                "این بازی قبلاً بسته شده است.",
                show_alert=True
            )

        return


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin:stats"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin:users"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin:add"
            )
        ],
        [
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin:remove"
            )
        ],
        [
            InlineKeyboardButton(
                "🎮 بازی‌های فعال",
                callback_data="admin:games"
            )
        ],
        [
            InlineKeyboardButton(
                "🛑 بستن بازی‌های گیرکرده",
                callback_data="admin:cleanup"
            )
        ]
    ])

    await update.effective_message.reply_text(
        "👑 پنل مدیریت\n\n"
        "مدیریت کاربران، موجودی و بازی‌ها:",
        reply_markup=keyboard
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    if not is_admin(user.id):
        return

    data = query.data

    if data == "admin:stats":

        con = db()

        users = con.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        balance = con.execute(
            "SELECT COALESCE(SUM(balance),0) AS s FROM users"
        ).fetchone()["s"]

        active_games = con.execute("""
            SELECT COUNT(*) AS c
            FROM games
            WHERE status IN
            ('waiting','friend_waiting','bot_running','friend_running')
        """).fetchone()["c"]

        con.close()

        await query.message.reply_text(
            "📊 آمار ربات\n\n"
            f"👥 کاربران: {users}\n"
            f"💰 مجموع موجودی: {money(balance)} TRX\n"
            f"🎮 بازی فعال: {active_games}"
        )

        return

    if data == "admin:users":

        con = db()

        rows = con.execute("""
            SELECT user_id, first_name, username, balance
            FROM users
            ORDER BY balance DESC
            LIMIT 20
        """).fetchall()

        con.close()

        if not rows:
            await query.message.reply_text(
                "👥 کاربری وجود ندارد."
            )
            return

        text = "👥 کاربران:\n\n"

        for row in rows:
            name = row["first_name"] or "بدون نام"
            text += (
                f"👤 {name}\n"
                f"🆔 {row['user_id']}\n"
                f"💰 {money(row['balance'])} TRX\n\n"
            )

        await query.message.reply_text(text)
        return

    if data == "admin:add":

        await query.message.reply_text(
            "➕ افزایش موجودی\n\n"
            "فرمت:\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 10"
        )
        return

    if data == "admin:remove":

        await query.message.reply_text(
            "➖ کسر موجودی\n\n"
            "فرمت:\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 10"
        )
        return

    if data == "admin:games":

        con = db()

        rows = con.execute("""
            SELECT game_id, creator_id, opponent_id,
                   game_type, amount, status
            FROM games
            WHERE status NOT IN ('finished','refunded')
            ORDER BY created_at DESC
            LIMIT 20
        """).fetchall()

        con.close()

        if not rows:
            await query.message.reply_text(
                "🎮 بازی فعال وجود ندارد."
            )
            return

        text = "🎮 بازی‌های فعال:\n\n"

        for r in rows:
            text += (
                f"🆔 {r['game_id']}\n"
                f"{r['game_type']} | {money(r['amount'])} TRX\n"
                f"سازنده: {r['creator_id']}\n"
                f"حریف: {r['opponent_id']}\n"
                f"وضعیت: {r['status']}\n\n"
            )

        await query.message.reply_text(text)
        return

    if data == "admin:cleanup":

        con = db()

        rows = con.execute("""
            SELECT game_id, creator_id, amount
            FROM games
            WHERE status IN
            ('waiting','friend_waiting','bot_running','friend_running')
        """).fetchall()

        con.close()

        count = 0

        for row in rows:
            if await refund_game(row["game_id"]):
                count += 1

        await query.message.reply_text(
            f"🛡️ پاکسازی انجام شد.\n"
            f"🎮 تعداد بازی بسته‌شده: {count}"
        )

        return


# =========================================================
# ADMIN ADD BALANCE
# =========================================================

async def add_balance_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "فرمت:\n"
            "/addbalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = float(
            normalize_digits(context.args[1]).replace(",", ".")
        )
    except Exception:
        await update.effective_message.reply_text(
            "❌ اطلاعات نامعتبر."
        )
        return

    if amount <= 0:
        await update.effective_message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    con = db()

    exists = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (target_id,)
    ).fetchone()

    con.close()

    if not exists:
        await update.effective_message.reply_text(
            "❌ این کاربر هنوز ربات را Start نکرده است."
        )
        return

    ok, new_balance = await change_balance(
        target_id,
        amount,
        "admin_add"
    )

    if not ok:
        await update.effective_message.reply_text(
            "❌ افزایش موجودی انجام نشد."
        )
        return

    await update.effective_message.reply_text(
        "✅ افزایش موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)} TRX\n"
        f"💰 موجودی جدید: {money(new_balance)} TRX"
    )


# =========================================================
# ADMIN REMOVE BALANCE
# =========================================================

async def remove_balance_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "فرمت:\n"
            "/removebalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = float(
            normalize_digits(context.args[1]).replace(",", ".")
        )
    except Exception:
        await update.effective_message.reply_text(
            "❌ اطلاعات نامعتبر."
        )
        return

    if amount <= 0:
        await update.effective_message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    con = db()

    exists = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (target_id,)
    ).fetchone()

    con.close()

    if not exists:
        await update.effective_message.reply_text(
            "❌ کاربر پیدا نشد."
        )
        return

    ok, new_balance = await change_balance(
        target_id,
        -amount,
        "admin_remove"
    )

    if not ok:
        await update.effective_message.reply_text(
            "❌ موجودی کاربر برای این کسر کافی نیست."
        )
        return

    await update.effective_message.reply_text(
        "✅ کسر موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➖ {money(amount)} TRX\n"
        f"💰 موجودی جدید: {money(new_balance)} TRX"
    )


# =========================================================
# ADD ADMIN
# =========================================================

async def add_admin_command(update, context):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "/addadmin USER_ID"
        )
        return

    try:
        target = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text(
            "❌ آیدی نامعتبر."
        )
        return

    add_admin(target)

    await update.effective_message.reply_text(
        f"✅ کاربر {target} مدیر شد."
    )


# =========================================================
# REMOVE ADMIN
# =========================================================

async def remove_admin_command(update, context):
    user = update.effective_user

    if user.id != OWNER_ID:
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "/removeadmin USER_ID"
        )
        return

    try:
        target = int(context.args[0])
    except Exception:
        await update.effective_message.reply_text(
            "❌ آیدی نامعتبر."
        )
        return

    remove_admin(target)

    await update.effective_message.reply_text(
        f"✅ دسترسی مدیریت {target} حذف شد."
    )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update, context):
    msg = update.effective_message

    if not msg or not msg.text:
        return

    text = normalize_digits(msg.text).strip()

    # موجودی
    if re.fullmatch(
        r"(موجودی|موجودی\s*💰|balance)",
        text,
        re.IGNORECASE
    ):
        await balance_handler(update, context)
        return

    # زیرمجموعه
    if text in (
        "زیرمجموعه",
        "زیر مجموعه",
        "رفرال",
        "referral"
    ):
        await referral(update, context)
        return

    # انتقال
    if re.match(
        r"^انتقال\s+",
        text,
        re.IGNORECASE
    ):
        await transfer_handler(update, context)
        return

    # بازی
    parsed = parse_game(text)

    if parsed:
        emoji, amount = parsed
        await create_game(
            update,
            context,
            emoji,
            amount
        )
        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "Unhandled error",
        exc_info=context.error
    )

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "🛡️ خطایی رخ داد؛ عملیات ناقص انجام نشد."
            )
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -------------------------------
    # Commands
    # -------------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler(
            "addbalance",
            add_balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "removebalance",
            remove_balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addadmin",
            add_admin_command
        )
    )

    application.add_handler(
        CommandHandler(
            "removeadmin",
            remove_admin_command
        )
    )

    # -------------------------------
    # Callbacks
    # -------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # -------------------------------
    # Persian / English text
    # -------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info("BET_BT started successfully.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
