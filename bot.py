# ============================================================
# BOT.PY - TRX INTERNAL GAME BOT
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

FORCE_JOIN = True
FORCE_CHANNEL = "@zobxt"
FORCE_CHANNEL_LINK = "https://t.me/zobxt"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
REF_REWARD = Decimal("0.05")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

DB_LOCK = asyncio.Lock()

# جلوگیری از چند بازی هم‌زمان با ربات
BOT_GAME_LOCK = asyncio.Lock()
BOT_BUSY = False

# ============================================================
# GAMES
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
        isolation_level=None
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
            referrer_id INTEGER DEFAULT NULL,
            referral_paid INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT NOT NULL,
            amount TEXT NOT NULL,
            rounds INTEGER NOT NULL,
            creator_rolls TEXT DEFAULT '',
            opponent_rolls TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            winner_id INTEGER DEFAULT NULL,
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
        CREATE TABLE IF NOT EXISTS house (
            id INTEGER PRIMARY KEY CHECK(id=1),
            owner_balance TEXT DEFAULT '0',
            fee_balance TEXT DEFAULT '0'
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
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

        # مهاجرت نسخه‌های قدیمی
        cols = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        if "referrer_id" not in cols:
            con.execute(
                "ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL"
            )

        if "referral_paid" not in cols:
            con.execute(
                "ALTER TABLE users ADD COLUMN referral_paid INTEGER DEFAULT 0"
            )

        con.commit()


# ============================================================
# MONEY
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

    text = normalize_digits(text)
    text = text.replace(",", ".").replace("٬", "").strip()

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if value < MIN_GAME or value > MAX_GAME:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


# ============================================================
# USERS
# ============================================================

def ensure_user(user, referrer_id=None):
    if not user:
        return

    with closing(db()) as con:

        row = con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user.id,)
        ).fetchone()

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
            ref = None

            if referrer_id:
                try:
                    ref = int(referrer_id)
                except Exception:
                    ref = None

            if ref == user.id:
                ref = None

            con.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance, referrer_id)
            VALUES (?, ?, ?, '0', ?)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                ref
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

    return D(row["balance"])


def name_of(user):
    if not user:
        return "کاربر"

    if getattr(user, "first_name", None):
        return user.first_name

    if getattr(user, "username", None):
        return "@" + user.username

    return str(user.id)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_blocked(user_id):
    row = get_user(user_id)
    return bool(row and int(row["blocked"]) == 1)


# ============================================================
# ATOMIC BALANCE
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
            """, (user_id,)).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            balance = D(row["balance"])

            if balance < amount:
                con.execute("ROLLBACK")
                return False

            new_balance = balance - amount

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
            """, (user_id,)).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            balance = D(row["balance"])
            new_balance = balance + amount

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


def transfer_atomic(sender, receiver, amount):
    amount = D(amount)

    if amount <= 0 or sender == receiver:
        return False

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            s = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (sender,)
            ).fetchone()

            r = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (receiver,)
            ).fetchone()

            if not s or not r:
                con.execute("ROLLBACK")
                return False

            sb = D(s["balance"])

            if sb < amount:
                con.execute("ROLLBACK")
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(sb - amount),
                sender
            ))

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(D(r["balance"]) + amount),
                receiver
            ))

            con.execute("""
            INSERT INTO transfers
            (sender_id, receiver_id, amount)
            VALUES (?, ?, ?)
            """, (
                sender,
                receiver,
                str(amount)
            ))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("TRANSFER ERROR")
            return False


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount, fee_amount):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)

    with closing(db()) as con:
        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            "SELECT owner_balance, fee_balance FROM house WHERE id=1"
        ).fetchone()

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


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=""):
    with closing(db()) as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        return row["value"] if row else default


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


# ============================================================
# FORCE JOIN
# ============================================================

async def joined_channel(context, user_id):
    if not FORCE_JOIN:
        return True

    if user_id in ADMIN_IDS:
        return True

    try:
        member = await context.bot.get_chat_member(
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

    if await joined_channel(context, user.id):
        return True

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=FORCE_CHANNEL_LINK
            )
        ]
    ])

    await update.effective_message.reply_text(
        "⛔ برای استفاده از ربات ابتدا در کانال عضو شو.",
        reply_markup=markup
    )

    return False


# ============================================================
# REFERRAL
# ============================================================

