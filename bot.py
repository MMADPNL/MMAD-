import os
import sqlite3
import secrets
import logging
from contextlib import closing

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 8552447077

DB_FILE = "BET_BT.db"

# هر رفرال
REFERRAL_REWARD = 0.05

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


def init_db():
    with closing(get_db()) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                points REAL NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referral_points REAL NOT NULL DEFAULT 0,
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

                rounds INTEGER NOT NULL,
                entry_points REAL NOT NULL,

                status TEXT NOT NULL DEFAULT 'waiting',

                creator_total INTEGER NOT NULL DEFAULT 0,
                opponent_total INTEGER NOT NULL DEFAULT 0,

                creator_round INTEGER NOT NULL DEFAULT 0,
                opponent_round INTEGER NOT NULL DEFAULT 0,

                robot_game INTEGER NOT NULL DEFAULT 0,

                settled INTEGER NOT NULL DEFAULT 0,

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
            INSERT OR IGNORE INTO settings(
                key,
                value
            )
            VALUES(
                'enabled',
                '1'
            )
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
            VALUES(
                ?,
                ?,
                ?
            )

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
        """, (
            user_id,
        )).fetchone()


def get_points(user_id):

    row = get_user(user_id)

    if not row:
        return 0.0

    return float(row["points"])


def fmt(value):

    text = f"{float(value):.8f}"

    text = text.rstrip("0").rstrip(".")

    return text if text else "0"


def normalize(value):

    return str(value).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )


def is_owner(user_id):

    return int(user_id) == OWNER_ID


def is_enabled():

    with closing(get_db()) as conn:

        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key='enabled'
        """).fetchone()

    if not row:
        return True

    return row["value"] == "1"


# ============================================================
# POINTS
# ============================================================

def change_points(
    user_id,
    amount,
    reason="change",
):

    amount = round(
        float(amount),
        8,
    )

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            row = conn.execute("""
                SELECT points
                FROM users
                WHERE user_id=?
            """, (
                user_id,
            )).fetchone()

            if not row:

                conn.rollback()

                return False

            old_balance = float(
                row["points"]
            )

            new_balance = round(
                old_balance + amount,
                8,
            )

            if new_balance < 0:

                conn.rollback()

                return False

            conn.execute("""
                UPDATE users
                SET points=?
                WHERE user_id=?
            """, (
                new_balance,
                user_id,
            ))

            conn.commit()

            return True

        except Exception:

            conn.rollback()

            logger.exception(
                "points error: %s",
                reason,
            )

            return False


# ============================================================
# REFERRAL
# ============================================================

