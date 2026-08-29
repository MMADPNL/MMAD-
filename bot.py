import os
import re
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

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB_FILE = "bet_bot.db"

REF_REWARD = 0.05

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referral_earned REAL NOT NULL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                draws INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER DEFAULT NULL,
                game_type TEXT NOT NULL,
                emoji TEXT NOT NULL,
                rounds INTEGER NOT NULL,
                stake REAL NOT NULL,

                creator_round INTEGER DEFAULT 0,
                opponent_round INTEGER DEFAULT 0,

                creator_score INTEGER DEFAULT 0,
                opponent_score INTEGER DEFAULT 0,

                robot INTEGER DEFAULT 0,
                status TEXT DEFAULT 'waiting',
                settled INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings(key,value)
            VALUES('enabled','1')
        """)

        conn.commit()


# ============================================================
# HELPERS
# ============================================================

def normalize(text):
    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    )


def fmt(value):
    value = round(float(value), 8)
    return (
        f"{value:.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


def ensure_user(user):
    if not user:
        return

    with closing(db()) as conn:
        conn.execute("""
            INSERT INTO users(
                user_id,
                name,
                username
            )
            VALUES(?,?,?)

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
    with closing(db()) as conn:
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


def change_balance(user_id, amount):
    amount = round(float(amount), 8)

    with closing(db()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                conn.rollback()
                return False

            new_balance = round(
                float(row["balance"]) + amount,
                8,
            )

            if new_balance < 0:
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

            conn.commit()
            return True

        except Exception:
            conn.rollback()
            log.exception("balance update error")
            return False


def bot_enabled():
    with closing(db()) as conn:
        row = conn.execute("""
            SELECT value
            FROM settings
            WHERE key='enabled'
        """).fetchone()

    return not row or row["value"] == "1"


def set_bot_enabled(value):
    with closing(db()) as conn:
        conn.execute("""
            INSERT INTO settings(key,value)
            VALUES('enabled',?)

            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (
            "1" if value else "0",
        ))

        conn.commit()


def get_game(game_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT *
            FROM games
            WHERE id=?
        """, (game_id,)).fetchone()


def active_game(user_id):
    with closing(db()) as conn:
        return conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting',
                'playing',
                'robot'
            )
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY rowid DESC
            LIMIT 1
        """, (
            user_id,
            user_id,
        )).fetchone()


# ============================================================
# JOIN
# ============================================================

async def is_joined(user_id, bot):
    try:
        member = await bot.get_chat_member(
            CHANNEL,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:
        log.warning(
            "join check failed: %s",
            e,
        )
        return False


def join_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="join_check",
            )
        ],
    ])


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard(user_id):

    rows = [
        [
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="ref",
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
            ),
        ],
    ]

    if user_id == OWNER_ID:
        rows.append([
            InlineKeyboardButton(
                "👑 پنل مدیریت",
                callback_data="admin",
            )
        ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# START
# ============================================================

async def start(update, context):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # ----------------------------
    # REFERRAL
    # ----------------------------

    if context.args:

        try:
            ref_id = int(
                normalize(context.args[0])
            )

            if (
                ref_id != user.id
                and get_user(ref_id)
            ):

                with closing(db()) as conn:

                    row = conn.execute("""
                        SELECT referred_by
                        FROM users
                        WHERE user_id=?
                    """, (
                        user.id,
                    )).fetchone()

                    if (
                        row
                        and row["referred_by"] is None
                    ):

                        conn.execute("""
                            UPDATE users
                            SET referred_by=?
                            WHERE user_id=?
                        """, (
                            ref_id,
                            user.id,
                        ))

                        conn.execute("""
                            UPDATE users
                            SET
                                referrals=referrals+1,
                                referral_earned=
                                    referral_earned+?,
                                balance=balance+?
                            WHERE user_id=?
                        """, (
                            REF_REWARD,
                            REF_REWARD,
                            ref_id,
                        ))

                        conn.commit()

        except Exception:
            pass

    # ----------------------------
    # JOIN
    # ----------------------------

    if not await is_joined(
        user.id,
        context.bot,
    ):

        await update.message.reply_text(
            "برای استفاده ابتدا عضو کانال شو.",
            reply_markup=join_keyboard(),
        )

        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        f"💰 موجودی شما: "
        f"{fmt(get_balance(user.id))} TRX"
    )


async def balance_group(update, context):

    if not update.message:
        return

    if update.message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    text = normalize(
        update.message.text.strip()
    )

    if text != "موجودی":
        return

    await show_balance(
        update,
        context,
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral_button(update, context):

    query = update.callback_query

    user = query.from_user

    ensure_user(user)

    row = get_user(user.id)

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start={user.id}"
    )

    await query.answer()

    await query.message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"👤 تعداد: {row['referrals']}\n"
        f"🎁 پاداش هر نفر: "
        f"{fmt(REF_REWARD)}\n"
        f"💰 دریافتی: "
        f"{fmt(row['referral_earned'])}\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# ============================================================
# EXAMPLES
# ============================================================

async def examples_button(update, context):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🎮 مثال بازی\n\n"
        "🎲 4 تاس 0.1\n"
        "🎲 10 تاس 0.1\n"
        "🎲 100 تاس 0.1\n\n"
        "🎯 4 دارت 0.1\n"
        "🏀 4 بسکتبال 0.1\n"
        "🎳 4 بولینگ 0.1\n\n"
        "اعداد فارسی هم قبول است:\n"
        "4 تاس ۰.۱\n\n"
        "🔢 تعداد راند محدود نیست."
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_button(update, context):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 1\n\n"
        "یا:\n"
        "انتقال 0.1"
    )


async def transfer_handler(update, context):

    message = update.message

    if not message:
        return

    if not message.reply_to_message:
        return

    text = normalize(
        message.text.strip()
    )

    if not text.startswith("انتقال"):
        return

    sender = update.effective_user

    receiver = (
        message.reply_to_message.from_user
    )

    if not receiver or receiver.is_bot:
        return

    if sender.id == receiver.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    parts = text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ فرمت:\n"
            "انتقال 1"
        )

        return

    try:
        amount = float(parts[1])

    except ValueError:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    ensure_user(sender)
    ensure_user(receiver)

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            sender_row = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (
                sender.id,
            )).fetchone()

            if not sender_row:

                conn.rollback()
                return

            if (
                float(sender_row["balance"])
                < amount
            ):

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
                receiver.id,
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
        f"👤 گیرنده: {receiver.full_name}\n"
        f"💰 مقدار: {fmt(amount)} TRX"
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

    text = normalize(
        text.strip()
    )

    pattern = (
        r"^(\d+)\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)$"
    )

    match = re.match(
        pattern,
        text,
    )

    if not match:
        return None

    rounds = int(match.group(1))

    game_type = match.group(2)

    stake = float(match.group(3))

    if rounds <= 0 or stake <= 0:
        return None

    return (
        rounds,
        game_type,
        GAME_TYPES[game_type],
        round(stake, 8),
    )


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
                "❌ لغو",
                callback_data=f"cancel:{game_id}",
            )
        ],
    ])


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context):

    message = update.message

    if not message:
        return

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

    if not bot_enabled():
        return

    user = update.effective_user

    ensure_user(user)

    if active_game(user.id):

        await message.reply_text(
            "❌ شما در حال بازی هستید."
        )

        return

    rounds, game_type, emoji, stake = parsed

    if get_balance(user.id) < stake:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(
        user.id,
        -stake,
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    game_id = secrets.token_hex(12)

    with closing(db()) as conn:

        try:

            conn.execute("""
                INSERT INTO games(
                    id,
                    chat_id,
                    creator_id,
                    game_type,
                    emoji,
                    rounds,
                    stake
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                game_id,
                message.chat.id,
                user.id,
                game_type,
                emoji,
                rounds,
                stake,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_balance(
                user.id,
                stake,
            )

            await message.reply_text(
                "❌ ساخت بازی انجام نشد."
            )

            return

    await message.reply_text(
        f"{emoji} بازی ساخته شد.\n\n"
        f"🎮 بازی: {game_type}\n"
        f"🔢 تعداد: {rounds}\n"
        f"💰 ورود: {fmt(stake)} TRX\n\n"
        "انتخاب کن:",
        reply_markup=game_keyboard(game_id),
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game(update, context):

    query = update.callback_query

    user = query.from_user

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
            "❌ بازی شروع شده.",
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

    stake = float(game["stake"])

    if get_balance(user.id) < stake:

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    if not change_balance(
        user.id,
        -stake,
    ):

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
                SELECT status, settled
                FROM games
                WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if (
                not current
                or current["status"] != "waiting"
                or current["settled"]
            ):

                conn.rollback()

                change_balance(
                    user.id,
                    stake,
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
                WHERE id=?
            """, (
                user.id,
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_balance(
                user.id,
                stake,
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
        f"{game['emoji']} بازی دوستان شروع شد.\n\n"
        f"🔢 تعداد راند: {game['rounds']}\n"
        f"💰 ورود: {fmt(stake)} TRX\n\n"
        "👤 بازیکن اول ایموجی بازی را می‌فرستد.\n"
        "👤 بازیکن دوم ایموجی بازی را می‌فرستد.\n\n"
        "🤖 ربات هیچ پرتابی انجام نمی‌دهد."
    )


# ============================================================
# ROBOT GAME
# ============================================================

async def robot_game(update, context):

    query = update.callback_query

    user = query.from_user

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
            "❌ بازی قابل شروع نیست.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        conn.execute("""
            UPDATE games
            SET
                opponent_id=0,
                robot=1,
                status='robot'
            WHERE id=?
            AND settled=0
        """, (
            game_id,
        ))

        conn.commit()

    await query.answer()

    await query.edit_message_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"🎮 {game['game_type']}\n"
        f"🔢 تعداد راند: {game['rounds']}\n"
        f"💰 ورود: {fmt(game['stake'])} TRX\n\n"
        f"👤 اول تو {game['emoji']} را بفرست.\n"
        "🤖 بعد از پرتاب تو، نوبت ربات می‌شود."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query

    user = query.from_user

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
            "❌ فقط سازنده.",
            show_alert=True,
        )

        return

    if game["status"] not in (
        "waiting",
        "robot",
    ):

        await query.answer(
            "❌ این بازی قابل لغو نیست.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        conn.execute("""
            UPDATE games
            SET
                status='cancelled',
                settled=1
            WHERE id=?
            AND settled=0
        """, (
            game_id,
        ))

        conn.commit()

    change_balance(
        user.id,
        float(game["stake"]),
    )

    await query.answer(
        "❌ لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        "💰 مبلغ ورود برگشت داده شد."
    )


# ============================================================
# DICE / DART / BASKETBALL / BOWLING
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

    game = None

    with closing(db()) as conn:

        game = conn.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND emoji=?
            AND status IN(
                'playing',
                'robot'
            )
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY rowid DESC
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

    # ========================================================
    # ROBOT
    #
    # مهم:
    # اول خود کاربر ایموجی می‌فرستد.
    # بعد ربات ایموجی خودش را می‌فرستد.
    # ========================================================

    if game["robot"]:

        if user.id != game["creator_id"]:
            return

        round_no = (
            int(game["creator_round"]) + 1
        )

        with closing(db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    creator_round=?,
                    creator_score=
                        creator_score+?
                WHERE id=?
                AND settled=0
            """, (
                round_no,
                value,
                game["id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 پرتاب تو: {value}\n"
            f"📍 راند {round_no} از {game['rounds']}"
        )

        # بعد از پرتاب کاربر، نوبت ربات است.
        try:

            robot_message = (
                await context.bot.send_dice(
                    chat_id=message.chat.id,
                    emoji=emoji,
                )
            )

            robot_value = int(
                robot_message.dice.value
            )

        except Exception:

            await message.reply_text(
                "❌ پرتاب ربات انجام نشد."
            )

            return

        with closing(db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    opponent_round=
                        opponent_round+1,
                    opponent_score=
                        opponent_score+?
                WHERE id=?
                AND settled=0
            """, (
                robot_value,
                game["id"],
            ))

            conn.commit()

        await message.reply_text(
            f"🤖 پرتاب ربات: {robot_value}"
        )

        if round_no >= int(game["rounds"]):

            await finish_game(
                game["id"],
                context,
            )

        return

    # ========================================================
    # FRIEND GAME
    #
    # اینجا ربات هیچ پرتابی انجام نمی‌دهد.
    # ========================================================

    if game["status"] != "playing":
        return

    if user.id == game["creator_id"]:

        with closing(db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    creator_round=
                        creator_round+1,
                    creator_score=
                        creator_score+?
                WHERE id=?
                AND settled=0
            """, (
                value,
                game["id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 پرتاب بازیکن اول ثبت شد: {value}"
        )

    elif user.id == game["opponent_id"]:

        with closing(db()) as conn:

            conn.execute("""
                UPDATE games
                SET
                    opponent_round=
                        opponent_round+1,
                    opponent_score=
                        opponent_score+?
                WHERE id=?
                AND settled=0
            """, (
                value,
                game["id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 پرتاب بازیکن دوم ثبت شد: {value}"
        )

    else:
        return

    current = get_game(
        game["id"]
    )

    if not current:
        return

    if (
        int(current["creator_round"])
        >= int(current["rounds"])
        and
        int(current["opponent_round"])
        >= int(current["rounds"])
    ):

        await finish_game(
            current["id"],
            context,
        )


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(game_id, context):

    game = get_game(game_id)

    if not game:
        return

    if game["settled"]:
        return

    creator_score = int(
        game["creator_score"]
    )

    opponent_score = int(
        game["opponent_score"]
    )

    # فقط یک بار تسویه
    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
                SELECT settled
                FROM games
                WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if (
                not current
                or current["settled"]
            ):

                conn.rollback()
                return

            conn.execute("""
                UPDATE games
                SET
                    status='finished',
                    settled=1
                WHERE id=?
            """, (
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()
            return

    # ----------------------------
    # DRAW
    # ----------------------------

    if creator_score == opponent_score:

        change_balance(
            game["creator_id"],
            float(game["stake"]),
        )

        if game["opponent_id"]:

            change_balance(
                game["opponent_id"],
                float(game["stake"]),
            )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_score}\n"
            f"👤 بازیکن دوم: {opponent_score}\n\n"
            "🤝 مساوی شد.\n"
            "💰 مبلغ ورود برگشت داده شد."
        )

    # ----------------------------
    # CREATOR WIN
    # ----------------------------

    elif creator_score > opponent_score:

        # برای اعتبار داخلی:
        reward = round(
            float(game["stake"]) * 2,
            8,
        )

        change_balance(
            game["creator_id"],
            reward,
        )

        with closing(db()) as conn:

            conn.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            if game["opponent_id"]:

                conn.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        losses=losses+1
                    WHERE user_id=?
                """, (
                    game["opponent_id"],
                ))

            conn.commit()

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_score}\n"
            f"👤 بازیکن دوم: {opponent_score}\n\n"
            "🏆 بازیکن اول برنده شد."
        )

    # ----------------------------
    # OPPONENT WIN
    # ----------------------------

    else:

        if game["opponent_id"]:

            reward = round(
                float(game["stake"]) * 2,
                8,
            )

            change_balance(
                game["opponent_id"],
                reward,
            )

            with closing(db()) as conn:

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

                conn.commit()

            text = (
                f"{game['emoji']} نتیجه نهایی\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "🏆 بازیکن دوم برنده شد."
            )

        else:

            with closing(db()) as conn:

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
                f"👤 تو: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"
                "🤖 ربات برنده شد."
            )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text,
    )


# ============================================================
# ADMIN PANEL
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
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            ),
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
            ),
        ],
    ])


async def admin_button(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:

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


async def admin_add(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()
    context.user_data["admin"] = "add"

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_remove(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()
    context.user_data["admin"] = "remove"

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_balance(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()
    context.user_data["admin"] = "balance"

    await query.answer()

    await query.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    with closing(db()) as conn:

        users = conn.execute("""
            SELECT COUNT(*) AS c
            FROM users
        """).fetchone()["c"]

        games = conn.execute("""
            SELECT COUNT(*) AS c
            FROM games
        """).fetchone()["c"]

        total = conn.execute("""
            SELECT COALESCE(
                SUM(balance),
                0
            ) AS total
            FROM users
        """).fetchone()["total"]

    await query.answer()

    await query.message.reply_text(
        "📊 آمار\n\n"
        f"👥 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"💰 مجموع موجودی: {fmt(total)} TRX\n"
        f"🔌 وضعیت: "
        f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}"
    )


async def admin_on(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    set_bot_enabled(True)

    await query.answer(
        "🟢 روشن شد."
    )

    await query.message.reply_text(
        "🟢 ربات روشن شد.",
        reply_markup=admin_keyboard(),
    )


async def admin_off(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    set_bot_enabled(False)

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

async def admin_text(update, context):

    user = update.effective_user

    if user.id != OWNER_ID:
        return

    action = context.user_data.get("admin")

    if not action:
        return

    text = normalize(
        update.message.text.strip()
    )

    # ----------------------------
    # USER BALANCE
    # ----------------------------

    if action == "balance":

        try:
            target_id = int(text)

        except ValueError:

            await update.message.reply_text(
                "❌ آیدی نامعتبر است."
            )

            return

        row = get_user(target_id)

        if not row:

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

            return

        await update.message.reply_text(
            "💰 اطلاعات کاربر\n\n"
            f"🆔 {target_id}\n"
            f"👤 {row['name']}\n"
            f"💰 موجودی: "
            f"{fmt(row['balance'])} TRX\n"
            f"👥 زیرمجموعه: {row['referrals']}\n"
            f"🎮 بازی: {row['games']}\n"
            f"🏆 برد: {row['wins']}\n"
            f"❌ باخت: {row['losses']}"
        )

        context.user_data.clear()

        return

    # ----------------------------
    # ADD / REMOVE
    # ----------------------------

    if action not in (
        "add",
        "remove",
    ):
        return

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ\n\n"
            "مثال:\n"
            "123456789 10"
        )

        return

    try:

        target_id = int(parts[0])
        amount = float(parts[1])

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر است."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد.\n"
            "ابتدا باید /start بزند."
        )

        return

    if action == "add":

        success = change_balance(
            target_id,
            amount,
        )

        title = "افزایش"

    else:

        success = change_balance(
            target_id,
            -amount,
        )

        title = "کسر"

    if not success:

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await update.message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 کاربر: {target_id}\n"
        f"💰 مقدار: {fmt(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{fmt(get_balance(target_id))} TRX"
    )

    context.user_data.clear()


# ============================================================
# HOME
# ============================================================

async def home_button(update, context):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(
            query.from_user.id
        ),
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):

    query = update.callback_query

    data = query.data

    if data == "join_check":

        if await is_joined(
            query.from_user.id,
            context.bot,
        ):

            await query.answer(
                "✅ عضویت تأیید شد."
            )

            await query.message.reply_text(
                "✅ تأیید شد.",
                reply_markup=main_keyboard(
                    query.from_user.id
                ),
            )

        else:

            await query.answer(
                "❌ هنوز عضو نیستی.",
                show_alert=True,
            )

        return

    if data == "balance":

        await balance_button(
            update,
            context,
        )

    elif data == "ref":

        await referral_button(
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

        await admin_add(
            update,
            context,
        )

    elif data == "admin_remove":

        await admin_remove(
            update,
            context,
        )

    elif data == "admin_balance":

        await admin_balance(
            update,
            context,
        )

    elif data == "admin_stats":

        await admin_stats(
            update,
            context,
        )

    elif data == "admin_on":

        await admin_on(
            update,
            context,
        )

    elif data == "admin_off":

        await admin_off(
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

async def text_router(update, context):

    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    user = update.effective_user

    ensure_user(user)

    # Admin input
    if (
        user.id == OWNER_ID
        and context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context,
        )

        return

    normalized = normalize(
        text.strip()
    )

    # Group balance
    if (
        normalized == "موجودی"
        and update.message.chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        )
    ):

        await balance_group(
            update,
            context,
        )

        return

    # Transfer
    if normalized.startswith("انتقال"):

        await transfer_handler(
            update,
            context,
        )

        return

    # Game creation
    if parse_game(text):

        await create_game(
            update,
            context,
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):

    log.error(
        "Unhandled error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN را در Environment Variables قرار بده."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # START
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # BUTTONS
    app.add_handler(
        CallbackQueryHandler(
            callback_router,
        )
    )

    # TELEGRAM GAME EMOJIS
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_emoji_handler,
        )
    )

    # TEXT
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    app.add_error_handler(
        error_handler
    )

    log.info(
        "BET_BT started successfully."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