def pay_referral(user_id):
    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT referrer_id, referral_paid
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            if not row["referrer_id"] or int(row["referral_paid"]) == 1:
                con.execute("ROLLBACK")
                return False

            referrer_id = int(row["referrer_id"])

            ref = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (referrer_id,)).fetchone()

            if not ref:
                con.execute("ROLLBACK")
                return False

            new_balance = D(ref["balance"]) + REF_REWARD

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(new_balance),
                referrer_id
            ))

            con.execute("""
            UPDATE users
            SET referral_paid=1
            WHERE user_id=?
            """, (user_id,))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("REF ERROR")
            return False


# ============================================================
# PARSING
# ============================================================

def parse_game(text):
    text = normalize_digits(text or "").strip()

    # مثال:
    # 1 تاس 0.5
    # 2 تاس 0.1
    # 100 بولینگ 1
    # تعداد نامحدود

    m = re.match(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    rounds = int(m.group(1))
    game_name = m.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game or rounds < 1:
        return None

    amount = parse_decimal_amount(
        m.group(3).replace(",", ".")
    )

    if amount is None:
        return None

    return game, rounds, amount


def parse_amount(text):
    text = normalize_digits(text or "")

    m = re.search(
        r"(\d+(?:[.,]\d+)?)",
        text
    )

    if not m:
        return None

    return parse_decimal_amount(
        m.group(1).replace(",", ".")
    )


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


def game_created_keyboard(game_id):
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

    referrer = None

    if context.args:
        payload = context.args[0]

        if payload.startswith("ref_"):
            try:
                referrer = int(
                    payload.replace("ref_", "", 1)
                )
            except Exception:
                referrer = None

    ensure_user(
        user,
        referrer
    )

    if not await require_join(update, context):
        return

    if is_blocked(user.id):
        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )
        return

    # فقط یک بار پاداش Ref
    paid = pay_referral(user.id)

    text = (
        "👋 سلام!\n\n"
        "🎮 به ربات بازی خوش آمدی.\n\n"
        "💰 واحد نمایش موجودی: TRX\n"
        "این موجودی اعتبار داخلی بازی است و TRX شبکه TRON نیست.\n\n"
        "از منوی زیر استفاده کن."
    )

    if paid:
        text += "\n\n🎁 پاداش زیرمجموعه ثبت شد."

    await update.effective_message.reply_text(
        text,
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

    if not await require_join(update, context):
        return

    if is_blocked(user.id):
        return

    await update.effective_message.reply_text(
        f"💰 موجودی:\n\n"
        f"💎 {money(get_balance(user.id))} TRX"
    )


# ============================================================
# MENUS
# ============================================================

async def game_menu(update, context):

    if not await require_join(update, context):
        return

    await update.effective_message.reply_text(
        "🎮 نوع بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if not await require_join(update, context):
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        await update.effective_message.reply_text(
            "❌ بازی با دوستان فقط داخل گپ قابل اجراست."
        )
        return

    await update.effective_message.reply_text(
        "👥 بازی با دوستان\n\n"
        "در گپ بنویس:\n\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "10 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "تعداد پرتاب محدودیت ندارد."
    )


async def bot_menu(update, context):

    if not await require_join(update, context):
        return

    await update.effective_message.reply_text(
        "🤖 بازی با ربات\n\n"
        "در گپ بنویس:\n\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "اول خودت ایموجی بازی را می‌فرستی؛ "
        "بعد از تمام پرتاب‌های تو، ربات خودش پرتاب می‌کند."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, game, rounds, amount):

    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    ensure_user(user)

    if not await require_join(update, context):
        return

    if is_blocked(user.id):
        return

    if get_balance(user.id) < amount:
        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(get_balance(user.id))} TRX"
        )
        return

    # اتمیک: مبلغ دقیقاً یک بار رزرو می‌شود
    if not debit_balance(user.id, amount):
        await message.reply_text(
            "❌ موجودی تغییر کرده؛ دوباره تلاش کن."
        )
        return

    game_id = None

    try:
        with closing(db()) as con:

            con.execute("BEGIN IMMEDIATE")

            cur = con.execute("""
            INSERT INTO games
            (
                chat_id,
                creator_id,
                game_type,
                amount,
                rounds,
                status
            )
            VALUES (?, ?, ?, ?, ?, 'waiting')
            """, (
                chat.id,
                user.id,
                game,
                str(amount),
                rounds
            ))

            game_id = cur.lastrowid

            text = (
                f"{GAME_LABELS[game]}\n\n"
                f"🎯 تعداد پرتاب: {rounds}\n"
                f"💰 مبلغ: {money(amount)} TRX\n\n"
                f"👤 سازنده: {name_of(user)}\n\n"
                f"یکی از گزینه‌ها را انتخاب کنید:"
            )

            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=text,
                reply_markup=game_created_keyboard(game_id)
            )

            con.execute("""
            UPDATE games
            SET message_id=?
            WHERE id=?
            """, (
                sent.message_id,
                game_id
            ))

            con.execute("COMMIT")

    except Exception:

        try:
            with closing(db()) as con:
                con.execute("BEGIN IMMEDIATE")

                if game_id:
                    con.execute("""
                    DELETE FROM games
                    WHERE id=? AND status='waiting'
                    """, (game_id,))

                con.execute("COMMIT")
        except Exception:
            pass

        # برگشت مبلغ
        credit_balance(
            user.id,
            amount
        )

        logger.exception("CREATE GAME ERROR")

        await message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ برگشت داده شد."
        )


