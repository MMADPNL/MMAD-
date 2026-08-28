# ============================================================
# BET_BTBOT
# Telegram Group Games
# Python 3.10+
# python-telegram-bot 20+
# ============================================================

import os
import sqlite3
import uuid
import time
import logging
from decimal import Decimal, InvalidOperation

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

BOT_USERNAME = "@BET_BTBOT"
FORCE_CHANNEL = "@zobxt"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# اگر OWNER_ID را در Secrets گذاشتی از همان استفاده می‌شود.
# مقدار پیش‌فرض فقط برای مالک اصلی پروژه است.
OWNER_ID = int(os.getenv("OWNER_ID", "8552447077"))

DATABASE = "bet_bot.db"

GAME_TIMEOUT = 300

# جایزه مجازی
WIN_PRIZE = Decimal("0.19")

# پاداش زیرمجموعه
REF_REWARD = Decimal("0.05")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("BET_BTBOT")


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    db = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_database():
    db = db_connect()

    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        name TEXT DEFAULT '',
        balance TEXT DEFAULT '0',
        referrer INTEGER,
        ref_paid INTEGER DEFAULT 0,
        blocked INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        message_id INTEGER,
        creator_id INTEGER NOT NULL,
        opponent_id INTEGER,
        game TEXT NOT NULL,
        emoji TEXT NOT NULL,
        amount TEXT NOT NULL,
        mode TEXT DEFAULT '',
        creator_roll INTEGER,
        opponent_roll INTEGER,
        status TEXT NOT NULL,
        created_at INTEGER,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        amount TEXT NOT NULL,
        UNIQUE(game_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        game_id TEXT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS referrals (
        user_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        reward TEXT NOT NULL,
        created_at INTEGER
    );
    """)

    db.commit()
    db.close()


# ============================================================
# HELPERS
# ============================================================

def normalize(text):
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫",
        "01234567890123456789."
    )

    return str(text).translate(table).strip().lower()


def decimal_value(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def display_name(user):
    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def saved_name(user_id):
    db = db_connect()

    row = db.execute(
        """
        SELECT name, username
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    db.close()

    if not row:
        return str(user_id)

    if row["name"]:
        return row["name"]

    if row["username"]:
        return "@" + row["username"]

    return str(user_id)


def save_user(user, referrer=None):
    db = db_connect()

    old = db.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if old:
        db.execute(
            """
            UPDATE users
            SET username=?, name=?
            WHERE user_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id
            )
        )
    else:
        valid_ref = None

        if referrer:
            try:
                rid = int(str(referrer).replace("ref_", ""))

                if rid != user.id:
                    exists = db.execute(
                        """
                        SELECT user_id
                        FROM users
                        WHERE user_id=?
                        """,
                        (rid,)
                    ).fetchone()

                    if exists:
                        valid_ref = rid
            except Exception:
                pass

        db.execute(
            """
            INSERT INTO users
            (
                user_id,
                username,
                name,
                balance,
                referrer,
                ref_paid,
                blocked,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                "0",
                valid_ref,
                0,
                0,
                int(time.time())
            )
        )

    db.commit()
    db.close()


def change_balance(db, user_id, amount):
    row = db.execute(
        """
        SELECT balance
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not row:
        raise Exception("USER_NOT_FOUND")

    current = decimal_value(row["balance"])
    new_balance = current + decimal_value(amount)

    if new_balance < 0:
        raise Exception("INSUFFICIENT_BALANCE")

    db.execute(
        """
        UPDATE users
        SET balance=?
        WHERE user_id=?
        """,
        (
            str(new_balance),
            user_id
        )
    )


# ============================================================
# FORCE JOIN
# ============================================================

def join_keyboard():
    return InlineKeyboardMarkup([
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


async def check_joined(bot, user_id):
    try:
        member = await bot.get_chat_member(
            FORCE_CHANNEL,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception:
        return False


async def require_join(update, context):
    user = update.effective_user

    if not user:
        return False

    if OWNER_ID and user.id == OWNER_ID:
        return True

    joined = await check_joined(
        context.bot,
        user.id
    )

    if joined:
        return True

    text = (
        "🔒 برای استفاده از ربات ابتدا باید عضو "
        "@zobxt شوید."
    )

    if update.callback_query:
        await update.callback_query.answer(
            "ابتدا عضو @zobxt شوید.",
            show_alert=True
        )

        try:
            await update.callback_query.message.reply_text(
                text,
                reply_markup=join_keyboard()
            )
        except Exception:
            pass

    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=join_keyboard()
        )

    return False


# ============================================================
# GAME PARSER
# ============================================================

GAME_TYPES = {
    "تاس": ("dice", "🎲"),
    "تاس": ("dice", "🎲"),

    "بولینگ": ("bowling", "🎳"),

    "دارت": ("darts", "🎯"),

    "بسکتبال": ("basketball", "🏀"),

    "dice": ("dice", "🎲"),
    "bowling": ("bowling", "🎳"),
    "darts": ("darts", "🎯"),
    "basketball": ("basketball", "🏀"),
}


def parse_game(text):
    parts = normalize(text).split()

    if len(parts) != 3:
        return None

    if parts[0] != "1":
        return None

    game_name = parts[1]

    if game_name not in GAME_TYPES:
        return None

    try:
        amount = Decimal(parts[2])
    except InvalidOperation:
        return None

    if amount <= 0:
        return None

    game, emoji = GAME_TYPES[game_name]

    return game, emoji, amount


# ============================================================
# TRANSACTION LOCK
# ============================================================

def lock_user_money(game_id, user_id, amount):
    db = db_connect()

    try:
        db.execute("BEGIN IMMEDIATE")

        exists = db.execute(
            """
            SELECT id
            FROM locks
            WHERE game_id=? AND user_id=?
            """,
            (
                game_id,
                user_id
            )
        ).fetchone()

        if exists:
            db.rollback()
            return False

        user = db.execute(
            """
            SELECT balance, blocked
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            db.rollback()
            return False

        if user["blocked"]:
            db.rollback()
            return False

        if decimal_value(user["balance"]) < amount:
            db.rollback()
            return False

        # قفل اتمیک موجودی
        change_balance(
            db,
            user_id,
            -amount
        )

        db.execute(
            """
            INSERT INTO locks
            (game_id,user_id,amount)
            VALUES(?,?,?)
            """,
            (
                game_id,
                user_id,
                str(amount)
            )
        )

        db.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                user_id,
                "LOCK",
                str(amount),
                int(time.time())
            )
        )

        db.commit()
        return True

    except Exception:
        db.rollback()
        logger.exception("LOCK ERROR")
        return False

    finally:
        db.close()