def process_referral(
    new_user_id,
    referrer_id,
):

    if new_user_id == referrer_id:
        return False

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            new_user = conn.execute("""
                SELECT referred_by
                FROM users
                WHERE user_id=?
            """, (
                new_user_id,
            )).fetchone()

            referrer = conn.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (
                referrer_id,
            )).fetchone()

            if not new_user or not referrer:

                conn.rollback()

                return False

            if new_user["referred_by"] is not None:

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
                    referral_points=
                        referral_points+?,
                    points=points+?
                WHERE user_id=?
            """, (
                REFERRAL_REWARD,
                REFERRAL_REWARD,
                referrer_id,
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
            )
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

    return InlineKeyboardMarkup(
        buttons
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if context.args:

        try:

            referrer_id = int(
                normalize(
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

        "💰 موجودی اولیه: 0\n\n"

        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"

        "از دکمه‌های زیر استفاده کن.",

        reply_markup=main_keyboard(
            user.id
        ),
    )


# ============================================================
# BALANCE
# ============================================================

async def balance_button(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    ensure_user(user)

    await query.answer()

    await query.message.reply_text(

        "💰 موجودی شما:\n\n"

        f"🪙 {fmt(get_points(user.id))}"
    )


# ============================================================
# REFERRALS
# ============================================================

async def referrals_button(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    ensure_user(user)

    row = get_user(user.id)

    try:

        bot = await context.bot.get_me()

        link = (
            f"https://t.me/"
            f"{bot.username}"
            f"?start={user.id}"
        )

    except Exception:

        link = "خطا در ساخت لینک"

    await query.answer()

    await query.message.reply_text(

        "👥 زیرمجموعه\n\n"

        f"👤 تعداد رفرال: "
        f"{row['referrals']}\n"

        f"🎁 هر رفرال: "
        f"{fmt(REFERRAL_REWARD)}\n"

        f"💰 دریافتی: "
        f"{fmt(row['referral_points'])}\n\n"

        f"🔗 لینک دعوت:\n"
        f"{link}"
    )


# ============================================================
# EXAMPLES
# ============================================================

async def examples_button(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(

        "🎮 مثال بازی\n\n"

        "🎲 4 تاس 0.1\n"
        "🎲 10 تاس 0.1\n"
        "🎲 100 تاس 0.1\n\n"

        "🎯 4 دارت 0.1\n"
        "🎯 10 دارت 0.1\n\n"

        "🏀 4 بسکتبال 0.1\n"
        "🏀 10 بسکتبال 0.1\n\n"

        "🎳 4 بولینگ 0.1\n"
        "🎳 10 بولینگ 0.1\n\n"

        "اعداد فارسی هم قبول می‌شوند:\n"
        "4 تاس ۰.۱\n"
        "10 دارت ۰.۱\n\n"

        "🔢 تعداد راند نامحدود است."
    )


# ============================================================
# TRANSFER INFO
# ============================================================

async def transfer_button(
    update,
    context,
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(

        "🔄 انتقال امتیاز\n\n"

        "روی پیام کاربر Reply کن و بنویس:\n\n"

        "انتقال 10\n\n"

        "مثال:\n"
        "انتقال 0.1"
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_handler(
    update,
    context,
):

    message = update.message

    sender = update.effective_user

    if not message.reply_to_message:
        return

    text = message.text.strip()

    if not text.startswith("انتقال"):
        return

    target = (
        message.reply_to_message.from_user
    )

    if not target:
        return

    if target.is_bot:
        await message.reply_text(
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )
        return

    if target.id == sender.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return

    ensure_user(sender)
    ensure_user(target)

    parts = text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 10"
        )

        return

    try:

        amount = float(
            normalize(parts[1])
        )

    except Exception:

        await message.reply_text(
            "❌ مقدار نامعتبر است."
        )

        return

    amount = round(
        amount,
        8,
    )

    if amount <= 0:

        await message.reply_text(
            "❌ مقدار باید بیشتر از صفر باشد."
        )

        return

    if get_points(sender.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    reference = (
        "transfer_"
        + secrets.token_hex(16)
    )

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            sender_row = conn.execute("""
                SELECT points
                FROM users
                WHERE user_id=?
            """, (
                sender.id,
            )).fetchone()

            target_row = conn.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (
                target.id,
            )).fetchone()

            if not sender_row or not target_row:

                conn.rollback()

                return

            sender_points = float(
                sender_row["points"]
            )

            if sender_points < amount:

                conn.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            conn.execute("""
                UPDATE users
                SET points=points-?
                WHERE user_id=?
            """, (
                amount,
                sender.id,
            ))

            conn.execute("""
                UPDATE users
                SET points=points+?
                WHERE user_id=?
            """, (
                amount,
                target.id,
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

        f"👤 گیرنده: "
        f"{target.full_name}\n"

        f"🪙 مقدار: "
        f"{fmt(amount)}"
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_TYPES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳",
}


def parse_game(text):

    parts = text.strip().split()

    if len(parts) != 3:
        return None

    try:

        rounds = int(
            normalize(parts[0])
        )

    except Exception:

        return None

    if rounds <= 0:
        return None

    game_type = parts[1]

    if game_type not in GAME_TYPES:
        return None

    try:

        amount = float(
            normalize(parts[2])
        )

    except Exception:

        return None

    amount = round(
        amount,
        8,
    )

    if amount <= 0:
        return None

    return (
        rounds,
        game_type,
        GAME_TYPES[game_type],
        amount,
    )


# ============================================================
# ACTIVE GAME
# ============================================================

def active_game(user_id):

    with closing(get_db()) as conn:

        return conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting',
                'waiting_creator',
                'playing'
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


# ============================================================
# GAME BUTTONS
# ============================================================

def game_buttons(game_id):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=
                f"join:{game_id}",
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=
                f"robot:{game_id}",
            )
        ],

        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=
                f"cancel:{game_id}",
            )
        ],
    ])


# ============================================================
# CREATE GAME
# ============================================================

async def game_command(
    update,
    context,
):

    message = update.message

    user = update.effective_user

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    parsed = parse_game(
        message.text
    )

    if not parsed:
        return

    if not is_enabled():
        return

    rounds, game_type, emoji, amount = parsed

    ensure_user(user)

    if active_game(user.id):

        await message.reply_text(
            "❌ شما در حال بازی هستید.\n"
            "ابتدا بازی قبلی تمام شود."
        )

        return

    # امتیاز ورود
    if not change_points(
        user.id,
        -amount,
        "game_entry",
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{fmt(get_points(user.id))}\n"
            f"🪙 مقدار ورود: "
            f"{fmt(amount)}"
        )

        return

    game_id = secrets.token_hex(12)

    with closing(get_db()) as conn:

        try:

            conn.execute("""
                INSERT INTO games(
                    game_id,
                    chat_id,
                    creator_id,
                    game_type,
                    emoji,
                    rounds,
                    entry_points,
                    status
                )
                VALUES(
                    ?, ?, ?, ?, ?, ?, ?, 'waiting'
                )
            """, (
                game_id,
                message.chat.id,
                user.id,
                game_type,
                emoji,
                rounds,
                amount,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_points(
                user.id,
                amount,
                "create_game_refund",
            )

            await message.reply_text(
                "❌ ساخت بازی ناموفق بود."
            )

            return

    await message.reply_text(

        f"{emoji} بازی ساخته شد.\n\n"

        f"🎮 بازی: {game_type}\n"
        f"🔢 تعداد راند: {rounds}\n"
        f"🪙 ورود هر بازیکن: "
        f"{fmt(amount)}\n\n"

        "👥 بازی با دوستان یا\n"
        "🤖 بازی با ربات را انتخاب کن.\n\n"

        f"در هر راند خود بازیکن "
        f"{emoji} را می‌فرستد.",

        reply_markup=game_buttons(
            game_id
        ),
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1,
    )[1]

    with closing(get_db()) as conn:

        game = conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
        """, (
            game_id,
        )).fetchone()

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True,
        )

        return

    if game["creator_id"] == user.id:

        await query.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True,
        )

        return

    if active_game(user.id):

        await query.answer(
            "❌ شما در حال بازی هستید.",
            show_alert=True,
        )

        return

    ensure_user(user)

    amount = float(
        game["entry_points"]
    )

    if not change_points(
        user.id,
        -amount,
        "game_join",
    ):

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            current = conn.execute("""
                SELECT status, settled
                FROM games
                WHERE game_id=?
            """, (
                game_id,
            )).fetchone()

            if (
                not current
                or current["status"] != "waiting"
                or current["settled"]
            ):

                conn.rollback()

                change_points(
                    user.id,
                    amount,
                    "join_refund",
                )

                await query.answer(
                    "❌ بازی پر شده.",
                    show_alert=True,
                )

                return

            conn.execute("""
                UPDATE games
                SET
                    opponent_id=?,
                    status='playing'
                WHERE game_id=?
            """, (
                user.id,
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_points(
                user.id,
                amount,
                "join_error_refund",
            )

            await query.answer(
                "❌ خطا.",
                show_alert=True,
            )

            return

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.edit_message_text(

        f"{game['emoji']} بازی شروع شد.\n\n"

        f"👤 بازیکن اول: "
        f"{game['creator_id']}\n"

        f"👤 بازیکن دوم: "
        f"{user.full_name}\n"

        f"🔢 راندها: "
        f"{game['rounds']}\n\n"

        f"هر بازیکن خودش "
        f"{game['emoji']} را می‌فرستد."
    )


# ============================================================
# ROBOT
# ============================================================

async def robot_game(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1,
    )[1]

    with closing(get_db()) as conn:

        game = conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
        """, (
            game_id,
        )).fetchone()

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده بازی.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی قابل شروع نیست.",
            show_alert=True,
        )

        return

    with closing(get_db()) as conn:

        conn.execute("""
            UPDATE games
            SET
                robot_game=1,
                opponent_id=0,
                status='waiting_creator'
            WHERE game_id=?
        """, (
            game_id,
        ))

        conn.commit()

    await query.answer()

    await query.edit_message_text(

        f"🤖 بازی با ربات\n\n"

        f"🎮 {game['game_type']}\n"
        f"🔢 تعداد راند: "
        f"{game['rounds']}\n"
        f"🪙 ورود: "
        f"{fmt(game['entry_points'])}\n\n"

        f"اول خودت "
        f"{game['emoji']} را بفرست.\n"

        "بعد ربات پرتاب می‌کند."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1,
    )[1]

    with closing(get_db()) as conn:

        game = conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
        """, (
            game_id,
        )).fetchone()

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده.",
            show_alert=True,
        )

        return

    if game["status"] not in (
        "waiting",
        "waiting_creator",
    ):

        await query.answer(
            "❌ قابل لغو نیست.",
            show_alert=True,
        )

        return

    with closing(get_db()) as conn:

        conn.execute("""
            UPDATE games
            SET
                status='cancelled',
                settled=1
            WHERE game_id=?
            AND settled=0
        """, (
            game_id,
        ))

        conn.commit()

    # مبلغ سازنده برگشت
    change_points(
        user.id,
        float(game["entry_points"]),
        "cancel_refund",
    )

    await query.answer(
        "❌ بازی لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        "💰 امتیاز ورود برگشت داده شد."
    )