# ============================================================
# GAME MENU CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    if is_blocked(user.id):
        return

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    label = GAME_LABELS[game]

    await query.message.reply_text(
        f"{label}\n\n"
        f"برای ساخت بازی در گپ بنویس:\n\n"
        f"1 {label.split(' ', 1)[1]} 0.5\n"
        f"2 {label.split(' ', 1)[1]} 0.5\n\n"
        f"تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# GAME DATABASE HELPERS
# ============================================================

def get_game(game_id):
    with closing(db()) as con:
        return con.execute(
            "SELECT * FROM games WHERE id=?",
            (game_id,)
        ).fetchone()


def save_rolls(game_id, creator_rolls, opponent_rolls, status=None):
    with closing(db()) as con:

        if status:
            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                opponent_rolls=?,
                status=?
            WHERE id=?
            """, (
                ",".join(map(str, creator_rolls)),
                ",".join(map(str, opponent_rolls)),
                status,
                game_id
            ))
        else:
            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                opponent_rolls=?
            WHERE id=?
            """, (
                ",".join(map(str, creator_rolls)),
                ",".join(map(str, opponent_rolls)),
                game_id
            ))

        con.commit()


def parse_rolls(value):
    if not value:
        return []

    result = []

    for x in value.split(","):
        try:
            result.append(int(x))
        except Exception:
            pass

    return result


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.split("_", 1)[1])
    except Exception:
        await query.answer("❌ بازی نامعتبر است.", show_alert=True)
        return

    ensure_user(user)

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute(
                    "SELECT * FROM games WHERE id=?",
                    (game_id,)
                ).fetchone()

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
                        "❌ این بازی دیگر قابل ورود نیست.",
                        show_alert=True
                    )
                    return

                creator_id = int(game["creator_id"])

                if creator_id == user.id:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ خودت سازنده این بازی هستی.",
                        show_alert=True
                    )
                    return

                amount = D(game["amount"])

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (user.id,)).fetchone()

                if not row or D(row["balance"]) < amount:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ موجودی کافی نیست.",
                        show_alert=True
                    )
                    return

                # قفل مبلغ حریف
                new_balance = D(row["balance"]) - amount

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(new_balance),
                    user.id
                ))

                con.execute("""
                UPDATE games
                SET opponent_id=?,
                    status='creator_turn'
                WHERE id=? AND status='waiting'
                """, (
                    user.id,
                    game_id
                ))

                if con.total_changes < 1:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ بازی هم‌زمان توسط شخص دیگری گرفته شد.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception("JOIN FRIEND ERROR")

                await query.answer(
                    "❌ خطا؛ دوباره تلاش کن.",
                    show_alert=True
                )
                return

    await query.answer("✅ وارد بازی شدی.")

    # حذف دکمه‌ها
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"👥 بازی شروع شد.\n\n"
        f"👤 سازنده، {game['creator_id']}، "
        f"ابتدا خودش {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد.\n\n"
        f"بعد از تمام شدن پرتاب‌های سازنده، "
        f"حریف نوبت خودش را انجام می‌دهد."
    )


# ============================================================
# BOT GAME
# ============================================================