# ============================================================
# REFUND
# ============================================================

def refund_game(game_id):
    db = db_connect()

    try:
        db.execute("BEGIN IMMEDIATE")

        game = db.execute(
            """
            SELECT status
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            db.rollback()
            return False

        locks = db.execute(
            """
            SELECT *
            FROM locks
            WHERE game_id=?
            """,
            (game_id,)
        ).fetchall()

        for lock in locks:

            already = db.execute(
                """
                SELECT id
                FROM transactions
                WHERE game_id=?
                AND user_id=?
                AND kind='REFUND'
                """,
                (
                    game_id,
                    lock["user_id"]
                )
            ).fetchone()

            if already:
                continue

            change_balance(
                db,
                lock["user_id"],
                decimal_value(lock["amount"])
            )

            db.execute(
                """
                INSERT INTO transactions
                (
                    id,
                    game_id,
                    user_id,
                    kind,
                    amount,
                    created_at
                )
                VALUES(?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    lock["user_id"],
                    "REFUND",
                    lock["amount"],
                    int(time.time())
                )
            )

        db.execute(
            """
            DELETE FROM locks
            WHERE game_id=?
            """,
            (game_id,)
        )

        db.execute(
            """
            UPDATE games
            SET status='REFUNDED',
                updated_at=?
            WHERE id=?
            """,
            (
                int(time.time()),
                game_id
            )
        )

        db.commit()
        return True

    except Exception:
        db.rollback()
        logger.exception("REFUND ERROR")
        return False

    finally:
        db.close()