# ============================================================
# DICE HANDLER
# ============================================================

async def game_dice_handler(
    update,
    context,
):

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

    emoji = message.dice.emoji

    if emoji not in (
        "🎲",
        "🎯",
        "🏀",
        "🎳",
    ):
        return

    user = update.effective_user

    ensure_user(user)

    with closing(get_db()) as conn:

        game = conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting_creator',
                'playing'
            )
            AND emoji=?
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY created_at DESC
            LIMIT 1
        """, (
            emoji,
            user.id,
            user.id,
        )).fetchone()

    if not game:
        return

    value = int(
        message.dice.value
    )

    game_id = game["game_id"]

    # ========================================================
    # ROBOT GAME
    # ========================================================

    if game["robot_game"]:

        if user.id != game["creator_id"]:
            return

        if game["status"] != "waiting_creator":
            return

        current_round = (
            int(game["creator_round"])
            + 1
        )

        creator_total = (
            int(game["creator_total"])
            + value
        )

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    creator_round=?,
                    creator_total=?
                WHERE game_id=?
                AND settled=0
            """, (
                current_round,
                creator_total,
                game_id,
            ))

            conn.commit()

        if current_round > int(
            game["rounds"]
        ):

            await finish_robot_game(
                game_id,
                context,
            )

            return

        await message.reply_text(
            f"✅ راند {current_round} ثبت شد.\n"
            f"🎯 امتیاز این پرتاب: {value}\n"
            f"📊 مجموع تو: {creator_total}\n\n"
            "🤖 ربات پرتاب می‌کند..."
        )

        try:

            robot = await context.bot.send_dice(
                chat_id=message.chat.id,
                emoji=emoji,
            )

            robot_value = int(
                robot.dice.value
            )

        except Exception:

            await message.reply_text(
                "❌ پرتاب ربات انجام نشد."
            )

            return

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    opponent_round=opponent_round+1,
                    opponent_total=opponent_total+?
                WHERE game_id=?
                AND settled=0
            """, (
                robot_value,
                game_id,
            ))

            conn.commit()

        await message.reply_text(
            f"🤖 ربات: {robot_value}"
        )

        if current_round >= int(
            game["rounds"]
        ):

            await finish_robot_game(
                game_id,
                context,
            )

        return

    # ========================================================
    # FRIEND GAME
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

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    creator_round=
                        creator_round+1,
                    creator_total=
                        creator_total+?
                WHERE game_id=?
                AND settled=0
            """, (
                value,
                game_id,
            ))

            conn.commit()

        await message.reply_text(
            f"✅ پرتاب ثبت شد: {value}"
        )

    elif user.id == opponent_id:

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    opponent_round=
                        opponent_round+1,
                    opponent_total=
                        opponent_total+?
                WHERE game_id=?
                AND settled=0
            """, (
                value,
                game_id,
            ))

            conn.commit()

        await message.reply_text(
            f"✅ پرتاب ثبت شد: {value}"
        )

    else:
        return

    updated = get_game(
        game_id
    )

    if not updated:
        return

    if (
        int(updated["creator_round"])
        >= int(updated["rounds"])
        and
        int(updated["opponent_round"])
        >= int(updated["rounds"])
    ):

        await finish_friend_game(
            game_id,
            context,
        )


# ============================================================
# GET GAME
# ============================================================

def get_game(game_id):

    with closing(get_db()) as conn:

        return conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
        """, (
            game_id,
        )).fetchone()