async def join_bot(update, context):

    global BOT_BUSY

    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.split("_", 1)[1])
    except Exception:
        await query.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    ensure_user(user)

    async with BOT_GAME_LOCK:

        if BOT_BUSY:
            # بازی هنوز waiting است؛ مبلغ نزد کاربر رزرو شده.
            # پس مبلغ را برمی‌گردانیم و بازی را لغو می‌کنیم.
            game = get_game(game_id)

            if (
                game
                and game["status"] == "waiting"
                and int(game["creator_id"]) == user.id
            ):
                amount = D(game["amount"])

                with closing(db()) as con:
                    con.execute("BEGIN IMMEDIATE")

                    changed = con.execute("""
                    UPDATE games
                    SET status='cancelled'
                    WHERE id=? AND status='waiting'
                    """, (game_id,)).rowcount

                    con.execute("COMMIT")

                if changed:
                    credit_balance(
                        user.id,
                        amount
                    )

                    try:
                        await query.message.edit_reply_markup(
                            reply_markup=None
                        )
                    except Exception:
                        pass

                    await query.answer(
                        "❌ ربات در حال بازی است؛ مبلغ شما برگشت داده شد.",
                        show_alert=True
                    )

                    try:
                        await query.message.reply_to_message
                    except Exception:
                        pass

                    await query.message.reply_text(
                        f"↩️ {name_of(user)}، ربات درحال بازی است.\n"
                        f"💰 مبلغ {money(amount)} TRX شما برگشت داده شد.",
                        reply_to_message_id=query.message.message_id
                    )

                    return

            await query.answer(
                "❌ ربات درحال بازی است.",
                show_alert=True
            )
            return

        game = get_game(game_id)

        if not game:
            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ این بازی دیگر قابل ورود نیست.",
                show_alert=True
            )
            return

        if int(game["creator_id"]) != user.id:
            await query.answer(
                "❌ فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
                show_alert=True
            )
            return

        # فعال شدن قفل ربات
        BOT_BUSY = True

        amount = D(game["amount"])

        try:
            with closing(db()) as con:
                con.execute("BEGIN IMMEDIATE")

                changed = con.execute("""
                UPDATE games
                SET status='bot_creator_turn'
                WHERE id=?
                  AND status='waiting'
                  AND creator_id=?
                """, (
                    game_id,
                    user.id
                )).rowcount

                if changed != 1:
                    con.execute("ROLLBACK")
                    BOT_BUSY = False

                    await query.answer(
                        "❌ بازی هم‌زمان تغییر کرد.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

        except Exception:
            BOT_BUSY = False

            await query.answer(
                "❌ خطا در شروع بازی.",
                show_alert=True
            )
            return

        await query.answer("✅ بازی با ربات شروع شد.")

        try:
            await query.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        await query.message.reply_text(
            f"🤖 بازی با ربات شروع شد.\n\n"
            f"👤 {name_of(user)}، ابتدا خودت "
            f"{game['rounds']} بار "
            f"{GAME_EMOJIS[game['game_type']]} بفرست.\n\n"
            f"بعد از تمام شدن پرتاب‌های تو، "
            f"ربات خودش {game['rounds']} بار می‌اندازد."
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.split("_", 1)[1])
    except Exception:
        await query.answer("❌ بازی نامعتبر است.", show_alert=True)
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute(
                    "SELECT * FROM games WHERE id=?",
                    (game_id,)
                ).fetchone()

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
                        "❌ بازی شروع شده و دیگر قابل لغو نیست.",
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

                changed = con.execute("""
                UPDATE games
                SET status='cancelled'
                WHERE id=? AND status='waiting'
                """, (game_id,)).rowcount

                if changed != 1:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ بازی قبلاً تغییر کرده.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                await query.answer(
                    "❌ خطا.",
                    show_alert=True
                )
                return

    amount = D(game["amount"])

    credit_balance(
        user.id,
        amount
    )

    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.answer("✅ بازی لغو شد.")

    await query.message.reply_text(
        f"❌ بازی لغو شد.\n"
        f"💰 {money(amount)} TRX برگشت داده شد."
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def game_action_callback(update, context):

    data = update.callback_query.data

    if data.startswith("join_"):
        await join_friend(update, context)
        return

    if data.startswith("bot_"):
        await join_bot(update, context)
        return

    if data.startswith("cancel_"):
        await cancel_game(update, context)
        return


# ============================================================
# ACTIVE GAME FIND
# ============================================================

def find_active_game(chat_id, user_id):

    with closing(db()) as con:

        rows = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
          AND status IN (
              'bot_creator_turn',
              'creator_turn',
              'opponent_turn'
          )
        ORDER BY id DESC
        LIMIT 100
        """, (
            chat_id,
        )).fetchall()

    for game in rows:

        creator = int(game["creator_id"])

        opponent = (
            int(game["opponent_id"])
            if game["opponent_id"] is not None
            else None
        )

        if game["status"] in (
            "bot_creator_turn",
            "creator_turn"
        ):
            if creator == user_id:
                return game

        elif game["status"] == "opponent_turn":
            if opponent == user_id:
                return game

    return None


# ============================================================
# ROLL VALIDATION
# ============================================================

def valid_game_dice(message, game_type):

    if not message.dice:
        return False

    return message.dice.emoji == GAME_EMOJIS[game_type]


# ============================================================
# FINISH FRIEND
# ============================================================

async def finish_friend_game(
    context,
    game,
    creator_rolls,
    opponent_rolls
):

    creator_id = int(game["creator_id"])
    opponent_id = int(game["opponent_id"])

    amount = D(game["amount"])

    creator_score = sum(creator_rolls)
    opponent_score = sum(opponent_rolls)

    # مساوی
    if creator_score == opponent_score:

        async with DB_LOCK:
            with closing(db()) as con:
                con.execute("BEGIN IMMEDIATE")

                # فقط یک بار finished شدن
                changed = con.execute("""
                UPDATE games
                SET status='finished',
                    winner_id=NULL
                WHERE id=?
                  AND status='opponent_turn'
                """, (
                    game["id"],
                )).rowcount

                if changed:
                    con.execute("COMMIT")
                else:
                    con.execute("ROLLBACK")
                    return

        credit_balance(creator_id, amount)
        credit_balance(opponent_id, amount)

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🤝 نتیجه بازی\n\n"
                f"👤 {creator_id}: {creator_score}\n"
                f"👤 {opponent_id}: {opponent_score}\n\n"
                "🤝 مساوی شد.\n"
                f"💰 {money(amount)} TRX به هر نفر برگشت داده شد."
            )
        )

        return

    if creator_score > opponent_score:
        winner_id = creator_id
        winner_score = creator_score
        loser_score = opponent_score
    else:
        winner_id = opponent_id
        winner_score = opponent_score
        loser_score = creator_score

    payout = (
        amount * Decimal("2")
        - OWNER_SHARE
        - BOT_FEE
    )

    async with DB_LOCK:
        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                changed = con.execute("""
                UPDATE games
                SET status='finished',
                    winner_id=?
                WHERE id=?
                  AND status='opponent_turn'
                """, (
                    winner_id,
                    game["id"]
                )).rowcount

                if changed != 1:
                    con.execute("ROLLBACK")
                    return

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (winner_id,)).fetchone()

                if not row:
                    con.execute("ROLLBACK")
                    return

                new_balance = D(row["balance"]) + payout

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(new_balance),
                    winner_id
                ))

                house = con.execute("""
                SELECT owner_balance, fee_balance
                FROM house
                WHERE id=1
                """).fetchone()

                con.execute("""
                UPDATE house
                SET owner_balance=?,
                    fee_balance=?
                WHERE id=1
                """, (
                    str(D(house["owner_balance"]) + OWNER_SHARE),
                    str(D(house["fee_balance"]) + BOT_FEE)
                ))

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception("FINISH FRIEND ERROR")
                return

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            "🏆 نتیجه بازی\n\n"
            f"👤 {creator_id}: {creator_score}\n"
            f"👤 {opponent_id}: {opponent_score}\n\n"
            f"🏆 برنده: {winner_id}\n"
            f"🎯 امتیاز برنده: {winner_score}\n"
            f"🎯 امتیاز حریف: {loser_score}\n\n"
            f"💰 دریافتی برنده: {money(payout)} TRX"
        )
    )


# ============================================================
# FINISH BOT
# ============================================================

async def finish_bot_game(
    context,
    game,
    user_rolls,
    bot_rolls
):

    global BOT_BUSY

    try:

        user_id = int(game["creator_id"])
        amount = D(game["amount"])

        user_score = sum(user_rolls)
        bot_score = sum(bot_rolls)

        # مساوی
        if user_score == bot_score:

            async with DB_LOCK:
                with closing(db()) as con:
                    try:
                        con.execute("BEGIN IMMEDIATE")

                        changed = con.execute("""
                        UPDATE games
                        SET status='finished',
                            winner_id=NULL,
                            opponent_rolls=?
                        WHERE id=?
                          AND status='bot_rolling'
                        """, (
                            ",".join(map(str, bot_rolls)),
                            game["id"]
                        )).rowcount

                        if changed != 1:
                            con.execute("ROLLBACK")
                            return

                        row = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (user_id,)).fetchone()

                        if not row:
                            con.execute("ROLLBACK")
                            return

                        new_balance = D(row["balance"]) + amount

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(new_balance),
                            user_id
                        ))

                        con.execute("COMMIT")

                    except Exception:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass

                        logger.exception("BOT DRAW ERROR")
                        return

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "🤝 نتیجه بازی\n\n"
                    f"👤 {user_id}: {user_score}\n"
                    f"🤖 ربات: {bot_score}\n\n"
                    "🤝 مساوی شد.\n"
                    f"💰 {money(amount)} TRX برگشت داده شد."
                )
            )

            return

        # کاربر برد
        if user_score > bot_score:

            payout = (
                amount * Decimal("2")
                - OWNER_SHARE
                - BOT_FEE
            )

            async with DB_LOCK:
                with closing(db()) as con:
                    try:
                        con.execute("BEGIN IMMEDIATE")

                        changed = con.execute("""
                        UPDATE games
                        SET status='finished',
                            winner_id=?,
                            opponent_rolls=?
                        WHERE id=?
                          AND status='bot_rolling'
                        """, (
                            user_id,
                            ",".join(map(str, bot_rolls)),
                            game["id"]
                        )).rowcount

                        if changed != 1:
                            con.execute("ROLLBACK")
                            return

                        row = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (user_id,)).fetchone()

                        if not row:
                            con.execute("ROLLBACK")
                            return

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(D(row["balance"]) + payout),
                            user_id
                        ))

                        house = con.execute("""
                        SELECT owner_balance, fee_balance
                        FROM house
                        WHERE id=1
                        """).fetchone()

                        con.execute("""
                        UPDATE house
                        SET owner_balance=?,
                            fee_balance=?
                        WHERE id=1
                        """, (
                            str(
                                D(house["owner_balance"])
                                + OWNER_SHARE
                            ),
                            str(
                                D(house["fee_balance"])
                                + BOT_FEE
                            )
                        ))

                        con.execute("COMMIT")

                    except Exception:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass

                        logger.exception("BOT USER WIN ERROR")
                        return

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "🏆 نتیجه بازی\n\n"
                    f"👤 {user_id}: {user_score}\n"
                    f"🤖 ربات: {bot_score}\n\n"
                    f"🏆 برنده: {user_id}\n"
                    f"🎯 امتیاز برنده: {user_score}\n\n"
                    f"💰 دریافتی: {money(payout)} TRX"
                )
            )

            return

        # ربات برد
        async with DB_LOCK:
            with closing(db()) as con:
                try:
                    con.execute("BEGIN IMMEDIATE")

                    changed = con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        opponent_rolls=?
                    WHERE id=?
                      AND status='bot_rolling'
                    """, (
                        ",".join(map(str, bot_rolls)),
                        game["id"]
                    )).rowcount

                    if changed != 1:
                        con.execute("ROLLBACK")
                        return

                    house = con.execute("""
                    SELECT owner_balance, fee_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?,
                        fee_balance=?
                    WHERE id=1
                    """, (
                        str(
                            D(house["owner_balance"])
                            + amount
                        ),
                        str(
                            D(house["fee_balance"])
                        )
                    ))

                    con.execute("COMMIT")

                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception("BOT WIN ERROR")
                    return

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🏆 نتیجه بازی\n\n"
                f"👤 {user_id}: {user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                "🏆 برنده: 🤖 ربات"
            )
        )

    finally:
        BOT_BUSY = False


