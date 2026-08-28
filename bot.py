# ============================================================
# BOT.PY - TRX-DENOMINATED INTERNAL GAME CREDIT BOT
# Python 3.10+
# python-telegram-bot 20+
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
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
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    8552447077
}

DB_FILE = "bot.db"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")

WITHDRAW_ENABLED_DEFAULT = 1

JOIN_REQUIRED = True
JOIN_CHANNEL = "@zobxt"
JOIN_URL = "https://t.me/zobxt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

DB_LOCK = asyncio.Lock()

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

GAME_EMOJIS = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯",
}

# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
        isolation_level=None,
        check_same_thread=False
    )
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:

        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance TEXT DEFAULT '0',
            blocked INTEGER DEFAULT 0,
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
            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER,
            game_type TEXT NOT NULL,
            amount TEXT NOT NULL,
            rounds INTEGER NOT NULL,
            creator_rolls TEXT DEFAULT '',
            opponent_rolls TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            winner_id INTEGER,
            settled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS house (
            id INTEGER PRIMARY KEY CHECK(id=1),
            owner_balance TEXT DEFAULT '0',
            fee_balance TEXT DEFAULT '0'
        )
        """)

        con.execute("""
        INSERT OR IGNORE INTO house
        (id, owner_balance, fee_balance)
        VALUES (1, '0', '0')
        """)

        con.execute("""
        INSERT OR IGNORE INTO settings
        (key, value)
        VALUES ('withdraw_enabled', '1')
        """)

        # ----------------------------------------------------
        # Migration
        # ----------------------------------------------------

        game_columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        ]

        if "settled" not in game_columns:
            con.execute("""
            ALTER TABLE games
            ADD COLUMN settled INTEGER DEFAULT 0
            """)

        user_columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        ]

        if "balance" not in user_columns:
            con.execute("""
            ALTER TABLE users
            ADD COLUMN balance TEXT DEFAULT '0'
            """)

        con.commit()


# ============================================================
# DECIMAL HELPERS
# ============================================================

def D(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value):
    value = D(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )

    if value == value.to_integral():
        return f"{int(value):,}"

    return f"{value:,.2f}".rstrip("0").rstrip(".")


def normalize_digits(text):
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return text.translate(table)


def parse_decimal_amount(text):
    if not text:
        return None

    text = normalize_digits(str(text))
    text = text.replace("٬", "")
    text = text.replace(",", ".")
    text = text.strip()

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if value < MIN_GAME:
        return None

    if value > MAX_GAME:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


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
        """, (
            user.id,
        )).fetchone()

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

            # بدون موجودی اولیه
            con.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance)
            VALUES (?, ?, ?, '0')
            """, (
                user.id,
                user.username or "",
                user.first_name or ""
            ))

        con.commit()


def get_user(user_id):
    with closing(db()) as con:
        return con.execute("""
        SELECT *
        FROM users
        WHERE user_id=?
        """, (
            user_id,
        )).fetchone()


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return Decimal("0")

    return D(row["balance"])


def is_blocked(user_id):
    row = get_user(user_id)

    return bool(
        row and
        int(row["blocked"]) == 1
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


def name_of_user_id(user_id):
    row = get_user(user_id)

    if not row:
        return str(user_id)

    if row["first_name"]:
        return row["first_name"]

    if row["username"]:
        return "@" + row["username"]

    return str(user_id)


def name_of(user):
    if not user:
        return "کاربر"

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


# ============================================================
# BALANCE - ATOMIC
# ============================================================

def debit_balance(user_id, amount):
    amount = D(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                user_id,
            )).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            current = D(row["balance"])

            if current < amount:
                con.execute("ROLLBACK")
                return False

            new_balance = current - amount

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(new_balance),
                user_id
            ))

            con.execute("COMMIT")

            return True

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("DEBIT ERROR")

            return False


def credit_balance(user_id, amount):
    amount = D(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                user_id,
            )).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            current = D(row["balance"])

            new_balance = current + amount

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(new_balance),
                user_id
            ))

            con.execute("COMMIT")

            return True

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("CREDIT ERROR")

            return False


def set_balance(user_id, amount):
    amount = max(
        Decimal("0"),
        D(amount)
    )

    with closing(db()) as con:

        row = con.execute("""
        SELECT user_id
        FROM users
        WHERE user_id=?
        """, (
            user_id,
        )).fetchone()

        if not row:
            return False

        con.execute("""
        UPDATE users
        SET balance=?
        WHERE user_id=?
        """, (
            str(amount),
            user_id
        ))

        con.commit()

    return True


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount, fee_amount):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT owner_balance, fee_balance
            FROM house
            WHERE id=1
            """).fetchone()

            owner = D(row["owner_balance"])
            fee = D(row["fee_balance"])

            con.execute("""
            UPDATE house
            SET owner_balance=?,
                fee_balance=?
            WHERE id=1
            """, (
                str(owner + owner_amount),
                str(fee + fee_amount)
            ))

            con.execute("COMMIT")

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("HOUSE ERROR")


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=""):
    with closing(db()) as con:

        row = con.execute("""
        SELECT value
        FROM settings
        WHERE key=?
        """, (
            key,
        )).fetchone()

        if not row:
            return default

        return row["value"]


