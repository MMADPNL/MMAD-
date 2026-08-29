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
# تنظیمات
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL_USERNAME = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB_FILE = "bot.sqlite3"

# ضریب جایزه داخلی
PAYOUT_RATE = 1.85

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# دیتابیس
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_db()) as db:

        db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER,
            game_type TEXT NOT NULL,
            emoji TEXT NOT NULL,
            rounds INTEGER NOT NULL,
            stake REAL NOT NULL,
            creator_throws INTEGER NOT NULL DEFAULT 0,
            opponent_throws INTEGER NOT NULL DEFAULT 0,
            creator_score INTEGER NOT NULL DEFAULT 0,
            opponent_score INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            settled INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        db.commit()


# ============================================================
# کاربر
# ============================================================

def ensure_user(user):

    with closing(get_db()) as db:

        db.execute("""
        INSERT INTO users (
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
            user.username or ""
        ))

        db.commit()


def get_balance(user_id):

    with closing(get_db()) as db:

        row = db.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            return 0

        return float(row["balance"])


def change_balance(user_id, amount):

    with closing(get_db()) as db:

        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                db.rollback()
                return False

            balance = float(row["balance"])
            new_balance = round(balance + amount, 8)

            if new_balance < 0:
                db.rollback()
                return False

            db.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                new_balance,
                user_id
            ))

            db.commit()
            return True

        except Exception:
            db.rollback()
            logger.exception("balance error")
            return False


def format_trx(amount):

    return (
        f"{float(amount):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# ============================================================
# اعداد فارسی
# ============================================================

def normalize_digits(text):

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return str(text).translate(table)


# ============================================================
# بازی‌ها
# ============================================================

GAME_TYPES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳"
}


def parse_game(text):

    text = normalize_digits(
        text.strip()
    )

    pattern = re.compile(
        r"^(\d+)\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)$"
    )

    match = pattern.match(text)

    if not match:
        return None

    rounds = int(match.group(1))
    game_name = match.group(2)
    amount = float(match.group(3))

    if rounds <= 0:
        return None

    if amount <= 0:
        return None

    return {
        "rounds": rounds,
        "game_type": game_name,
        "emoji": GAME_TYPES[game_name],
        "stake": round(amount, 8)
    }


def get_game(game_id):

    with closing(get_db()) as db:

        return db.execute("""
        SELECT *
        FROM games
        WHERE game_id=?
        """, (game_id,)).fetchone()


def active_game(user_id):

    with closing(get_db()) as db:

        return db.execute("""
        SELECT *
        FROM games
        WHERE settled=0
        AND status IN ('waiting', 'playing')
        AND (
            creator_id=?
            OR opponent_id=?
        )
        LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()


# ============================================================
# عضویت
# ============================================================

async def check_membership(bot, user_id):

    if user_id == OWNER_ID:
        return True

    try:

        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "creator",
            "administrator",
            "member"
        )

    except Exception:

        logger.warning(
            "Membership check failed"
        )

        return True


async def require_membership(update, context):

    user = update.effective_user

    if not user:
        return False

    if await check_membership(
        context.bot,
        user.id
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_member"
            )
        ]
    ])

    if update.callback_query:

        await update.callback_query.answer(
            "❌ ابتدا عضو کانال شوید.",
            show_alert=True
        )

        await update.callback_query.message.reply_text(
            "❌ برای استفاده ابتدا عضو کانال شوید.",
            reply_markup=keyboard
        )

    elif update.message:

        await update.message.reply_text(
            "❌ برای استفاده ابتدا عضو کانال شوید.",
            reply_markup=keyboard
        )

    return False


# ============================================================
# منوی اصلی
# ============================================================

def main_keyboard(user_id):

    rows = [
        [
            InlineKeyboardButton(
                "🎮 بازی",
                callback_data="games"
            ),
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer_help"
            ),
            InlineKeyboardButton(
                "💸 برداشت",
                callback_data="withdraw"
            )
        ],
        [
            InlineKeyboardButton(
                "📖 راهنما",
                callback_data="help"
            ),
            InlineKeyboardButton(
                "🎯 مثال بازی",
                callback_data="examples"
            )
        ]
    ]

    if user_id == OWNER_ID:

        rows.append([
            InlineKeyboardButton(
                "👑 پنل مالک",
                callback_data="admin"
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

    ensure_user(user)

    if not await require_membership(
        update,
        context
    ):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از دکمه‌های زیر استفاده کن.",
        reply_markup=main_keyboard(user.id)
    )


# ============================================================
# منوی بازی
# ============================================================

async def games_menu(update, context):

    q = update.callback_query

    await q.answer()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data="friend_info"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data="robot_info"
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 مثال",
                callback_data="examples"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="home"
            )
        ]
    ])

    await q.message.reply_text(
        "🎮 بازی\n\n"
        "نوع بازی را انتخاب کن.",
        reply_markup=keyboard
    )