# ============================================================
# PROCESS ROLLS
# ============================================================

async def process_user_roll(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user or not message.dice:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    game = find_active_game(
        message.chat_id,
        user.id
    )

    if not game:
        return

    game_type = game["game_type"]

    if not valid_game_dice(
        message,
        game_type
    ):
        await message.reply_text(
            f"❌ برای این بازی باید "
            f"{GAME_EMOJIS[game_type]} بفرستی.",
            reply_to_message_id=message.message_id
        )
        return

    game_id = int(game["id"])
    rounds = int(game["rounds"])

    creator_rolls = parse_rolls(
        game["creator_rolls"]
    )

    opponent_rolls = parse_rolls(
        game["opponent_rolls"]
    )

    value = int(message.dice.value)

    # ========================================================
    # BOT GAME - USER
    # ========================================================

    if game["status"] == "bot_creator_turn":

        if user.id != int(game["creator_id"]):
            return

        if len(creator_rolls) >= rounds:
            return

        creator_rolls.append(value)

        if len(creator_rolls) < rounds:

            save_rolls(
                game_id,
                creator_rolls,
                []
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {value}\n"
                f"🎯 {rounds - len(creator_rolls)} پرتاب باقی مانده.",
                reply_to_message_id=message.message_id
            )

            return

        # کاربر تمام کرد
        save_rolls(
            game_id,
            creator_rolls,
            [],
            "bot_rolling"
        )

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            f"🤖 حالا ربات {rounds} بار "
            f"{GAME_EMOJIS[game_type]} می‌اندازد.",
            reply_to_message_id=message.message_id
        )

        bot_rolls = []

        try:

            # ضد باگ: قبل از هر پرتاب وضعیت را چک می‌کنیم
            current = get_game(game_id)

            if not current or current["status"] != "bot_rolling":
                return

            for i in range(rounds):

                current = get_game(game_id)

                if not current or current["status"] != "bot_rolling":
                    return

                sent = await context.bot.send_dice(
                    chat_id=message.chat_id,
                    emoji=GAME_EMOJIS[game_type]
                )

                bot_rolls.append(
                    int(sent.dice.value)
                )

                # برای جلوگیری از فشار
                await asyncio.sleep(0.7)

            current = get_game(game_id)

            if not current or current["status"] != "bot_rolling":
                return

            await finish_bot_game(
                context,
                current,
                creator_rolls,
                bot_rolls
            )

        except Exception:

            logger.exception(
                "BOT ROLL ERROR"
            )

            # اگر ارسال ربات شکست خورد:
            # بازی را اتمیک تمام می‌کنیم و مبلغ کاربر را برمی‌گردانیم.
            try:
                async with DB_LOCK:

                    with closing(db()) as con:
                        con.execute("BEGIN IMMEDIATE")

                        changed = con.execute("""
                        UPDATE games
                        SET status='error_refunded'
                        WHERE id=?
                          AND status='bot_rolling'
                        """, (
                            game_id,
                        )).rowcount

                        if changed == 1:

                            row = con.execute("""
                            SELECT balance
                            FROM users
                            WHERE user_id=?
                            """, (user.id,)).fetchone()

                            if row:
                                con.execute("""
                                UPDATE users
                                SET balance=?
                                WHERE user_id=?
                                """, (
                                    str(
                                        D(row["balance"])
                                        + D(game["amount"])
                                    ),
                                    user.id
                                ))

                        con.execute("COMMIT")

                await message.reply_text(
                    f"⚠️ بازی ربات دچار خطا شد.\n"
                    f"💰 {money(game['amount'])} TRX "
                    f"به موجودی شما برگشت داده شد.",
                    reply_to_message_id=message.message_id
                )

            except Exception:
                logger.exception(
                    "BOT REFUND ERROR"
                )

        return

    # ========================================================
    # FRIEND - CREATOR
    # ========================================================

    if game["status"] == "creator_turn":

        if user.id != int(game["creator_id"]):
            return

        if len(creator_rolls) >= rounds:
            return

        creator_rolls.append(value)

        if len(creator_rolls) < rounds:

            save_rolls(
                game_id,
                creator_rolls,
                []
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {value}\n"
                f"🎯 {rounds - len(creator_rolls)} پرتاب باقی مانده.",
                reply_to_message_id=message.message_id
            )

            return

        save_rolls(
            game_id,
            creator_rolls,
            [],
            "opponent_turn"
        )

        opponent_id = int(game["opponent_id"])

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            f"🎯 حالا حریف، {opponent_id}، "
            f"خودش {rounds} بار "
            f"{GAME_EMOJIS[game_type]} بفرستد.",
            reply_to_message_id=message.message_id
        )

        return

    # ========================================================
    # FRIEND - OPPONENT
    # ========================================================

    if game["status"] == "opponent_turn":

        opponent_id = int(game["opponent_id"])

        if user.id != opponent_id:
            return

        if len(opponent_rolls) >= rounds:
            return

        opponent_rolls.append(value)

        if len(opponent_rolls) < rounds:

            save_rolls(
                game_id,
                creator_rolls,
                opponent_rolls
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {value}\n"
                f"🎯 {rounds - len(opponent_rolls)} پرتاب باقی مانده.",
                reply_to_message_id=message.message_id
            )

            return

        save_rolls(
            game_id,
            creator_rolls,
            opponent_rolls,
            "finishing"
        )

        current = get_game(game_id)

        if not current:
            return

        await finish_friend_game(
            context,
            current,
            creator_rolls,
            opponent_rolls
        )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.effective_message
    user = update.effective_user

    if not message.reply_to_message:
        await message.reply_text(
            "💸 روی پیام کاربر Reply کن.\n\n"
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

    amount = parse_amount(message.text)

    if amount is None:
        await message.reply_text(
            "❌ مقدار نامعتبر.\n"
            "مثال: انتقال 0.5"
        )
        return

    ensure_user(target)

    async with DB_LOCK:

        success = transfer_atomic(
            user.id,
            target.id,
            amount
        )

    if not success:
        await message.reply_text(
            "❌ انتقال انجام نشد؛ "
            "موجودی کافی نیست یا تراکنش تغییر کرده."
        )
        return

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💰 مقدار: {money(amount)} TRX"
    )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(update, context):

    user = update.effective_user

    ensure_user(user)

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5\n\n"
        "بعد اطلاعات موردنیاز را ارسال کن."
    )

    context.user_data["request_mode"] = "amount"


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
# HELP
# ============================================================