# ============================================================
# PAY WINNER
# ============================================================

def pay_winner(game_id, winner_id):
    db = db_connect()

    try:
        db.execute("BEGIN IMMEDIATE")

        game = db.execute(
            """
            SELECT status
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            db.rollback()
            return False

        if game["status"] in (
            "FINISHED",
            "REFUNDED",
            "CANCELLED"
        ):
            db.rollback()
            return False

        # ضد پرداخت دوباره
        already = db.execute(
            """
            SELECT id
            FROM transactions
            WHERE game_id=?
            AND kind='PRIZE'
            """,
            (game_id,)
        ).fetchone()

        if already:
            db.rollback()
            return False

        change_balance(
            db,
            winner_id,
            WIN_PRIZE
        )

        db.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                winner_id,
                "PRIZE",
                str(WIN_PRIZE),
                int(time.time())
            )
        )

        # مبلغ‌های قفل‌شده دیگر خرج نمی‌شوند
        db.execute(
            """
            DELETE FROM locks
            WHERE game_id=?
            """,
            (game_id,)
        )

        db.execute(
            """
            UPDATE games
            SET status='FINISHED',
                updated_at=?
            WHERE id=?
            """,
            (
                int(time.time()),
                game_id
            )
        )

        db.commit()
        return True

    except Exception:
        db.rollback()
        logger.exception("PRIZE ERROR")
        return False

    finally:
        db.close()


# ============================================================
# DRAW REFUND
# ============================================================

def draw_refund(game_id):
    return refund_game(game_id)


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, game, emoji, amount):

    user = update.effective_user
    chat = update.effective_chat

    game_id = uuid.uuid4().hex

    save_user(user)

    # قفل مبلغ سازنده
    if not lock_user_money(
        game_id,
        user.id,
        amount
    ):
        await update.effective_message.reply_text(
            "❌ موجودی مجازی کافی نیست."
        )
        return

    now = int(time.time())

    db = db_connect()

    db.execute(
        """
        INSERT INTO games
        (
            id,
            chat_id,
            creator_id,
            game,
            emoji,
            amount,
            mode,
            status,
            created_at,
            updated_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            game_id,
            chat.id,
            user.id,
            game,
            emoji,
            str(amount),
            "",
            "MODE_SELECT",
            now,
            now
        )
    )

    db.commit()
    db.close()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_game:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend_game:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel_game:{game_id}"
            )
        ]
    ])

    try:
        msg = await update.effective_message.reply_text(
            f"{emoji} بازی جدید\n\n"
            f"👤 سازنده: {display_name(user)}\n"
            f"🎮 نوع بازی: {game}\n\n"
            f"🤖 برای بازی با ربات بزنید.\n"
            f"👥 برای بازی با دوست بزنید.",
            reply_markup=keyboard
        )

        db = db_connect()

        db.execute(
            """
            UPDATE games
            SET message_id=?
            WHERE id=?
            """,
            (
                msg.message_id,
                game_id
            )
        )

        db.commit()
        db.close()

    except Exception:
        logger.exception("CREATE GAME MESSAGE ERROR")
        refund_game(game_id)


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(query, context, game):

    user = query.from_user

    if user.id != game["creator_id"]:
        await query.answer(
            "فقط سازنده بازی می‌تواند.",
            show_alert=True
        )
        return

    db = db_connect()

    changed = db.execute(
        """
        UPDATE games
        SET mode='BOT',
            status='CREATOR_ROLL',
            updated_at=?
        WHERE id=?
        AND creator_id=?
        AND status='MODE_SELECT'
        """,
        (
            int(time.time()),
            game["id"],
            user.id
        )
    ).rowcount

    db.commit()
    db.close()

    if changed != 1:
        await query.answer(
            "این بازی دیگر فعال نیست.",
            show_alert=True
        )
        return

    await query.answer()

    try:

        await query.edit_message_text(
            "🤖 بازی با ربات شروع شد.\n\n"
            "👤 اول شما رول می‌کنید..."
        )

        # اول کاربر
        user_roll_message = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        user_roll = user_roll_message.dice.value

        db = db_connect()

        db.execute(
            """
            UPDATE games
            SET creator_roll=?,
                status='BOT_ROLL',
                updated_at=?
            WHERE id=?
            """,
            (
                user_roll,
                int(time.time()),
                game["id"]
            )
        )

        db.commit()
        db.close()

        await context.bot.send_message(
            game["chat_id"],
            "🤖 حالا ربات رول می‌کند..."
        )

        # بعد ربات
        bot_roll_message = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        bot_roll = bot_roll_message.dice.value

        db = db_connect()

        db.execute(
            """
            UPDATE games
            SET opponent_roll=?,
                status='SETTLING',
                updated_at=?
            WHERE id=?
            """,
            (
                bot_roll,
                int(time.time()),
                game["id"]
            )
        )

        db.commit()
        db.close()

        player_name = display_name(user)

        if user_roll > bot_roll:

            winner = user.id

            result = (
                f"🏆 برنده: {player_name}"
            )

            paid = pay_winner(
                game["id"],
                winner
            )

        elif bot_roll > user_roll:

            winner = None

            result = "🏆 برنده: 🤖 ربات"

            # چون ربات موجودی واقعی ندارد،
            # مبلغ بازی برگشت داده می‌شود.
            paid = draw_refund(game["id"])

        else:

            winner = None

            result = "🤝 بازی مساوی شد"

            paid = draw_refund(game["id"])

        if not paid:
            refund_game(game["id"])

            await context.bot.send_message(
                game["chat_id"],
                "🛡️ خطا در تسویه؛ مبلغ مجازی برگشت داده شد."
            )

            return

        if winner:

            prize = (
                "\n💰 جایزه: ۰٫۱۹ TRX مجازی"
            )

        else:

            prize = (
                "\n💰 مبلغ بازی برگشت داده شد."
            )

        await context.bot.send_message(
            game["chat_id"],
            f"🎮 نتیجه {game['emoji']}\n\n"
            f"👤 {player_name}: {user_roll}\n"
            f"🤖 ربات: {bot_roll}\n\n"
            f"{result}"
            f"{prize}"
        )

    except Exception:
        logger.exception("BOT GAME ERROR")

        refund_game(game["id"])

        try:
            await context.bot.send_message(
                game["chat_id"],
                "🛡️ بازی با خطا مواجه شد؛ مبلغ مجازی برگشت داده شد."
            )
        except Exception:
            pass