async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال‌ها:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "مثال‌های دیگر:\n"
        "10 تاس 0.1\n"
        "20 دارت 1\n"
        "100 بسکتبال 2\n\n"
        "تعداد بازی محدود نیست."
    )


async def friend_info(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "بعد از ساخت، بازیکن دوم وارد می‌شود.\n\n"
        "در این حالت فقط کاربران پرتاب می‌کنند.\n"
        "ربات هیچ 🎲🎯🏀🎳 نمی‌فرستد."
    )


async def robot_info(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "کاربر ایموجی را می‌فرستد.\n"
        "بعد از آن ربات پرتاب می‌کند."
    )


# ============================================================
# ساخت بازی
# ============================================================

async def create_game(update, context):

    message = update.message

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        message.text or ""
    )

    if not parsed:
        return

    user = update.effective_user

    ensure_user(user)

    if not await require_membership(
        update,
        context
    ):
        return

    old_game = active_game(user.id)

    if old_game:

        await message.reply_text(
            "⏳ شما در حال بازی است.\n\n"
            "موجودی برگشت داده شد ❌️❌️"
        )

        return

    amount = parsed["stake"]

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{format_trx(get_balance(user.id))} TRX"
        )

        return

    if not change_balance(
        user.id,
        -amount
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    game_id = secrets.token_hex(12)

    with closing(get_db()) as db:

        try:

            db.execute("""
            INSERT INTO games (
                game_id,
                chat_id,
                creator_id,
                game_type,
                emoji,
                rounds,
                stake,
                mode,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                game_id,
                message.chat.id,
                user.id,
                parsed["game_type"],
                parsed["emoji"],
                parsed["rounds"],
                amount,
                "friend",
                "waiting"
            ))

            db.commit()

        except Exception:

            db.rollback()

            change_balance(
                user.id,
                amount
            )

            await message.reply_text(
                "❌ خطا در ساخت بازی."
            )

            return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 ورود بازیکن",
                callback_data=f"join:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"robot:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]
    ])

    await message.reply_text(
        f"{parsed['emoji']} بازی ساخته شد.\n\n"
        f"🎮 {parsed['game_type']}\n"
        f"🔢 تعداد: {parsed['rounds']}\n"
        f"💰 ورود: {format_trx(amount)} TRX",
        reply_markup=keyboard
    )


# ============================================================
# ورود دوست
# ============================================================

async def join_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    user = q.from_user

    if user.id == game["creator_id"]:

        await q.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده.",
            show_alert=True
        )

        return

    old = active_game(user.id)

    if old:

        await q.answer(
            "❌ شما در حال بازی است.",
            show_alert=True
        )

        return

    amount = float(game["stake"])

    if get_balance(user.id) < amount:

        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    if not change_balance(
        user.id,
        -amount
    ):

        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            current = db.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
            AND status='waiting'
            AND settled=0
            """, (game_id,)).fetchone()

            if not current:

                db.rollback()

                change_balance(
                    user.id,
                    amount
                )

                await q.answer(
                    "❌ بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            db.execute("""
            UPDATE games
            SET
                opponent_id=?,
                status='playing',
                mode='friend'
            WHERE game_id=?
            """, (
                user.id,
                game_id
            ))

            db.commit()

        except Exception:

            db.rollback()

            change_balance(
                user.id,
                amount
            )

            await q.answer(
                "❌ خطا.",
                show_alert=True
            )

            return

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی شروع شد.\n\n"
        f"🔢 تعداد دور: {game['rounds']}\n\n"
        "👤 بازیکن اول پرتاب می‌کند.\n"
        "👤 سپس بازیکن دوم.\n\n"
        "⚠️ ربات هیچ پرتابی انجام نمی‌دهد."
    )


# ============================================================
# بازی با ربات
# ============================================================

async def robot_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if q.from_user.id != game["creator_id"]:

        await q.answer(
            "❌ فقط سازنده بازی.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده.",
            show_alert=True
        )

        return

    with closing(get_db()) as db:

        db.execute("""
        UPDATE games
        SET
            opponent_id=0,
            mode='robot',
            status='playing'
        WHERE game_id=?
        AND status='waiting'
        """, (game_id,))

        db.commit()

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"{game['emoji']} {game['game_type']}\n"
        f"🔢 تعداد: {game['rounds']}\n\n"
        f"👤 حالا {game['emoji']} را بفرست.\n"
        "بعد از پرتاب تو، ربات پرتاب می‌کند."
    )


# ============================================================
# لغو
# ============================================================

async def cancel_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:
        await q.answer(
            "❌ پیدا نشد.",
            show_alert=True
        )
        return

    if q.from_user.id != game["creator_id"]:

        await q.answer(
            "❌ دسترسی نداری.",
            show_alert=True
        )

        return

    if game["settled"]:

        await q.answer(
            "❌ قبلاً بسته شده.",
            show_alert=True
        )

        return

    with closing(get_db()) as db:

        db.execute("""
        UPDATE games
        SET
            status='cancelled',
            settled=1
        WHERE game_id=?
        AND settled=0
        """, (game_id,))

        db.commit()

    change_balance(
        game["creator_id"],
        float(game["stake"])
    )

    if (
        game["opponent_id"]
        and game["opponent_id"] != 0
    ):

        change_balance(
            game["opponent_id"],
            float(game["stake"])
        )

    await q.answer(
        "لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        "💰 موجودی بازیکنان برگشت داده شد."
    )


# ============================================================
# پردازش پرتاب کاربران
# ============================================================

async def process_dice(update, context):

    message = update.message

    if not message:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    dice = message.dice

    if not dice:
        return

    emoji = dice.emoji

    if emoji not in GAME_TYPES.values():
        return

    user = update.effective_user

    with closing(get_db()) as db:

        game = db.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND emoji=?
        AND status='playing'
        AND settled=0
        AND (
            creator_id=?
            OR opponent_id=?
        )
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            message.chat.id,
            emoji,
            user.id,
            user.id
        )).fetchone()

    if not game:
        return

    value = int(dice.value)

    # ========================================================
    # بازی با ربات
    # ========================================================

    if game["mode"] == "robot":

        if user.id != game["creator_id"]:
            return

        if (
            game["creator_throws"]
            >= game["rounds"]
        ):
            return

        if (
            game["creator_throws"]
            >
            game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ ربات هنوز پرتاب خودش را انجام نداده."
            )

            return

        with closing(get_db()) as db:

            db.execute("""
            UPDATE games
            SET
                creator_throws=creator_throws+1,
                creator_score=creator_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"]
            ))

            db.commit()

        await message.reply_text(
            f"👤 پرتاب شما: {value}\n"
            "🤖 نوبت ربات..."
        )

        # فقط در mode=robot ربات پرتاب می‌کند.
        robot_message = await context.bot.send_dice(
            chat_id=message.chat.id,
            emoji=emoji
        )

        robot_value = int(
            robot_message.dice.value
        )

        with closing(get_db()) as db:

            db.execute("""
            UPDATE games
            SET
                opponent_throws=opponent_throws+1,
                opponent_score=opponent_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                robot_value,
                game["game_id"]
            ))

            db.commit()

        await message.reply_text(
            f"🤖 پرتاب ربات: {robot_value}"
        )

        current = get_game(
            game["game_id"]
        )

        if (
            current["creator_throws"]
            >= current["rounds"]
            and
            current["opponent_throws"]
            >= current["rounds"]
        ):

            await finish_game(
                game["game_id"],
                context
            )

        return

    # ========================================================
    # بازی دوستانه
    # ========================================================

    if game["mode"] != "friend":
        return

    # بازیکن اول
    if user.id == game["creator_id"]:

        if game["creator_throws"] >= game["rounds"]:
            return

        if (
            game["creator_throws"]
            >
            game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ منتظر پرتاب بازیکن دوم هستیم."
            )

            return

        with closing(get_db()) as db:

            db.execute("""
            UPDATE games
            SET
                creator_throws=creator_throws+1,
                creator_score=creator_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"]
            ))

            db.commit()

        await message.reply_text(
            f"👤 بازیکن اول: {value}\n"
            "⏳ نوبت بازیکن دوم."
        )

    # بازیکن دوم
    elif user.id == game["opponent_id"]:

        if game["opponent_throws"] >= game["rounds"]:
            return

        if (
            game["creator_throws"]
            <= game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ هنوز نوبت شما نیست."
            )

            return

        with closing(get_db()) as db:

            db.execute("""
            UPDATE games
            SET
                opponent_throws=opponent_throws+1,
                opponent_score=opponent_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"]
            ))

            db.commit()

        await message.reply_text(
            f"👤 بازیکن دوم: {value}"
        )

    else:
        return

    # مهم:
    # در این قسمت هیچ send_dice وجود ندارد.
    # بنابراین بازی دوستانه هیچ پرتابی از طرف ربات ندارد.

    current = get_game(
        game["game_id"]
    )

    if (
        current["creator_throws"]
        >= current["rounds"]
        and
        current["opponent_throws"]
        >= current["rounds"]
    ):

        await finish_game(
            game["game_id"],
            context
        )


# ============================================================
# پایان بازی
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

    # قفل تسویه
    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            current = db.execute("""
            SELECT settled
            FROM games
            WHERE game_id=?
            """, (game_id,)).fetchone()

            if not current or current["settled"]:
                db.rollback()
                return

            db.execute("""
            UPDATE games
            SET
                settled=1,
                status='finished'
            WHERE game_id=?
            """, (game_id,))

            db.commit()

        except Exception:

            db.rollback()

            logger.exception(
                "settlement error"
            )

            return

    stake = float(game["stake"])

    # مساوی
    if creator_score == opponent_score:

        change_balance(
            game["creator_id"],
            stake
        )

        if game["mode"] == "friend":

            change_balance(
                game["opponent_id"],
                stake
            )

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "🤝 مساوی شد.\n"
                "💰 مبلغ برگشت داده شد."
            )

        else:

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"
                "🤝 مساوی شد.\n"
                "💰 مبلغ برگشت داده شد."
            )

    # نفر اول
    elif creator_score > opponent_score:

        reward = round(
            stake * PAYOUT_RATE,
            8
        )

        change_balance(
            game["creator_id"],
            reward
        )

        if game["mode"] == "friend":

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "🏆 بازیکن اول برنده شد.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

        else:

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"
                "🏆 شما برنده شدید.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

    # نفر دوم
    else:

        reward = round(
            stake * PAYOUT_RATE,
            8
        )

        if game["mode"] == "friend":

            change_balance(
                game["opponent_id"],
                reward
            )

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "🏆 بازیکن دوم برنده شد.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

        else:

            text = (
                f"{game['emoji']} نتیجه\n\n"
                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"
                "🤖 ربات برنده شد."
            )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# ============================================================
# موجودی
# ============================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    ensure_user(q.from_user)

    await q.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"{format_trx(get_balance(q.from_user.id))} TRX"
    )


async def balance_text(update, context):

    message = update.message

    if not message:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    if normalize_digits(
        message.text.strip()
    ) != "موجودی":
        return

    user = update.effective_user

    ensure_user(user)

    await message.reply_text(
        f"💰 موجودی شما: "
        f"{format_trx(get_balance(user.id))} TRX"
    )


# ============================================================
# انتقال Reply
# ============================================================

async def transfer_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 1"
    )


async def transfer(update, context):

    message = update.message

    if not message:
        return

    text = normalize_digits(
        message.text.strip()
    )

    if not text.startswith("انتقال"):
        return

    if not message.reply_to_message:

        await message.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    parts = text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ مثال:\n"
            "انتقال 0.1"
        )

        return

    try:

        amount = float(parts[1])

    except ValueError:

        await message.reply_text(
            "❌ مبلغ اشتباه است."
        )

        return

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    sender = update.effective_user
    receiver = (
        message
        .reply_to_message
        .from_user
    )

    if not receiver:
        return

    if sender.id == receiver.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if receiver.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )

        return

    ensure_user(sender)
    ensure_user(receiver)

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (sender.id,)).fetchone()

            if not row:

                db.rollback()

                await message.reply_text(
                    "❌ کاربر پیدا نشد."
                )

                return

            balance = float(
                row["balance"]
            )

            if balance < amount:

                db.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            db.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                sender.id
            ))

            db.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id
            ))

            db.commit()

        except Exception:

            db.rollback()

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💰 مقدار: {format_trx(amount)} TRX"
    )


# ============================================================
# برداشت
# ============================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "این بخش در حال حاضر فعال نیست."
    )


# ============================================================
# راهنما
# ============================================================

async def help_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی:\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام کاربر Reply کن:\n"
        "انتقال 0.1"
    )


# ============================================================
# پنل مالک
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
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
                "💰 موجودی کاربر",
                callback_data="admin_balance"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="home"
            )
        ]
    ])


async def admin_menu(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:

        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    await q.answer()

    await q.message.reply_text(
        "👑 پنل مالک",
        reply_markup=admin_keyboard()
    )


async def admin_add(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "add"

    await q.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_remove(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "remove"

    await q.message.reply_text(
        "➖ کاهش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ"
    )


async def admin_balance(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    with closing(get_db()) as db:

        users = db.execute("""
        SELECT COUNT(*) c
        FROM users
        """).fetchone()["c"]

        games = db.execute("""
        SELECT COUNT(*) c
        FROM games
        """).fetchone()["c"]

        active = db.execute("""
        SELECT COUNT(*) c
        FROM games
        WHERE settled=0
        """).fetchone()["c"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"⏳ فعال: {active}"
    )


# ============================================================
# پردازش پنل
# ============================================================

async def admin_text(update, context):

    if not update.message:
        return

    user = update.effective_user

    if user.id != OWNER_ID:
        return

    action = context.user_data.get(
        "admin_action"
    )

    if not action:
        return

    text = normalize_digits(
        update.message.text.strip()
    )

    if action == "balance":

        try:
            target = int(text)
        except ValueError:

            await update.message.reply_text(
                "❌ آیدی اشتباه است."
            )

            return

        await update.message.reply_text(
            f"💰 موجودی کاربر:\n"
            f"{format_trx(get_balance(target))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ"
        )

        return

    try:

        target = int(parts[0])
        amount = float(parts[1])

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار اشتباه است."
        )

        return

    if amount <= 0:
        return

    if action == "add":

        ok = change_balance(
            target,
            amount
        )

    else:

        ok = change_balance(
            target,
            -amount
        )

    if ok:

        await update.message.reply_text(
            "✅ انجام شد.\n\n"
            f"💰 موجودی جدید: "
            f"{format_trx(get_balance(target))} TRX"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


# ============================================================
# Callback Router
# ============================================================

async def callback_router(update, context):

    data = update.callback_query.data

    if data == "check_member":

        q = update.callback_query

        ok = await check_membership(
            context.bot,
            q.from_user.id
        )

        if ok:

            await q.answer(
                "✅ تأیید شد.",
                show_alert=True
            )

        else:

            await q.answer(
                "❌ هنوز عضو نیستی.",
                show_alert=True
            )

        return

    if data == "games":
        await games_menu(update, context)

    elif data == "examples":
        await examples(update, context)

    elif data == "friend_info":
        await friend_info(update, context)

    elif data == "robot_info":
        await robot_info(update, context)

    elif data == "balance":
        await balance_button(update, context)

    elif data == "transfer_help":
        await transfer_help(update, context)

    elif data == "withdraw":
        await withdraw(update, context)

    elif data == "help":
        await help_button(update, context)

    elif data == "admin":
        await admin_menu(update, context)

    elif data == "admin_add":
        await admin_add(update, context)

    elif data == "admin_remove":
        await admin_remove(update, context)

    elif data == "admin_balance":
        await admin_balance(update, context)

    elif data == "admin_stats":
        await admin_stats(update, context)

    elif data == "home":

        q = update.callback_query

        await q.answer()

        await q.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=main_keyboard(
                q.from_user.id
            )
        )

    elif data.startswith("join:"):
        await join_game(update, context)

    elif data.startswith("robot:"):
        await robot_game(update, context)

    elif data.startswith("cancel:"):
        await cancel_game(update, context)


# ============================================================
# Text Router
# ============================================================

async def text_router(update, context):

    if not update.message:
        return

    user = update.effective_user

    ensure_user(user)

    # پنل مالک
    if (
        user.id == OWNER_ID
        and context.user_data.get("admin_action")
    ):

        await admin_text(
            update,
            context
        )

        return

    text = normalize_digits(
        update.message.text.strip()
    )

    # موجودی
    if text == "موجودی":

        await balance_text(
            update,
            context
        )

        return

    # انتقال
    if text.startswith("انتقال"):

        await transfer(
            update,
            context
        )

        return

    # بازی
    if parse_game(text):

        await create_game(
            update,
            context
        )

        return


# ============================================================
# خطا
# ============================================================

async def error_handler(update, context):

    logger.exception(
        "Unhandled error",
        exc_info=context.error
    )


# ============================================================
# اجرا
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

    # فقط یک /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # دکمه‌ها
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # ایموجی‌های واقعی تلگرام
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            process_dice
        )
    )

    # فقط یک Text Router
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