async def help_command(update, context):

    await update.effective_message.reply_text(
        "📖 راهنما\n\n"

        "🎮 ساخت بازی در گپ:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"

        "👥 بازی دوستان:\n"
        "سازنده اول خودش ایموجی بازی را می‌فرستد؛ "
        "بعد حریف خودش می‌فرستد.\n\n"

        "🤖 بازی با ربات:\n"
        "اول خود کاربر ایموجی بازی را می‌فرستد؛ "
        "بعد ربات خودش می‌اندازد.\n\n"

        "💰 موجودی\n"
        "💸 انتقال مقدار ← با Reply\n"
        "📤 درخواست\n\n"

        "👥 هر Ref: 0.05 TRX\n\n"

        "⚠️ TRX نمایش‌داده‌شده در این ربات "
        "اعتبار داخلی بازی است و TRX واقعی شبکه TRON نیست."
    )


# ============================================================
# REF COMMAND
# ============================================================

async def ref_command(update, context):

    user = update.effective_user

    ensure_user(user)

    me = await context.bot.get_me()

    link = (
        f"https://t.me/{me.username}"
        f"?start=ref_{user.id}"
    )

    await update.effective_message.reply_text(
        "👥 لینک زیرمجموعه شما:\n\n"
        f"{link}\n\n"
        f"🎁 پاداش هر Ref: {money(REF_REWARD)} TRX"
    )