def set_setting(key, value):
    with closing(db()) as con:

        con.execute("""
        INSERT INTO settings(key,value)
        VALUES (?,?)
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
# JOIN CHECK
# ============================================================

async def check_join_required(context, user_id):

    if not JOIN_REQUIRED:
        return True

    try:

        member = await context.bot.get_chat_member(
            JOIN_CHANNEL,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception as e:

        logger.warning(
            "JOIN CHECK ERROR: %s",
            e
        )

        # اگر کانال قابل بررسی نبود، بات قفل نمی‌شود.
        return True


async def require_join(update, context):

    user = update.effective_user

    if not user:
        return False

    if await check_join_required(
        context,
        user.id
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در گپ",
                url=JOIN_URL
            )
        ]
    ])

    await update.effective_message.reply_text(
        "⛔ برای استفاده از ربات ابتدا عضو گپ شوید.",
        reply_markup=keyboard
    )

    return False


# ============================================================
# PARSING
# ============================================================

def parse_amount_from_command(text):
    text = normalize_digits(text or "")

    m = re.search(
        r"(-?\d+(?:[.,]\d+)?)",
        text
    )

    if not m:
        return None

    return parse_decimal_amount(
        m.group(1).replace(",", ".")
    )


def parse_game(text):
    """
    مثال:
    1 تاس 0.1
    2 تاس 0.1
    100 تاس 0.5
    2 بولینگ 1
    5 بسکتبال 0.2
    10 دارت 0.5

    تعداد محدود نیست.
    """

    text = normalize_digits(text or "").strip()

    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    match = pattern.match(text)

    if not match:
        return None

    try:
        rounds = int(match.group(1))
    except Exception:
        return None

    if rounds < 1:
        return None

    game_name = match.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_decimal_amount(
        match.group(3)
    )

    if amount is None:
        return None

    return game, rounds, amount


# ============================================================
# KEYBOARDS
# ============================================================

def user_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["👥 بازی با دوستان", "🤖 بازی با ربات"],
            ["💸 انتقال", "📤 درخواست"],
            ["📖 راهنما"]
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


def created_game_keyboard(game_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_{game_id}"
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
                "📥 وضعیت برداشت",
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
# GAME DATABASE HELPERS
# ============================================================

def get_game(game_id):
    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE id=?
        """, (
            game_id,
        )).fetchone()


def parse_rolls(value):
    if not value:
        return []

    result = []

    for x in value.split(","):

        try:
            result.append(int(x))

        except Exception:
            continue

    return result


def save_creator_roll(
    game_id,
    roll_value
):
    """
    اتمیک اضافه کردن یک پرتاب.
    """

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return None

            if row["status"] != "bot_creator_turn":
                con.execute("ROLLBACK")
                return None

            rolls = parse_rolls(
                row["creator_rolls"]
            )

            rounds = int(row["rounds"])

            if len(rolls) >= rounds:
                con.execute("ROLLBACK")
                return None

            rolls.append(
                int(roll_value)
            )

            new_status = (
                "bot_creator_turn"
                if len(rolls) < rounds
                else
                "bot_ready"
            )

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                status=?
            WHERE id=?
            AND status='bot_creator_turn'
            """, (
                ",".join(map(str, rolls)),
                new_status,
                game_id
            ))

            con.execute("COMMIT")

            return {
                "rolls": rolls,
                "status": new_status,
                "rounds": rounds
            }

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "SAVE CREATOR ROLL ERROR"
            )

            return None


def save_friend_creator_roll(
    game_id,
    roll_value
):

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return None

            if row["status"] != "creator_turn":
                con.execute("ROLLBACK")
                return None

            rolls = parse_rolls(
                row["creator_rolls"]
            )

            rounds = int(row["rounds"])

            if len(rolls) >= rounds:
                con.execute("ROLLBACK")
                return None

            rolls.append(
                int(roll_value)
            )

            new_status = (
                "creator_turn"
                if len(rolls) < rounds
                else
                "opponent_turn"
            )

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                status=?
            WHERE id=?
            AND status='creator_turn'
            """, (
                ",".join(map(str, rolls)),
                new_status,
                game_id
            ))

            con.execute("COMMIT")

            return {
                "rolls": rolls,
                "status": new_status,
                "rounds": rounds,
                "opponent_id": row["opponent_id"]
            }

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "FRIEND CREATOR ROLL ERROR"
            )

            return None


def save_friend_opponent_roll(
    game_id,
    roll_value
):

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return None

            if row["status"] != "opponent_turn":
                con.execute("ROLLBACK")
                return None

            rolls = parse_rolls(
                row["opponent_rolls"]
            )

            rounds = int(row["rounds"])

            if len(rolls) >= rounds:
                con.execute("ROLLBACK")
                return None

            rolls.append(
                int(roll_value)
            )

            new_status = (
                "opponent_turn"
                if len(rolls) < rounds
                else
                "friend_ready"
            )

            con.execute("""
            UPDATE games
            SET opponent_rolls=?,
                status=?
            WHERE id=?
            AND status='opponent_turn'
            """, (
                ",".join(map(str, rolls)),
                new_status,
                game_id
            ))

            con.execute("COMMIT")

            return {
                "rolls": rolls,
                "status": new_status
            }

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "FRIEND OPPONENT ROLL ERROR"
            )

            return None