# ============================================================
# FINISH ROBOT
# ============================================================

async def finish_robot_game(
    game_id,
    context,
):

    game = get_game(game_id)

    if not game or game["settled"]:
        return

    creator_total = int(
        game["creator_total"]
    )

    robot_total = int(
        game["opponent_total"]
    )

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            current = conn.execute("""
                SELECT settled
                FROM games
                WHERE game_id=?
            """, (
                game_id,
            )).fetchone()

            if not current or current["settled"]:

                conn.rollback()

                return

            conn.execute("""
                UPDATE games
                SET
                    status='finished',
                    settled=1
                WHERE game_id=?
                AND settled=0
            """, (
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            return

    amount = float(
        game["entry_points"]
    )

    # مساوی
    if creator_total == robot_total:

        change_points(
            game["creator_id"],
            amount,
            "robot_draw_refund",
        )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 تو: {creator_total}\n"
            f"🤖 ربات: {robot_total}\n\n"
            "🤝 مساوی شد.\n"
            "💰 امتیاز ورود برگشت داده شد."
        )

    elif creator_total > robot_total:

        # جایزه امتیازی برابر مجموع دو ورود
        reward = amount * 2

        change_points(
            game["creator_id"],
            reward,
            "robot_win",
        )

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            conn.commit()

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 تو: {creator_total}\n"
            f"🤖 ربات: {robot_total}\n\n"
            "🏆 برنده شدی!\n"
            f"💰 جایزه: {fmt(reward)}"
        )

    else:

        with closing(get_db()) as conn:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    losses=losses+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            conn.commit()

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 تو: {creator_total}\n"
            f"🤖 ربات: {robot_total}\n\n"
            "🤖 ربات برنده شد."
        )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text,
    )