# ============================================================
# ADMIN
# ============================================================

async def admin(update, context):

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
    except Exception:
        await update.message.reply_text(
            "❌ ID نامعتبر."
        )
        return

    amount = parse_decimal_amount(
        normalize_digits(context.args[1])
    )

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
        f"✅ انجام شد.\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)} TRX\n"
        f"💰 موجودی جدید: {money(get_balance(target_id))} TRX"
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
    except Exception:
        await update.message.reply_text(
            "❌ ID نامعتبر."
        )
        return

    amount = parse_decimal_amount(
        normalize_digits(context.args[1])
    )

    if amount is None:
        await update.message.reply_text(
            "❌ مقدار نامعتبر."
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
        f"✅ انجام شد.\n"
        f"👤 {target_id}\n"
        f"➖ {money(amount)} TRX"
    )


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

            text += (
                f"👤 {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {money(row['balance'])} TRX\n"
                f"{'🚫 مسدود' if row['blocked'] else '✅ فعال'}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )
        return

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            games = con.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

            active = con.execute("""
            SELECT COUNT(*)
            FROM games
            WHERE status IN (
                'waiting',
                'creator_turn',
                'opponent_turn',
                'bot_creator_turn',
                'bot_rolling'
            )
            """).fetchone()[0]

            total = con.execute("""
            SELECT SUM(CAST(balance AS REAL))
            FROM users
            """).fetchone()[0] or 0

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"🎮 کل بازی‌ها: {games:,}\n"
            f"🔥 بازی‌های فعال: {active:,}\n"
            f"💰 مجموع موجودی: {money(total)} TRX"
        )
        return

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 5"
        )
        return

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT"
        )
        return

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
                "📋 درخواستی وجود ندارد."
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

    # --------------------------------------------------------
    # REQUEST MODE
    # --------------------------------------------------------

    request_mode = context.user_data.get(
        "request_mode"
    )

    if request_mode == "wallet":

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

        game, rounds, amount = parsed

        await create_game(
            update,
            context,
            game,
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
        "balance"
    ):

        await show_balance(
            update,
            context
        )
        return

    # --------------------------------------------------------
    # GAME MENU
    # --------------------------------------------------------

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )
        return

    # --------------------------------------------------------
    # FRIEND
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

        amount = parse_amount(
            normalized
        )

        if amount:

            context.user_data["request_amount"] = amount
            context.user_data["request_mode"] = "wallet"

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
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("balance", show_balance)
    )

    application.add_handler(
        CommandHandler("game", game_menu)
    )

    application.add_handler(
        CommandHandler("friends", friends_menu)
    )

    application.add_handler(
        CommandHandler("ref", ref_command)
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

    # --------------------------------------------------------
    # GAME MENU BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    # --------------------------------------------------------
    # GAME ACTION BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_action_callback,
            pattern=r"^(join_|bot_|cancel_)"
        )
    )

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # --------------------------------------------------------
    # REAL TELEGRAM GAME EMOJIS
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