# ============================================================
# FRIEND GAME SELECT
# ============================================================

async def friend_game(query):

    user = query.from_user
    game_id = query.data.split(":", 1)[1]

    db = db_connect()

    changed = db.execute(
        """
        UPDATE games
        SET mode='FRIEND',
            status='WAITING_OPPONENT',
            updated_at=?
        WHERE id=?
        AND creator_id=?
        AND status='MODE_SELECT'
        """,
        (
            int(time.time()),
            game_id,
            user.id
        )
    ).rowcount

    db.commit()

    game = db.execute(
        """
        SELECT *
        FROM games
        WHERE id=?
        """,
        (game_id,)
    ).fetchone()

    db.close()

    if changed != 1:
        await query.answer(
            "این بازی دیگر فعال نیست.",
            show_alert=True
        )
        return

    await query.answer()

    await query.edit_message_text(
        f"👥 بازی با دوستان\n\n"
        f"👤 سازنده: {display_name(user)}\n\n"
        f"منتظر ورود حریف...",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👥 ورود به بازی",
                    callback_data=f"join_game:{game_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"cancel_game:{game_id}"
                )
            ]
        ])
    )


# ============================================================
# JOIN FRIEND GAME
# ============================================================

async def join_game(query, context, game):

    user = query.from_user

    if user.id == game["creator_id"]:
        await query.answer(
            "سازنده نمی‌تواند حریف خودش باشد.",
            show_alert=True
        )
        return

    save_user(user)

    amount = decimal_value(
        game["amount"]
    )

    # اول مبلغ حریف را موقت قفل می‌کنیم
    temporary_lock = (
        game["id"]
        + ":JOIN:"
        + str(user.id)
    )

    if not lock_user_money(
        temporary_lock,
        user.id,
        amount
    ):
        await query.answer(
            "❌ موجودی مجازی کافی نیست.",
            show_alert=True
        )
        return

    db = db_connect()

    try:

        db.execute("BEGIN IMMEDIATE")

        current = db.execute(
            """
            SELECT status,opponent_id
            FROM games
            WHERE id=?
            """,
            (game["id"],)
        ).fetchone()

        if (
            not current
            or current["status"] != "WAITING_OPPONENT"
            or current["opponent_id"] is not None
        ):
            db.rollback()
            raise Exception("GAME_ALREADY_TAKEN")

        lock = db.execute(
            """
            SELECT amount
            FROM locks
            WHERE game_id=?
            AND user_id=?
            """,
            (
                temporary_lock,
                user.id
            )
        ).fetchone()

        if not lock:
            db.rollback()
            raise Exception("LOCK_NOT_FOUND")

        db.execute(
            """
            DELETE FROM locks
            WHERE game_id=?
            AND user_id=?
            """,
            (
                temporary_lock,
                user.id
            )
        )

        db.execute(
            """
            INSERT INTO locks
            (game_id,user_id,amount)
            VALUES(?,?,?)
            """,
            (
                game["id"],
                user.id,
                lock["amount"]
            )
        )

        db.execute(
            """
            UPDATE games
            SET opponent_id=?,
                status='CREATOR_ROLL',
                updated_at=?
            WHERE id=?
            """,
            (
                user.id,
                int(time.time()),
                game["id"]
            )
        )

        db.commit()

    except Exception as e:

        try:
            db.rollback()
        except Exception:
            pass

        db.close()

        # برگشت قفل موقت
        refund_game(temporary_lock)

        if str(e) == "GAME_ALREADY_TAKEN":
            await query.answer(
                "این بازی قبلاً گرفته شده.",
                show_alert=True
            )
        else:
            await query.answer(
                "خطا در ورود به بازی.",
                show_alert=True
            )

        return

    finally:
        try:
            db.close()
        except Exception:
            pass

    await query.answer(
        "وارد بازی شدی ✅"
    )

    try:

        creator_name = saved_name(
            game["creator_id"]
        )

        opponent_name = display_name(
            user
        )

        await query.edit_message_text(
            "🎮 بازی شروع شد.\n\n"
            f"👤 سازنده: {creator_name}\n"
            f"👤 حریف: {opponent_name}\n\n"
            "نوبت سازنده است..."
        )

        # سازنده اول رول می‌کند
        creator_roll_message = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        creator_roll = creator_roll_message.dice.value

        db = db_connect()

        db.execute(
            """
            UPDATE games
            SET creator_roll=?,
                status='OPPONENT_ROLL',
                updated_at=?
            WHERE id=?
            """,
            (
                creator_roll,
                int(time.time()),
                game["id"]
            )
        )

        db.commit()
        db.close()

        await context.bot.send_message(
            game["chat_id"],
            f"👤 {opponent_name}، حالا نوبت شماست..."
        )

        # حریف دوم رول می‌کند
        opponent_roll_message = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        opponent_roll = opponent_roll_message.dice.value

        db = db_connect()

        db.execute(
            """
            UPDATE games
            SET opponent_roll=?,
                status='SETTLING',
                updated_at=?
            WHERE id=?
            """,
            (
                opponent_roll,
                int(time.time()),
                game["id"]
            )
        )

        db.commit()
        db.close()

        if creator_roll > opponent_roll:

            winner_id = game["creator_id"]

            result = (
                f"🏆 برنده: {creator_name}"
            )

        elif opponent_roll > creator_roll:

            winner_id = user.id

            result = (
                f"🏆 برنده: {opponent_name}"
            )

        else:

            winner_id = None

            result = "🤝 بازی مساوی شد"

        if winner_id:

            ok = pay_winner(
                game["id"],
                winner_id
            )

        else:

            ok = draw_refund(
                game["id"]
            )

        if not ok:

            refund_game(
                game["id"]
            )

            await context.bot.send_message(
                game["chat_id"],
                "🛡️ خطا در تسویه؛ مبلغ مجازی برگشت داده شد."
            )

            return

        if winner_id:

            prize = (
                "\n💰 جایزه برنده: ۰٫۱۹ TRX مجازی"
            )

        else:

            prize = (
                "\n💰 بازی مساوی شد؛ مبلغ برگشت داده شد."
            )

        await context.bot.send_message(
            game["chat_id"],
            f"🎮 نتیجه {game['emoji']}\n\n"
            f"👤 {creator_name}: {creator_roll}\n"
            f"👤 {opponent_name}: {opponent_roll}\n\n"
            f"{result}"
            f"{prize}"
        )

    except Exception:

        logger.exception(
            "FRIEND GAME ERROR"
        )

        refund_game(
            game["id"]
        )

        try:
            await context.bot.send_message(
                game["chat_id"],
                "🛡️ بازی با خطا مواجه شد؛ مبلغ مجازی برگشت داده شد."
            )
        except Exception:
            pass


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(query):

    user = query.from_user
    game_id = query.data.split(":", 1)[1]

    db = db_connect()

    game = db.execute(
        """
        SELECT *
        FROM games
        WHERE id=?
        """,
        (game_id,)
    ).fetchone()

    db.close()

    if not game:
        await query.answer(
            "بازی پیدا نشد.",
            show_alert=True
        )
        return

    if user.id != game["creator_id"]:
        await query.answer(
            "فقط سازنده می‌تواند بازی را لغو کند.",
            show_alert=True
        )
        return

    if game["status"] in (
        "FINISHED",
        "REFUNDED",
        "CANCELLED"
    ):
        await query.answer(
            "بازی قبلاً بسته شده.",
            show_alert=True
        )
        return

    refund_game(game_id)

    db = db_connect()

    db.execute(
        """
        UPDATE games
        SET status='CANCELLED',
            updated_at=?
        WHERE id=?
        """,
        (
            int(time.time()),
            game_id
        )
    )

    db.commit()
    db.close()

    await query.answer(
        "بازی لغو شد."
    )

    try:
        await query.edit_message_text(
            "❌ بازی لغو شد.\n"
            "🛡️ مبلغ مجازی برگشت داده شد."
        )
    except Exception:
        pass