# ============================================================
# FINISH FRIEND
# ============================================================

async def finish_friend_game(
    game_id,
    context,
):

    game = get_game(game_id)

    if not game or game["settled"]:
        return

    creator_total = int(
        game["creator_total"]
    )

    opponent_total = int(
        game["opponent_total"]
    )

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            current = conn.execute("""
                SELECT settled
                FROM games
                WHERE game_id=?
            """, (
                game_id,
            )).fetchone()

            if not current or current["settled"]:

                conn.rollback()

                return

            conn.execute("""
                UPDATE games
                SET
                    status='finished',
                    settled=1
                WHERE game_id=?
                AND settled=0
            """, (
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            return

    amount = float(
        game["entry_points"]
    )

    total_pool = amount * 2

    if creator_total == opponent_total:

        change_points(
            game["creator_id"],
            amount,
            "draw_creator",
        )

        change_points(
            game["opponent_id"],
            amount,
            "draw_opponent",
        )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_total}\n"
            f"👤 بازیکن دوم: {opponent_total}\n\n"
            "🤝 مساوی شد.\n"
            "💰 امتیاز ورود هر دو نفر برگشت داده شد."
        )

    elif creator_total > opponent_total:

        change_points(
            game["creator_id"],
            total_pool,
            "friend_win",
        )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_total}\n"
            f"👤 بازیکن دوم: {opponent_total}\n\n"
            "🏆 بازیکن اول برنده شد."
        )

    else:

        change_points(
            game["opponent_id"],
            total_pool,
            "friend_win",
        )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_total}\n"
            f"👤 بازیکن دوم: {opponent_total}\n\n"
            "🏆 بازیکن دوم برنده شد."
        )

    with closing(get_db()) as conn:

        if creator_total > opponent_total:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    losses=losses+1
                WHERE user_id=?
            """, (
                game["opponent_id"],
            ))

        elif opponent_total > creator_total:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    losses=losses+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (
                game["opponent_id"],
            ))

        else:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    draws=draws+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    draws=draws+1
                WHERE user_id=?
            """, (
                game["opponent_id"],
            ))

        conn.commit()

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text,
    )