def lock_bot_rolling(game_id):
    """
    فقط یک coroutine می‌تواند وارد مرحله bot rolling شود.
    """

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            cur = con.execute("""
            UPDATE games
            SET status='bot_rolling'
            WHERE id=?
            AND status='bot_ready'
            """, (
                game_id,
            ))

            if cur.rowcount != 1:

                con.execute("ROLLBACK")
                return False

            con.execute("COMMIT")

            return True

        except Exception:

            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "BOT LOCK ERROR"
            )

            return False


# ============================================================
# ACTIVE GAME FINDER
# ============================================================

def find_active_game(
    chat_id,
    user_id
):

    with closing(db()) as con:

        rows = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND status IN (
            'bot_creator_turn',
            'bot_ready',
            'creator_turn',
            'opponent_turn'
        )
        ORDER BY id DESC
        LIMIT 500
        """, (
            chat_id,
        )).fetchall()

    for game in rows:

        creator_id = int(
            game["creator_id"]
        )

        opponent_id = (
            int(game["opponent_id"])
            if game["opponent_id"] is not None
            else None
        )

        if game["status"] in (
            "bot_creator_turn",
            "bot_ready"
        ):

            if creator_id == user_id:
                return game

        elif game["status"] == "creator_turn":

            if creator_id == user_id:
                return game

        elif game["status"] == "opponent_turn":

            if opponent_id == user_id:
                return game

    return None


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await require_join(
        update,
        context
    ):
        return

    if is_blocked(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )

        return

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "🎮 ربات بازی آماده است.",
        reply_markup=user_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await require_join(
        update,
        context
    ):
        return

    if is_blocked(user.id):
        return

    balance = get_balance(
        user.id
    )

    await update.effective_message.reply_text(
        f"💰 موجودی {name_of(user)}:\n\n"
        f"💎 {money(balance)} TRX"
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "🎮 بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await update.effective_message.reply_text(
            "❌ بازی با دوستان فقط داخل گپ است."
        )

        return

    await update.effective_message.reply_text(
        "👥 بازی با دوستان\n\n"
        "فرمت ساخت بازی:\n\n"
        "1 تاس 0.1\n"
        "2 تاس 0.1\n"
        "5 تاس 0.5\n"
        "2 بولینگ 0.1\n"
        "3 بسکتبال 0.2\n"
        "4 دارت 0.5\n\n"
        "تعداد پرتاب محدودیت ندارد."
    )


async def bot_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n\n"
        "1 تاس 0.1\n"
        "2 تاس 0.1\n"
        "5 تاس 0.5\n"
        "2 بولینگ 0.1\n"
        "3 بسکتبال 0.2\n"
        "4 دارت 0.5\n\n"
        "اول خودت ایموجی بازی را می‌اندازی؛ "
        "بعد از کامل شدن پرتاب‌های تو، ربات خودش می‌اندازد."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context,
    game_type,
    rounds,
    amount
):

    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    # --------------------------------------------------------
    # قفل مبلغ سازنده
    # --------------------------------------------------------

    async with DB_LOCK:

        if not debit_balance(
            user.id,
            amount
        ):

            await message.reply_text(
                "❌ موجودی کافی نیست یا تغییر کرده است."
            )

            return

        try:

            with closing(db()) as con:

                cur = con.execute("""
                INSERT INTO games
                (
                    chat_id,
                    creator_id,
                    game_type,
                    amount,
                    rounds,
                    status,
                    settled
                )
                VALUES (?, ?, ?, ?, ?, 'waiting', 0)
                """, (
                    chat.id,
                    user.id,
                    game_type,
                    str(amount),
                    rounds
                ))

                game_id = cur.lastrowid

                con.commit()

            text = (
                f"{GAME_LABELS[game_type]}\n\n"
                f"🎮 تعداد پرتاب: {rounds}\n"
                f"💰 مبلغ: {money(amount)} TRX\n\n"
                f"👤 سازنده: {name_of(user)}\n\n"
                "برای شروع یکی از گزینه‌ها را بزن:"
            )

            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                reply_markup=created_game_keyboard(
                    game_id
                )
            )

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET message_id=?
                WHERE id=?
                """, (
                    sent.message_id,
                    game_id
                ))

                con.commit()

        except Exception:

            # اگر ساخت پیام شکست خورد،
            # مبلغ فقط یک بار برگردانده می‌شود.
            credit_balance(
                user.id,
                amount
            )

            try:

                with closing(db()) as con:

                    con.execute("""
                    UPDATE games
                    SET status='cancelled',
                        settled=1
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    con.commit()

            except Exception:
                pass

            logger.exception(
                "CREATE GAME ERROR"
            )

            await message.reply_text(
                "❌ ساخت بازی ناموفق بود؛ "
                "مبلغ به موجودی برگشت."
            )


# ============================================================
# GAME CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    game_type = query.data.replace(
        "game_",
        "",
        1
    )

    if game_type not in GAME_LABELS:
        return

    label = GAME_LABELS[game_type]

    await query.message.reply_text(
        f"{label}\n\n"
        f"مثال:\n"
        f"1 {label.split(' ', 1)[1]} 0.1\n\n"
        "تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    if not await check_join_required(
        context,
        user.id
    ):
        await query.answer(
            "ابتدا عضو گپ شوید.",
            show_alert=True
        )
        return

    ensure_user(user)

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی پیدا نشد.",
                        show_alert=True
                    )

                    return

                if game["status"] != "waiting":

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی قبلاً شروع شده.",
                        show_alert=True
                    )

                    return

                creator_id = int(
                    game["creator_id"]
                )

                if creator_id == user.id:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ خودت سازنده بازی هستی.",
                        show_alert=True
                    )

                    return

                amount = D(
                    game["amount"]
                )

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                if not row:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ کاربر پیدا نشد.",
                        show_alert=True
                    )

                    return

                balance = D(
                    row["balance"]
                )

                if balance < amount:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ موجودی کافی نیست.",
                        show_alert=True
                    )

                    return

                # قفل مبلغ حریف
                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(balance - amount),
                    user.id
                ))

                con.execute("""
                UPDATE games
                SET opponent_id=?,
                    status='creator_turn'
                WHERE id=?
                AND status='waiting'
                """, (
                    user.id,
                    game_id
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "JOIN FRIEND ERROR"
                )

                await query.answer(
                    "❌ خطا هنگام ورود به بازی.",
                    show_alert=True
                )

                return

    # --------------------------------------------------------
    # حذف دکمه‌ها
    # --------------------------------------------------------

    try:

        await query.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await query.message.reply_text(
        "👥 بازی شروع شد.\n\n"
        f"👤 سازنده: {name_of_user_id(creator_id)}\n"
        f"👤 حریف: {name_of(user)}\n\n"
        f"🎯 ابتدا {name_of_user_id(creator_id)} "
        f"خودش {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد."
    )


# ============================================================
# JOIN BOT
# ============================================================

async def join_bot(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    ensure_user(user)

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی پیدا نشد.",
                        show_alert=True
                    )

                    return

                if game["status"] != "waiting":

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی قبلاً شروع شده.",
                        show_alert=True
                    )

                    return

                creator_id = int(
                    game["creator_id"]
                )

                if creator_id != user.id:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ فقط سازنده بازی می‌تواند "
                        "بازی با ربات را شروع کند.",
                        show_alert=True
                    )

                    return

                # مبلغ از قبل هنگام ساخت بازی قفل شده.
                # اینجا دوباره کم نمی‌شود.

                con.execute("""
                UPDATE games
                SET status='bot_creator_turn'
                WHERE id=?
                AND status='waiting'
                """, (
                    game_id,
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "JOIN BOT ERROR"
                )

                await query.answer(
                    "❌ خطا هنگام شروع بازی.",
                    show_alert=True
                )

                return

    # حذف دکمه‌ها
    try:

        await query.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await query.message.reply_text(
        "🤖 بازی با ربات شروع شد.\n\n"
        f"👤 {name_of(user)}\n\n"
        f"اول خودت {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرست.\n\n"
        "بعد از آخرین پرتاب، ربات خودش بازی را انجام می‌دهد."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی پیدا نشد.",
                        show_alert=True
                    )

                    return

                if game["status"] != "waiting":

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ بازی شروع شده و قابل لغو نیست.",
                        show_alert=True
                    )

                    return

                if int(game["creator_id"]) != user.id:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ فقط سازنده می‌تواند لغو کند.",
                        show_alert=True
                    )

                    return

                if int(game["settled"]) == 1:

                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ این بازی قبلاً تسویه شده.",
                        show_alert=True
                    )

                    return

                amount = D(
                    game["amount"]
                )

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                if row:

                    balance = D(
                        row["balance"]
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(balance + amount),
                        user.id
                    ))

                con.execute("""
                UPDATE games
                SET status='cancelled',
                    settled=1
                WHERE id=?
                AND settled=0
                """, (
                    game_id,
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "CANCEL ERROR"
                )

                await query.answer(
                    "❌ خطا هنگام لغو.",
                    show_alert=True
                )

                return

    try:

        await query.message.edit_reply_markup(
            reply_markup=None
        )

    except Exception:
        pass

    await query.message.reply_text(
        f"❌ بازی لغو شد.\n"
        f"💰 {money(game['amount'])} TRX برگشت داده شد."
    )


# ============================================================
# SAFE BOT DICE
# ============================================================

async def safe_bot_dice(
    context,
    chat_id,
    emoji
):

    try:

        sent = await context.bot.send_dice(
            chat_id=chat_id,
            emoji=emoji
        )

        if not sent:
            return None

        if not sent.dice:
            return None

        return int(
            sent.dice.value
        )

    except Exception:

        logger.exception(
            "BOT SEND DICE ERROR"
        )

        return None


# ============================================================
# BOT ROLL ENGINE
# ============================================================

async def run_bot_rolls(
    context,
    game_id
):

    game = get_game(
        game_id
    )

    if not game:
        return

    if game["status"] != "bot_ready":
        return

    # --------------------------------------------------------
    # قفل ضد اجرای هم‌زمان
    # --------------------------------------------------------

    if not lock_bot_rolling(
        game_id
    ):
        return

    game = get_game(
        game_id
    )

    if not game:
        return

    rounds = int(
        game["rounds"]
    )

    game_type = game["game_type"]

    bot_rolls = []

    # --------------------------------------------------------
    # ربات واقعاً خودش ایموجی می‌اندازد
    # --------------------------------------------------------

    for index in range(rounds):

        value = await safe_bot_dice(
            context,
            game["chat_id"],
            GAME_EMOJIS[game_type]
        )

        # یک retry در صورت خطای Telegram
        if value is None:

            await asyncio.sleep(1)

            value = await safe_bot_dice(
                context,
                game["chat_id"],
                GAME_EMOJIS[game_type]
            )

        # ----------------------------------------------------
        # اگر ربات نتوانست پرتاب کند
        # ----------------------------------------------------

        if value is None:

            amount = D(
                game["amount"]
            )

            user_id = int(
                game["creator_id"]
            )

            async with DB_LOCK:

                with closing(db()) as con:

                    try:

                        con.execute(
                            "BEGIN IMMEDIATE"
                        )

                        row = con.execute("""
                        SELECT status, settled
                        FROM games
                        WHERE id=?
                        """, (
                            game_id,
                        )).fetchone()

                        if (
                            row and
                            row["status"] == "bot_rolling" and
                            int(row["settled"]) == 0
                        ):

                            con.execute("""
                            UPDATE games
                            SET status='bot_error',
                                settled=1
                            WHERE id=?
                            """, (
                                game_id,
                            ))

                            user_row = con.execute("""
                            SELECT balance
                            FROM users
                            WHERE user_id=?
                            """, (
                                user_id,
                            )).fetchone()

                            if user_row:

                                balance = D(
                                    user_row["balance"]
                                )

                                con.execute("""
                                UPDATE users
                                SET balance=?
                                WHERE user_id=?
                                """, (
                                    str(balance + amount),
                                    user_id
                                ))

                        con.execute("COMMIT")

                    except Exception:

                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass

                        logger.exception(
                            "BOT ERROR REFUND"
                        )

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "⚠️ ربات نتوانست بازی را کامل کند.\n\n"
                    f"💰 {money(amount)} TRX "
                    "به سازنده برگشت داده شد."
                )
            )

            return

        bot_rolls.append(
            value
        )

        # فاصله بین پرتاب‌ها
        if index < rounds - 1:
            await asyncio.sleep(0.8)

    # --------------------------------------------------------
    # ذخیره نتیجه و قفل پایان
    # --------------------------------------------------------

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                row = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not row:

                    con.execute("ROLLBACK")
                    return

                if row["status"] != "bot_rolling":

                    con.execute("ROLLBACK")
                    return

                if int(row["settled"]) == 1:

                    con.execute("ROLLBACK")
                    return

                con.execute("""
                UPDATE games
                SET opponent_rolls=?,
                    status='bot_finished'
                WHERE id=?
                AND status='bot_rolling'
                AND settled=0
                """, (
                    ",".join(
                        map(str, bot_rolls)
                    ),
                    game_id
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "SAVE BOT RESULT ERROR"
                )

                return

    await settle_bot_game(
        context,
        game_id
    )


# ============================================================
# BOT SETTLEMENT
# ============================================================

async def settle_bot_game(
    context,
    game_id
):

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:

                    con.execute("ROLLBACK")
                    return

                if game["status"] != "bot_finished":

                    con.execute("ROLLBACK")
                    return

                if int(game["settled"]) == 1:

                    con.execute("ROLLBACK")
                    return

                creator_id = int(
                    game["creator_id"]
                )

                amount = D(
                    game["amount"]
                )

                creator_rolls = parse_rolls(
                    game["creator_rolls"]
                )

                bot_rolls = parse_rolls(
                    game["opponent_rolls"]
                )

                creator_score = sum(
                    creator_rolls
                )

                bot_score = sum(
                    bot_rolls
                )

                # ------------------------------------------------
                # مساوی
                # ------------------------------------------------

                if creator_score == bot_score:

                    row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        creator_id,
                    )).fetchone()

                    if row:

                        balance = D(
                            row["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(balance + amount),
                            creator_id
                        ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        settled=1
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    result_type = "draw"

                # ------------------------------------------------
                # کاربر برد
                # ------------------------------------------------

                elif creator_score > bot_score:

                    payout = (
                        amount * Decimal("2")
                        - OWNER_SHARE
                        - BOT_FEE
                    )

                    row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        creator_id,
                    )).fetchone()

                    if not row:

                        con.execute("ROLLBACK")
                        return

                    balance = D(
                        row["balance"]
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(balance + payout),
                        creator_id
                    ))

                    house = con.execute("""
                    SELECT owner_balance,
                           fee_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    owner_balance = D(
                        house["owner_balance"]
                    )

                    fee_balance = D(
                        house["fee_balance"]
                    )

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?,
                        fee_balance=?
                    WHERE id=1
                    """, (
                        str(
                            owner_balance +
                            OWNER_SHARE
                        ),
                        str(
                            fee_balance +
                            BOT_FEE
                        )
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=?,
                        settled=1
                    WHERE id=?
                    """, (
                        creator_id,
                        game_id
                    ))

                    result_type = "user"
                    result_payout = payout

                # ------------------------------------------------
                # ربات برد
                # ------------------------------------------------

                else:

                    house = con.execute("""
                    SELECT owner_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    owner_balance = D(
                        house["owner_balance"]
                    )

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?
                    WHERE id=1
                    """, (
                        str(
                            owner_balance + amount
                        ),
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        settled=1
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    result_type = "bot"

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "BOT SETTLEMENT ERROR"
                )

                return

    # --------------------------------------------------------
    # نتیجه
    # --------------------------------------------------------

    if result_type == "draw":

        text = (
            "🤝 نتیجه بازی\n\n"
            f"👤 {name_of_user_id(creator_id)}: "
            f"{creator_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            "🤝 مساوی شد.\n"
            f"💰 {money(amount)} TRX برگشت داده شد."
        )

    elif result_type == "user":

        text = (
            "🏆 نتیجه بازی\n\n"
            f"👤 {name_of_user_id(creator_id)}: "
            f"{creator_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            f"🏆 برنده: "
            f"{name_of_user_id(creator_id)}\n"
            f"🎯 امتیاز برنده: {creator_score}\n"
            f"💰 دریافتی: "
            f"{money(result_payout)} TRX"
        )

    else:

        text = (
            "🏆 نتیجه بازی\n\n"
            f"👤 {name_of_user_id(creator_id)}: "
            f"{creator_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            "🏆 برنده: 🤖 ربات"
        )

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=text
        )

    except Exception:

        logger.exception(
            "SEND BOT RESULT ERROR"
        )