# ============================================================
# TRANSFER BY REPLY
# ============================================================

def parse_transfer(text):

    parts = normalize(text).split()

    if len(parts) != 2:
        return None

    if parts[0] not in (
        "انتقال",
        "ارسال",
        "transfer"
    ):
        return None

    try:
        amount = Decimal(parts[1])
    except InvalidOperation:
        return None

    if amount <= 0:
        return None

    return amount


async def transfer_by_reply(update, context):

    message = update.effective_message

    if not message:
        return False

    if not message.reply_to_message:
        return False

    amount = parse_transfer(
        message.text
    )

    if amount is None:
        return False

    if not await require_join(
        update,
        context
    ):
        return True

    sender = update.effective_user
    receiver = message.reply_to_message.from_user

    if not receiver:
        await message.reply_text(
            "❌ گیرنده پیدا نشد."
        )
        return True

    if receiver.is_bot:
        await message.reply_text(
            "❌ نمی‌توان به ربات انتقال داد."
        )
        return True

    if sender.id == receiver.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return True

    save_user(receiver)

    db = db_connect()

    try:

        db.execute("BEGIN IMMEDIATE")

        sender_row = db.execute(
            """
            SELECT balance,blocked
            FROM users
            WHERE user_id=?
            """,
            (sender.id,)
        ).fetchone()

        receiver_row = db.execute(
            """
            SELECT blocked
            FROM users
            WHERE user_id=?
            """,
            (receiver.id,)
        ).fetchone()

        if not sender_row or not receiver_row:
            raise Exception("USER_NOT_FOUND")

        if sender_row["blocked"]:
            raise Exception("SENDER_BLOCKED")

        if receiver_row["blocked"]:
            raise Exception("RECEIVER_BLOCKED")

        if decimal_value(
            sender_row["balance"]
        ) < amount:
            raise Exception("INSUFFICIENT")

        # انتقال اتمیک
        change_balance(
            db,
            sender.id,
            -amount
        )

        change_balance(
            db,
            receiver.id,
            amount
        )

        db.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex,
                None,
                sender.id,
                "TRANSFER_OUT",
                str(amount),
                int(time.time())
            )
        )

        db.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex,
                None,
                receiver.id,
                "TRANSFER_IN",
                str(amount),
                int(time.time())
            )
        )

        db.commit()

        # عمداً مبلغ نمایش داده نمی‌شود
        await message.reply_text(
            "✅ انتقال انجام شد."
        )

    except Exception as e:

        db.rollback()

        if str(e) == "INSUFFICIENT":
            text = "❌ موجودی مجازی کافی نیست."
        elif str(e) in (
            "SENDER_BLOCKED",
            "RECEIVER_BLOCKED"
        ):
            text = "❌ این حساب امکان انتقال ندارد."
        else:
            text = "❌ انتقال انجام نشد."

        await message.reply_text(
            text
        )

    finally:
        db.close()

    return True