# ============================================================
# ADMIN MENU
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
                callback_data="admin_on",
            ),

            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_off",
            ),
        ],

        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ],
    ])


async def admin_button(
    update,
    context,
):

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
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard(),
    )


async def admin_add_button(
    update,
    context,
):

    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()

    context.user_data[
        "admin_action"
    ] = "add"

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_remove_button(
    update,
    context,
):

    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()

    context.user_data[
        "admin_action"
    ] = "remove"

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_balance_button(
    update,
    context,
):

    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    context.user_data.clear()

    context.user_data[
        "admin_action"
    ] = "balance"

    await query.answer()

    await query.message.reply_text(
        "💰 آیدی عددی کاربر را بفرست."
    )


async def admin_stats_button(
    update,
    context,
):

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

        points = conn.execute("""
            SELECT COALESCE(
                SUM(points),
                0
            ) p
            FROM users
        """).fetchone()["p"]

    await query.answer()

    await query.message.reply_text(

        "📊 آمار\n\n"

        f"👥 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"💰 مجموع موجودی: "
        f"{fmt(points)}\n\n"

        f"🔌 وضعیت: "
        f"{'🟢 روشن' if is_enabled() else '🔴 خاموش'}"
    )


async def admin_on_button(
    update,
    context,
):

    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    with closing(get_db()) as conn:

        conn.execute("""
            INSERT INTO settings(
                key,
                value
            )
            VALUES(
                'enabled',
                '1'
            )

            ON CONFLICT(key)
            DO UPDATE SET value='1'
        """)

        conn.commit()

    await query.answer(
        "🟢 روشن شد."
    )

    await query.message.reply_text(
        "🟢 ربات روشن شد.",
        reply_markup=admin_keyboard(),
    )