# ============================================================
# FRIEND SETTLEMENT
# ============================================================

async def settle_friend_game(
    context,
    game_id
):

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:

                    con.execute("ROLLBACK")
                    return

                if game["status"] != "friend_ready":

                    con.execute("ROLLBACK")
                    return

                if int(game["settled"]) == 1:

                    con.execute("ROLLBACK")
                    return

                creator_id = int(
                    game["creator_id"]
                )

                opponent_id = int(
                    game["opponent_id"]
                )

                amount = D(
                    game["amount"]
                )

                creator_rolls = parse_rolls(
                    game["creator_rolls"]
                )

                opponent_rolls = parse_rolls(
                    game["opponent_rolls"]
                )

                creator_score = sum(
                    creator_rolls
                )

                opponent_score = sum(
                    opponent_rolls
                )

                # ------------------------------------------------
                # مساوی
                # ------------------------------------------------

                if creator_score == opponent_score:

                    creator_row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        creator_id,
                    )).fetchone()

                    opponent_row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        opponent_id,
                    )).fetchone()

                    if creator_row:

                        balance = D(
                            creator_row["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(balance + amount),
                            creator_id
                        ))

                    if opponent_row:

                        balance = D(
                            opponent_row["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(balance + amount),
                            opponent_id
                        ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        settled=1
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    result_type = "draw"

                else:

                    if creator_score > opponent_score:

                        winner_id = creator_id
                        loser_id = opponent_id
                        winner_score = creator_score
                        loser_score = opponent_score

                    else:

                        winner_id = opponent_id
                        loser_id = creator_id
                        winner_score = opponent_score
                        loser_score = creator_score

                    payout = (
                        amount * Decimal("2")
                        - OWNER_SHARE
                        - BOT_FEE
                    )

                    winner_row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        winner_id,
                    )).fetchone()

                    if not winner_row:

                        con.execute("ROLLBACK")
                        return

                    winner_balance = D(
                        winner_row["balance"]
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(
                            winner_balance +
                            payout
                        ),
                        winner_id
                    ))

                    house = con.execute("""
                    SELECT owner_balance,
                           fee_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    owner_balance = D(
                        house["owner_balance"]
                    )

                    fee_balance = D(
                        house["fee_balance"]
                    )

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?,
                        fee_balance=?
                    WHERE id=1
                    """, (
                        str(
                            owner_balance +
                            OWNER_SHARE
                        ),
                        str(
                            fee_balance +
                            BOT_FEE
                        )
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=?,
                        settled=1
                    WHERE id=?
                    """, (
                        winner_id,
                        game_id
                    ))

                    result_type = "winner"

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "FRIEND SETTLEMENT ERROR"
                )

                return

    # --------------------------------------------------------
    # نتیجه
    # --------------------------------------------------------

    if result_type == "draw":

        text = (
            "🤝 نتیجه بازی\n\n"
            f"👤 {name_of_user_id(creator_id)}: "
            f"{creator_score}\n"
            f"👤 {name_of_user_id(opponent_id)}: "
            f"{opponent_score}\n\n"
            "🤝 مساوی شد.\n"
            f"💰 {money(amount)} TRX به هر دو نفر برگشت داده شد."
        )

    else:

        text = (
            "🏆 نتیجه بازی\n\n"
            f"👤 {name_of_user_id(creator_id)}: "
            f"{creator_score}\n"
            f"👤 {name_of_user_id(opponent_id)}: "
            f"{opponent_score}\n\n"
            f"🏆 برنده: "
            f"{name_of_user_id(winner_id)}\n"
            f"🎯 امتیاز برنده: {winner_score}\n"
            f"🎯 امتیاز حریف: {loser_score}\n"
            f"💰 دریافتی: {money(payout)} TRX"
        )

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=text
        )

    except Exception:

        logger.exception(
            "SEND FRIEND RESULT ERROR"
        )


# ============================================================
# USER DICE ROUTER
# ============================================================

async def process_user_roll(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    dice = message.dice

    if not dice:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    # --------------------------------------------------------
    # فقط بازی فعال مربوط به همین کاربر
    # --------------------------------------------------------

    game = find_active_game(
        message.chat_id,
        user.id
    )

    if not game:
        return

    game_type = game["game_type"]

    # --------------------------------------------------------
    # ایموجی اشتباه
    # --------------------------------------------------------

    if dice.emoji != GAME_EMOJIS[game_type]:

        await message.reply_text(
            f"❌ برای این بازی باید "
            f"{GAME_EMOJIS[game_type]} بفرستی."
        )

        return

    game_id = int(
        game["id"]
    )

    # ========================================================
    # BOT GAME
    # ========================================================

    if game["status"] == "bot_creator_turn":

        result = save_creator_roll(
            game_id,
            int(dice.value)
        )

        if result is None:
            return

        rolls = result["rolls"]
        rounds = result["rounds"]

        if result["status"] == "bot_creator_turn":

            remaining = (
                rounds -
                len(rolls)
            )

            await message.reply_text(
                f"👤 {name_of(user)}: "
                f"{dice.value}\n\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        # ----------------------------------------------------
        # آخرین پرتاب کاربر
        # ----------------------------------------------------

        await message.reply_text(
            f"👤 {name_of(user)}: "
            f"{dice.value}\n\n"
            "✅ پرتاب‌های شما کامل شد.\n"
            "🤖 حالا ربات خودش می‌اندازد..."
        )

        # مهم:
        # فقط یک اجرا وارد bot_rolling می‌شود.
        await run_bot_rolls(
            context,
            game_id
        )

        return

    # ========================================================
    # FRIEND CREATOR
    # ========================================================

    if game["status"] == "creator_turn":

        if int(game["creator_id"]) != user.id:
            return

        result = save_friend_creator_roll(
            game_id,
            int(dice.value)
        )

        if result is None:
            return

        rolls = result["rolls"]
        rounds = result["rounds"]

        if result["status"] == "creator_turn":

            remaining = (
                rounds -
                len(rolls)
            )

            await message.reply_text(
                f"👤 {name_of(user)}: "
                f"{dice.value}\n\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        opponent_id = int(
            result["opponent_id"]
        )

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            f"👉 حالا "
            f"{name_of_user_id(opponent_id)} "
            f"خودش {rounds} بار "
            f"{GAME_EMOJIS[game_type]} بفرستد."
        )

        return

    # ========================================================
    # FRIEND OPPONENT
    # ========================================================

    if game["status"] == "opponent_turn":

        opponent_id = int(
            game["opponent_id"]
        )

        if opponent_id != user.id:
            return

        result = save_friend_opponent_roll(
            game_id,
            int(dice.value)
        )

        if result is None:
            return

        rolls = result["rolls"]
        rounds = int(
            game["rounds"]
        )

        if result["status"] == "opponent_turn":

            remaining = (
                rounds -
                len(rolls)
            )

            await message.reply_text(
                f"👤 {name_of(user)}: "
                f"{dice.value}\n\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            "⏳ در حال محاسبه نتیجه..."
        )

        await settle_friend_game(
            context,
            game_id
        )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال باید روی پیام کاربر Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 0.5"
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

    amount = parse_amount_from_command(
        message.text
    )

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    ensure_user(target)

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                sender = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                receiver = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    target.id,
                )).fetchone()

                if not sender or not receiver:

                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )

                    return

                sender_balance = D(
                    sender["balance"]
                )

                receiver_balance = D(
                    receiver["balance"]
                )

                if sender_balance < amount:

                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ موجودی کافی نیست."
                    )

                    return

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(
                        sender_balance -
                        amount
                    ),
                    user.id
                ))

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(
                        receiver_balance +
                        amount
                    ),
                    target.id
                ))

                con.execute("""
                INSERT INTO transfers
                (sender_id, receiver_id, amount)
                VALUES (?, ?, ?)
                """, (
                    user.id,
                    target.id,
                    str(amount)
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "TRANSFER ERROR"
                )

                await message.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {money(amount)} TRX"
    )


# ============================================================
# REQUEST
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
            str(amount),
            wallet
        ))

        con.commit()


async def request_menu(
    update,
    context
):

    user = update.effective_user

    ensure_user(user)

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5\n\n"
        "بعد اطلاعات درخواست را بفرست."
    )

    context.user_data[
        "request_mode"
    ] = "amount"


# ============================================================
# HELP
# ============================================================

async def help_command(
    update,
    context
):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی در گپ:\n\n"
        "1 تاس 0.1\n"
        "2 تاس 0.1\n"
        "5 تاس 0.5\n"
        "2 بولینگ 0.1\n"
        "3 بسکتبال 0.2\n"
        "4 دارت 0.5\n\n"
        "👥 بازی دوستان:\n"
        "سازنده اول خودش می‌اندازد، "
        "بعد حریف خودش می‌اندازد.\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر خودش می‌اندازد، "
        "بعد ربات خودش می‌اندازد.\n\n"
        "💰 موجودی\n"
        "💸 انتقال 0.5 ← با Reply\n"
        "📤 درخواست\n\n"
        "تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# ADMIN
# ============================================================

async def admin(
    update,
    context
):

    user = update.effective_user

    if not user or not is_admin(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.effective_message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update,
    context
):

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
            ORDER BY user_id DESC
            LIMIT 50
            """).fetchall()

        text = "👥 کاربران\n\n"

        for row in rows:

            name = (
                row["first_name"]
                or row["username"]
                or str(row["user_id"])
            )

            status = (
                "🚫"
                if row["blocked"]
                else
                "✅"
            )

            text += (
                f"{status} {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {money(row['balance'])} TRX\n\n"
            )

        await query.edit_message_text(
            text[:4000]
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

            games = con.execute("""
            SELECT COUNT(*)
            FROM games
            """).fetchone()[0]

            active = con.execute("""
            SELECT COUNT(*)
            FROM games
            WHERE status NOT IN (
                'finished',
                'cancelled',
                'bot_error'
            )
            """).fetchone()[0]

            pending = con.execute("""
            SELECT COUNT(*)
            FROM requests
            WHERE status='pending'
            """).fetchone()[0]

            house = con.execute("""
            SELECT owner_balance,
                   fee_balance
            FROM house
            WHERE id=1
            """).fetchone()

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"🎮 کل بازی‌ها: {games:,}\n"
            f"🟢 بازی‌های فعال: {active:,}\n"
            f"📋 درخواست‌ها: {pending:,}\n\n"
            f"👑 سهم مالک: "
            f"{money(house['owner_balance'])} TRX\n"
            f"🤖 کارمزد: "
            f"{money(house['fee_balance'])} TRX\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if withdraw_enabled() else 'خاموش 🔴'}"
        )

        return

    # --------------------------------------------------------
    # WITHDRAW
    # --------------------------------------------------------

    if data == "admin_withdraw_toggle":

        value = not withdraw_enabled()

        set_setting(
            "withdraw_enabled",
            "1" if value else "0"
        )

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if value else 'خاموش 🔴'}",
            reply_markup=admin_keyboard()
        )

        return

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 0.5"
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 0.5"
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
                f"💰 {money(row['amount'])} TRX\n"
                f"📝 {row['wallet']}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )


# ============================================================
# ADMIN ADD
# ============================================================

async def add_balance(
    update,
    context
):

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

        amount = parse_decimal_amount(
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

    credit_balance(
        target_id,
        amount
    )

    await update.message.reply_text(
        f"✅ افزایش موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN REMOVE
# ============================================================

async def remove_balance(
    update,
    context
):

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

        amount = parse_decimal_amount(
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

    if not debit_balance(
        target_id,
        amount
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    await update.message.reply_text(
        f"✅ کاهش موجودی انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➖ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


# ============================================================
# BLOCK
# ============================================================

async def block(
    update,
    context
):

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

        con.commit()

    await update.message.reply_text(
        f"🚫 کاربر {target_id} مسدود شد."
    )


async def unblock(
    update,
    context
):

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

        con.commit()

    await update.message.reply_text(
        f"✅ کاربر {target_id} رفع مسدودی شد."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    text = (
        message.text or ""
    ).strip()

    normalized = normalize_digits(
        text
    )

    # --------------------------------------------------------
    # REQUEST FLOW
    # --------------------------------------------------------

    request_mode = context.user_data.get(
        "request_mode"
    )

    if request_mode == "amount":

        amount = parse_amount_from_command(
            normalized
        )

        if amount:

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

    elif request_mode == "wallet":

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
    # GAME CREATION
    # --------------------------------------------------------

    parsed = parse_game(
        normalized
    )

    if parsed:

        game_type, rounds, amount = parsed

        await create_game(
            update,
            context,
            game_type,
            rounds,
            amount
        )

        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if text in (
        "💰 موجودی",
        "موجودی",
        "موجودی ترون",
        "موجودی TRX",
        "موجودی trx",
        "balance"
    ):

        await show_balance(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # FRIENDS
    # --------------------------------------------------------

    if text == "👥 بازی با دوستان":

        await friends_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if text == "🤖 بازی با ربات":

        await bot_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if re.match(
        r"^(انتقال|transfer)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        await transfer(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

    if re.match(
        r"^(درخواست|request)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        amount = parse_amount_from_command(
            normalized
        )

        if amount:

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

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if text == "📖 راهنما":

        await help_command(
            update,
            context
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "UNHANDLED ERROR: %s",
        context.error,
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
            show_balance
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
            friends_menu
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
    # GAME MENU CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    # --------------------------------------------------------
    # GAME ACTION CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            join_friend,
            pattern=r"^join_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_bot,
            pattern=r"^bot_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_\d+$"
        )
    )

    # --------------------------------------------------------
    # ADMIN CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # --------------------------------------------------------
    # DICE / GAMES
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            process_user_roll
        ),
        group=0
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        ),
        group=1
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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