# ============================================================
# BALANCE
# ============================================================

async def balance_command(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    # طبق درخواست:
    # عدد موجودی در گپ نمایش داده نمی‌شود.
    await update.effective_message.reply_text(
        "💰 موجودی مجازی TRX شما فعال است."
    )


# ============================================================
# REFERRAL
# ============================================================

def process_referral(user_id):

    db = db_connect()

    try:

        db.execute("BEGIN IMMEDIATE")

        user = db.execute(
            """
            SELECT referrer,ref_paid
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            db.rollback()
            return

        if not user["referrer"]:
            db.rollback()
            return

        if user["ref_paid"]:
            db.rollback()
            return

        referrer = db.execute(
            """
            SELECT user_id
            FROM users
            WHERE user_id=?
            """,
            (user["referrer"],)
        ).fetchone()

        if not referrer:
            db.rollback()
            return

        # فقط یک بار پاداش
        existing = db.execute(
            """
            SELECT user_id
            FROM referrals
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if existing:
            db.rollback()
            return

        change_balance(
            db,
            user["referrer"],
            REF_REWARD
        )

        db.execute(
            """
            INSERT INTO referrals
            (
                user_id,
                referrer_id,
                reward,
                created_at
            )
            VALUES(?,?,?,?)
            """,
            (
                user_id,
                user["referrer"],
                str(REF_REWARD),
                int(time.time())
            )
        )

        db.execute(
            """
            UPDATE users
            SET ref_paid=1
            WHERE user_id=?
            """,
            (user_id,)
        )

        db.commit()

    except Exception:
        db.rollback()
        logger.exception(
            "REFERRAL ERROR"
        )

    finally:
        db.close()


# ============================================================
# START
# ============================================================

async def start_command(update, context):

    user = update.effective_user

    referrer = None

    if context.args:
        referrer = context.args[0]

    save_user(
        user,
        referrer
    )

    if not await require_join(
        update,
        context
    ):
        return

    process_referral(
        user.id
    )

    await update.effective_message.reply_text(
        "🎮 BET_BTBOT آماده است.\n\n"
        "دستورات بازی در گپ:\n\n"
        "🎲 1 تاس 0.1\n"
        "🎳 1 بولینگ 0.1\n"
        "🎯 1 دارت 0.1\n"
        "🏀 1 بسکتبال 0.1\n\n"
        "🤖 اول کاربر رول می‌کند، بعد ربات.\n"
        "👥 در بازی دوستان اول سازنده، بعد حریف.\n\n"
        "💸 انتقال با Reply:\n"
        "انتقال 0.1\n\n"
        "💰 موجودی TRX مجازی است."
    )


# ============================================================
# OWNER PANEL
# ============================================================

async def owner_panel(update, context):

    if update.effective_user.id != OWNER_ID:
        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    db = db_connect()

    users = db.execute(
        "SELECT COUNT(*) AS n FROM users"
    ).fetchone()["n"]

    games = db.execute(
        "SELECT COUNT(*) AS n FROM games"
    ).fetchone()["n"]

    active = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM games
        WHERE status NOT IN
        ('FINISHED','REFUNDED','CANCELLED')
        """
    ).fetchone()["n"]

    referrals = db.execute(
        "SELECT COUNT(*) AS n FROM referrals"
    ).fetchone()["n"]

    blocked = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM users
        WHERE blocked=1
        """
    ).fetchone()["n"]

    db.close()

    await update.effective_message.reply_text(
        "👑 پنل مدیریت\n\n"
        f"👥 کاربران: {users}\n"
        f"🎮 کل بازی‌ها: {games}\n"
        f"🟢 بازی‌های فعال: {active}\n"
        f"👥 زیرمجموعه‌های ثبت‌شده: {referrals}\n"
        f"🚫 کاربران مسدود: {blocked}"
    )


# ============================================================
# CALLBACK
# ============================================================

async def callback_handler(update, context):

    query = update.callback_query

    if query.data == "check_join":

        if await check_joined(
            context.bot,
            query.from_user.id
        ):
            await query.answer(
                "عضویت تأیید شد ✅",
                show_alert=True
            )
        else:
            await query.answer(
                "هنوز عضو @zobxt نیستید.",
                show_alert=True
            )

        return

    if not await require_join(
        update,
        context
    ):
        return

    parts = query.data.split(":", 1)

    if len(parts) != 2:
        return

    action = parts[0]
    game_id = parts[1]

    db = db_connect()

    game = db.execute(
        """
        SELECT *
        FROM games
        WHERE id=?
        """,
        (game_id,)
    ).fetchone()

    db.close()

    if not game:
        await query.answer(
            "بازی پیدا نشد.",
            show_alert=True
        )
        return

    if action == "bot_game":

        await bot_game(
            query,
            context,
            game
        )

    elif action == "friend_game":

        await friend_game(
            query
        )

    elif action == "join_game":

        await join_game(
            query,
            context,
            game
        )

    elif action == "cancel_game":

        await cancel_game(
            query
        )


# ============================================================
# CLEANUP STUCK GAMES
# ============================================================

async def cleanup_games(context):

    cutoff = int(time.time()) - GAME_TIMEOUT

    db = db_connect()

    stuck = db.execute(
        """
        SELECT id
        FROM games
        WHERE updated_at<?
        AND status NOT IN
        ('FINISHED','REFUNDED','CANCELLED')
        """,
        (cutoff,)
    ).fetchall()

    db.close()

    for row in stuck:

        game_id = row["id"]

        logger.warning(
            "Refunding stuck game %s",
            game_id
        )

        refund_game(
            game_id
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.effective_message

    if not message:
        return

    user = update.effective_user

    if not user:
        return

    save_user(user)

    # انتقال Reply
    if message.reply_to_message:

        handled = await transfer_by_reply(
            update,
            context
        )

        if handled:
            return

    # بازی فقط در گپ
    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        message.text
    )

    if not parsed:
        return

    if not await require_join(
        update,
        context
    ):
        return

    game, emoji, amount = parsed

    await create_game(
        update,
        context,
        game,
        emoji,
        amount
    )


# ============================================================
# ERRORS
# ============================================================

async def error_handler(update, context):

    logger.error(
        "UNHANDLED ERROR",
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

    init_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "panel",
            owner_panel
        )
    )

    application.add_handler(
        CommandHandler(
            "موجودی",
            balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "ترونی",
            balance_command
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
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

    if application.job_queue:

        application.job_queue.run_repeating(
            cleanup_games,
            interval=60,
            first=60
        )

    logger.info(
        "%s is running...",
        BOT_USERNAME
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