async def admin_off_button(
    update,
    context,
):

    query = update.callback_query

    if not is_owner(
        update.effective_user.id
    ):
        return

    with closing(get_db()) as conn:

        conn.execute("""
            INSERT INTO settings(
                key,
                value
            )
            VALUES(
                'enabled',
                '0'
            )

            ON CONFLICT(key)
            DO UPDATE SET value='0'
        """)

        conn.commit()

    await query.answer(
        "🔴 خاموش شد."
    )

    await query.message.reply_text(
        "🔴 ربات خاموش شد.",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(
    update,
    context,
):

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

    # --------------------------------------------------------
    # USER BALANCE
    # --------------------------------------------------------

    if action == "balance":

        try:

            target_id = int(
                normalize(text)
            )

        except Exception:

            await message.reply_text(
                "❌ آیدی نامعتبر است."
            )

            return

        row = get_user(
            target_id
        )

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
            f"{fmt(row['points'])}\n"
            f"👥 رفرال: "
            f"{row['referrals']}\n"
            f"🎮 بازی: "
            f"{row['games']}\n"
            f"🏆 برد: "
            f"{row['wins']}\n"
            f"❌ باخت: "
            f"{row['losses']}\n"
            f"🤝 مساوی: "
            f"{row['draws']}"
        )

        context.user_data.clear()

        return

    # --------------------------------------------------------
    # ADD / REMOVE
    # --------------------------------------------------------

    if action not in (
        "add",
        "remove",
    ):
        return

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
            normalize(parts[0])
        )

        amount = float(
            normalize(parts[1])
        )

    except Exception:

        await message.reply_text(
            "❌ مقدار نامعتبر است."
        )

        return

    amount = round(
        amount,
        8,
    )

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not get_user(target_id):

        await message.reply_text(
            "❌ کاربر پیدا نشد.\n"
            "کاربر باید ابتدا /start بزند."
        )

        return

    if action == "add":

        success = change_points(
            target_id,
            amount,
            "admin_add",
        )

        title = "افزایش"

    else:

        success = change_points(
            target_id,
            -amount,
            "admin_remove",
        )

        title = "کسر"

    if not success:

        await message.reply_text(
            "❌ عملیات انجام نشد.\n"
            "ممکن است موجودی برای کسر کافی نباشد."
        )

        return

    await message.reply_text(

        f"✅ {title} انجام شد.\n\n"

        f"🆔 کاربر: {target_id}\n"
        f"🪙 مقدار: {fmt(amount)}\n"
        f"💰 موجودی جدید: "
        f"{fmt(get_points(target_id))}"
    )

    context.user_data.clear()


# ============================================================
# HOME
# ============================================================

async def home_button(
    update,
    context,
):

    query = update.callback_query

    user = update.effective_user

    await query.answer()

    await query.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(
            user.id
        ),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context,
):

    query = update.callback_query

    data = query.data

    if data == "balance":

        await balance_button(
            update,
            context,
        )

    elif data == "referrals":

        await referrals_button(
            update,
            context,
        )

    elif data == "examples":

        await examples_button(
            update,
            context,
        )

    elif data == "transfer":

        await transfer_button(
            update,
            context,
        )

    elif data == "admin":

        await admin_button(
            update,
            context,
        )

    elif data == "admin_add":

        await admin_add_button(
            update,
            context,
        )

    elif data == "admin_remove":

        await admin_remove_button(
            update,
            context,
        )

    elif data == "admin_balance":

        await admin_balance_button(
            update,
            context,
        )

    elif data == "admin_stats":

        await admin_stats_button(
            update,
            context,
        )

    elif data == "admin_on":

        await admin_on_button(
            update,
            context,
        )

    elif data == "admin_off":

        await admin_off_button(
            update,
            context,
        )

    elif data == "home":

        await home_button(
            update,
            context,
        )

    elif data.startswith("join:"):

        await join_game(
            update,
            context,
        )

    elif data.startswith("robot:"):

        await robot_game(
            update,
            context,
        )

    elif data.startswith("cancel:"):

        await cancel_game(
            update,
            context,
        )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(
    update,
    context,
):

    message = update.message

    if not message or not message.text:
        return

    text = message.text.strip()

    # ADMIN
    if (
        is_owner(
            update.effective_user.id
        )
        and context.user_data.get(
            "admin_action"
        )
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
    if parse_game(text):

        await game_command(
            update,
            context,
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context,
):

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

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # فقط /start
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # دکمه‌ها
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # تاس / دارت / بسکتبال / بولینگ
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_dice_handler,
        )
    )

    # متن‌ها
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
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
        allowed_updates=
        Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
