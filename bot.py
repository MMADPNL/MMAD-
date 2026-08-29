import os
import re
import sqlite3
import secrets
import logging
import asyncio
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

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = 8552447077

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB = "bot.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(__name__)

# قفل برای جلوگیری از ثبت همزمان و باگ تداخل
GAME_LOCK = asyncio.Lock()


# =========================================================
# DATABASE
# =========================================================

def db():
    con = sqlite3.connect(
        DB,
        timeout=30,
        check_same_thread=False
    )
    con.row_factory = sqlite3.Row
    return con


def init_db():

    with closing(db()) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            balance REAL DEFAULT 0
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            creator INTEGER NOT NULL,
            opponent INTEGER DEFAULT NULL,

            creator_name TEXT DEFAULT '',
            opponent_name TEXT DEFAULT '',

            game TEXT NOT NULL,
            emoji TEXT NOT NULL,

            rounds INTEGER NOT NULL,

            creator_round INTEGER DEFAULT 0,
            opponent_round INTEGER DEFAULT 0,

            creator_score INTEGER DEFAULT 0,
            opponent_score INTEGER DEFAULT 0,

            amount REAL DEFAULT 0,

            mode TEXT NOT NULL,
            status TEXT NOT NULL,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # جدول ثبت پرتاب‌ها برای ضد دوباره‌کاری
        con.execute("""
        CREATE TABLE IF NOT EXISTS throws (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            side TEXT NOT NULL,
            round_no INTEGER NOT NULL,
            value INTEGER NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

            UNIQUE(
                game_id,
                side,
                round_no
            )
        )
        """)

        con.commit()


# =========================================================
# USERS
# =========================================================

def register(user):

    with closing(db()) as con:

        con.execute("""
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
            user.username or ""
        ))

        con.commit()


def balance(user_id):

    with closing(db()) as con:

        row = con.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            return 0.0

        return float(row["balance"])


def add_balance(user_id, amount):

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                con.rollback()
                return False

            new_balance = (
                float(row["balance"])
                + float(amount)
            )

            if new_balance < 0:
                con.rollback()
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                round(new_balance, 8),
                user_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()

            log.exception(
                "balance update error"
            )

            return False


def money(value):

    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# =========================================================
# DIGITS
# =========================================================

def digits(text):

    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


# =========================================================
# GAMES
# =========================================================

GAMES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳"
}


def parse_game(text):

    text = digits(
        text.strip()
    )

    m = re.fullmatch(
        r"(\d+)\s+(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)",
        text
    )

    if not m:
        return None

    rounds = int(m.group(1))
    game = m.group(2)
    amount = float(m.group(3))

    if rounds <= 0:
        return None

    # جلوگیری از بازی‌های بسیار سنگین
    if rounds > 100:
        return None

    if amount <= 0:
        return None

    return {
        "rounds": rounds,
        "game": game,
        "emoji": GAMES[game],
        "amount": amount
    }


def get_game(game_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE id=?
        """, (game_id,)).fetchone()


def user_game(user_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE status IN ('waiting','playing')
        AND (
            creator=?
            OR opponent=?
        )
        LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()


# =========================================================
# MEMBERSHIP
# =========================================================

async def member_ok(bot, user_id):

    if user_id == OWNER_ID:
        return True

    try:

        m = await bot.get_chat_member(
            CHANNEL,
            user_id
        )

        return m.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception:

        return True


async def membership(update, context):

    user = update.effective_user

    if await member_ok(
        context.bot,
        user.id
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی",
                callback_data="membership"
            )
        ]
    ])

    if update.message:

        await update.message.reply_text(
            "❌ ابتدا عضو کانال شوید.",
            reply_markup=keyboard
        )

    elif update.callback_query:

        await update.callback_query.answer(
            "❌ ابتدا عضو کانال شوید.",
            show_alert=True
        )

    return False


# =========================================================
# MAIN KEYBOARD
# =========================================================

def keyboard(user_id):

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
                callback_data="transfer"
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


# =========================================================
# START
# =========================================================

async def start(update, context):

    user = update.effective_user

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از دکمه‌های زیر استفاده کن.",
        reply_markup=keyboard(user.id)
    )


# =========================================================
# GAMES MENU
# =========================================================

async def games(update, context):

    q = update.callback_query

    await q.answer()

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data="friends"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data="robot_help"
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 مثال‌ها",
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
        "🎮 بازی‌ها\n\n"
        "نوع بازی را انتخاب کن.",
        reply_markup=kb
    )


async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "عدد اول = تعداد پرتاب\n"
        "عدد آخر = مبلغ شرط داخلی"
    )


async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "در این حالت فقط کاربران پرتاب می‌کنند.\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "اول بازیکن اول، سپس بازیکن دوم."
    )


async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "هر ۴ بازی پشتیبانی می‌شود:\n\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "ترتیب بازی:\n\n"
        "👤 کاربر تمام پرتاب‌های خودش را می‌زند.\n"
        "⬇️\n"
        "🤖 سپس ربات تمام پرتاب‌های خودش را می‌زند."
    )


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(update, context):

    msg = update.message

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        msg.text or ""
    )

    if not parsed:
        return

    user = update.effective_user

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    async with GAME_LOCK:

        existing = user_game(
            user.id
        )

        if existing:

            await msg.reply_text(
                "⏳ شما در یک بازی فعال هستید.\n"
                "ابتدا همان بازی را تمام یا لغو کنید."
            )

            return

        game_id = secrets.token_hex(16)

        with closing(db()) as con:

            con.execute("""
            INSERT INTO games(
                id,
                chat_id,
                creator,
                opponent,
                creator_name,
                opponent_name,
                game,
                emoji,
                rounds,
                amount,
                mode,
                status
            )
            VALUES (?, ?, ?, NULL, ?, '', ?, ?, ?, ?, ?, ?)
            """, (
                game_id,
                msg.chat.id,
                user.id,
                user.full_name or "کاربر",
                parsed["game"],
                parsed["emoji"],
                parsed["rounds"],
                parsed["amount"],
                "friend",
                "waiting"
            ))

            con.commit()

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 ورود به بازی",
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

    await msg.reply_text(
        f"{parsed['emoji']} بازی ساخته شد.\n\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 پرتاب: {parsed['rounds']}\n"
        f"💰 شرط: {money(parsed['amount'])} TRX",
        reply_markup=kb
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    async with GAME_LOCK:

        game = get_game(
            game_id
        )

        if not game:

            await q.answer(
                "❌ بازی وجود ندارد.",
                show_alert=True
            )

            return

        user = q.from_user

        if user.id == game["creator"]:

            await q.answer(
                "❌ خودت سازنده بازی هستی.",
                show_alert=True
            )

            return

        if game["status"] != "waiting":

            await q.answer(
                "❌ بازی قبلاً شروع شده.",
                show_alert=True
            )

            return

        old = user_game(
            user.id
        )

        if old:

            await q.answer(
                "❌ شما در بازی دیگری هستید.",
                show_alert=True
            )

            return

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                opponent=?,
                opponent_name=?,
                status='playing',
                mode='friend'
            WHERE id=?
            AND status='waiting'
            """, (
                user.id,
                user.full_name or "کاربر",
                game_id
            ))

            con.commit()

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی شروع شد!\n\n"
        f"👤 سازنده: {game['creator_name']}\n"
        f"👤 حریف: {user.full_name}\n\n"
        f"🎮 {game['game']}\n"
        f"🔢 {game['rounds']} پرتاب\n\n"
        "👤 ابتدا سازنده پرتاب می‌کند.\n"
        "👤 سپس حریف."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    async with GAME_LOCK:

        game = get_game(
            game_id
        )

        if not game:

            await q.answer(
                "❌ بازی وجود ندارد.",
                show_alert=True
            )

            return

        if q.from_user.id != game["creator"]:

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

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                opponent=0,
                opponent_name='ربات',
                mode='robot',
                status='playing'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            con.commit()

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی شروع شد.\n\n"
        f"👤 {game['creator_name']}\n"
        f"🤖 ربات\n\n"
        f"{game['emoji']} {game['game']}\n"
        f"🔢 {game['rounds']} پرتاب\n\n"
        f"👤 اول شما باید {game['rounds']} پرتاب "
        f"{game['emoji']} بفرستید.\n\n"
        "بعد از تمام شدن پرتاب‌های شما، "
        "ربات پرتاب‌های خودش را انجام می‌دهد."
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    async with GAME_LOCK:

        game = get_game(
            game_id
        )

        if not game:

            await q.answer(
                "❌ پیدا نشد.",
                show_alert=True
            )

            return

        if q.from_user.id != game["creator"]:

            await q.answer(
                "❌ فقط سازنده.",
                show_alert=True
            )

            return

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            con.commit()

    await q.answer(
        "لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد."
    )


# =========================================================
# SAVE THROW
# =========================================================

def save_throw(
    game_id,
    user_id,
    side,
    round_no,
    value
):

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            # ضد ثبت دوباره
            exists = con.execute("""
            SELECT id
            FROM throws
            WHERE game_id=?
            AND side=?
            AND round_no=?
            """, (
                game_id,
                side,
                round_no
            )).fetchone()

            if exists:

                con.rollback()

                return False

            con.execute("""
            INSERT INTO throws(
                game_id,
                user_id,
                side,
                round_no,
                value
            )
            VALUES (?, ?, ?, ?, ?)
            """, (
                game_id,
                user_id,
                side,
                round_no,
                value
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()

            return False


# =========================================================
# DICE HANDLER
# =========================================================

async def dice_handler(update, context):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    dice = msg.dice

    if not dice:
        return

    emoji = dice.emoji

    if emoji not in GAMES.values():
        return

    user = update.effective_user

    async with GAME_LOCK:

        game = None

        with closing(db()) as con:

            game = con.execute("""
            SELECT *
            FROM games
            WHERE chat_id=?
            AND emoji=?
            AND status='playing'
            AND (
                creator=?
                OR opponent=?
            )
            ORDER BY created_at DESC
            LIMIT 1
            """, (
                msg.chat.id,
                emoji,
                user.id,
                user.id
            )).fetchone()

        if not game:
            return

        value = int(
            dice.value
        )

        # =====================================================
        # ROBOT MODE
        # =====================================================

        if game["mode"] == "robot":

            if user.id != game["creator"]:
                return

            # اول باید تمام پرتاب‌های کاربر انجام شود
            if game["creator_round"] >= game["rounds"]:

                await msg.reply_text(
                    "⏳ پرتاب‌های شما تمام شده؛ "
                    "اکنون نوبت ربات است."
                )

                return

            round_no = (
                int(game["creator_round"])
                + 1
            )

            saved = save_throw(
                game["id"],
                user.id,
                "creator",
                round_no,
                value
            )

            if not saved:

                return

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET
                    creator_round=?,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                """, (
                    round_no,
                    value,
                    game["id"]
                ))

                con.commit()

            if round_no < game["rounds"]:

                await msg.reply_text(
                    f"👤 {game['creator_name']}: "
                    f"پرتاب {round_no}/{game['rounds']} "
                    f"→ {value}\n\n"
                    f"🎯 هنوز {game['rounds'] - round_no} "
                    "پرتاب باقی مانده."
                )

                return

            # تمام پرتاب‌های کاربر تمام شد
            await msg.reply_text(
                f"👤 {game['creator_name']} تمام "
                f"{game['rounds']} پرتاب را انجام داد.\n\n"
                "🤖 حالا نوبت ربات است."
            )

            # ربات بعد از تمام شدن کاربر بازی می‌کند
            await robot_turn(
                game["id"],
                context
            )

            return

        # =====================================================
        # FRIEND MODE
        # =====================================================

        if game["mode"] != "friend":
            return

        # -----------------------------------------------------
        # PLAYER 1
        # -----------------------------------------------------

        if user.id == game["creator"]:

            if game["creator_round"] >= game["rounds"]:
                return

            # اگر بازیکن اول یک دور جلوتر است
            if game["creator_round"] > game["opponent_round"]:

                await msg.reply_text(
                    "⏳ هنوز نوبت بازیکن دوم است."
                )

                return

            round_no = (
                int(game["creator_round"])
                + 1
            )

            saved = save_throw(
                game["id"],
                user.id,
                "creator",
                round_no,
                value
            )

            if not saved:
                return

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET
                    creator_round=?,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                """, (
                    round_no,
                    value,
                    game["id"]
                ))

                con.commit()

            await msg.reply_text(
                f"👤 {game['creator_name']}\n"
                f"پرتاب {round_no}/{game['rounds']} → {value}\n\n"
                "⏳ نوبت حریف."
            )

        # -----------------------------------------------------
        # PLAYER 2
        # -----------------------------------------------------

        elif user.id == game["opponent"]:

            if game["opponent_round"] >= game["rounds"]:
                return

            if game["creator_round"] <= game["opponent_round"]:

                await msg.reply_text(
                    "⏳ هنوز نوبت سازنده است."
                )

                return

            round_no = (
                int(game["opponent_round"])
                + 1
            )

            saved = save_throw(
                game["id"],
                user.id,
                "opponent",
                round_no,
                value
            )

            if not saved:
                return

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET
                    opponent_round=?,
                    opponent_score=opponent_score+?
                WHERE id=?
                AND status='playing'
                """, (
                    round_no,
                    value,
                    game["id"]
                ))

                con.commit()

            await msg.reply_text(
                f"👤 {game['opponent_name']}\n"
                f"پرتاب {round_no}/{game['rounds']} → {value}"
            )

        else:
            return

        current = get_game(
            game["id"]
        )

        if (
            current["creator_round"]
            >= current["rounds"]
            and
            current["opponent_round"]
            >= current["rounds"]
        ):

            await finish(
                current["id"],
                context
            )


# =========================================================
# ROBOT TURN
# =========================================================

async def robot_turn(game_id, context):

    game = get_game(
        game_id
    )

    if not game:
        return

    if game["status"] != "playing":
        return

    if game["mode"] != "robot":
        return

    # ربات تمام پرتاب‌ها را بعد از کاربر انجام می‌دهد
    for round_no in range(
        1,
        int(game["rounds"]) + 1
    ):

        current = get_game(
            game_id
        )

        if not current:
            return

        if current["status"] != "playing":
            return

        if current["opponent_round"] >= current["rounds"]:
            break

        # ارسال ایموجی توسط ربات
        sent = await context.bot.send_dice(
            chat_id=current["chat_id"],
            emoji=current["emoji"]
        )

        bot_value = int(
            sent.dice.value
        )

        # ضد ثبت
        saved = save_throw(
            game_id,
            0,
            "opponent",
            round_no,
            bot_value
        )

        if not saved:
            continue

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                opponent_round=?,
                opponent_score=opponent_score+?
            WHERE id=?
            AND status='playing'
            """, (
                round_no,
                bot_value,
                game_id
            ))

            con.commit()

        await context.bot.send_message(
            chat_id=current["chat_id"],
            text=(
                f"🤖 ربات\n"
                f"پرتاب {round_no}/{current['rounds']} "
                f"→ {bot_value}"
            )
        )

        # فاصله کوچک برای جلوگیری از اسپم
        await asyncio.sleep(0.8)

    final_game = get_game(
        game_id
    )

    if not final_game:
        return

    if (
        final_game["creator_round"]
        >= final_game["rounds"]
        and
        final_game["opponent_round"]
        >= final_game["rounds"]
    ):

        await finish(
            game_id,
            context
        )


# =========================================================
# FINISH
# =========================================================

async def finish(game_id, context):

    game = get_game(
        game_id
    )

    if not game:
        return

    creator_score = int(
        game["creator_score"]
    )

    opponent_score = int(
        game["opponent_score"]
    )

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='finished'
        WHERE id=?
        AND status='playing'
        """, (
            game_id,
        ))

        con.commit()

    if creator_score > opponent_score:

        winner_name = (
            game["creator_name"]
        )

        result = (
            f"🏆 {winner_name} برنده شد!"
        )

    elif opponent_score > creator_score:

        winner_name = (
            game["opponent_name"]
        )

        result = (
            f"🏆 {winner_name} برنده شد!"
        )

    else:

        result = "🤝 بازی مساوی شد."

    text = (
        f"{game['emoji']} نتیجه بازی\n\n"
        f"👤 سازنده: {game['creator_name']}\n"
        f"👤 حریف: {game['opponent_name']}\n\n"
        f"📊 {game['creator_name']}: "
        f"{creator_score}\n"
        f"📊 {game['opponent_name']}: "
        f"{opponent_score}\n\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"{result}"
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        "💰 موجودی شما:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام گیرنده Reply کن و بنویس:\n\n"
        "انتقال 1"
    )


async def transfer_handler(update, context):

    msg = update.message

    if not msg:
        return

    text = digits(
        msg.text.strip()
    )

    if not text.startswith("انتقال"):
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    parts = text.split()

    if len(parts) != 2:

        await msg.reply_text(
            "❌ مثال:\n"
            "انتقال 0.1"
        )

        return

    try:

        amount = float(
            parts[1]
        )

    except ValueError:

        await msg.reply_text(
            "❌ مبلغ اشتباه است."
        )

        return

    if amount <= 0:
        return

    sender = update.effective_user

    receiver = (
        msg.reply_to_message.from_user
    )

    if not receiver:
        return

    if sender.id == receiver.id:

        await msg.reply_text(
            "❌ انتقال به خودت ممکن نیست."
        )

        return

    register(sender)
    register(receiver)

    async with GAME_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    sender.id,
                )).fetchone()

                if not row:

                    con.rollback()

                    await msg.reply_text(
                        "❌ کاربر پیدا نشد."
                    )

                    return

                current = float(
                    row["balance"]
                )

                if current < amount:

                    con.rollback()

                    await msg.reply_text(
                        "❌ موجودی کافی نیست."
                    )

                    return

                con.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
                """, (
                    amount,
                    sender.id
                ))

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount,
                    receiver.id
                ))

                con.commit()

            except Exception:

                con.rollback()

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {receiver.full_name}\n"
        f"💰 مقدار: {money(amount)} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی فعال نیست."
    )


# =========================================================
# HELP
# =========================================================

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
        "🤖 بازی با ربات:\n"
        "اول تمام پرتاب‌های کاربر، "
        "بعد تمام پرتاب‌های ربات.\n\n"
        "💰 موجودی:\n"
        "از دکمه موجودی.\n\n"
        "🔄 انتقال:\n"
        "Reply → انتقال 0.1"
    )


# =========================================================
# ADMIN
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش",
                callback_data="admin_add"
            ),
            InlineKeyboardButton(
                "➖ کاهش",
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


async def admin(update, context):

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

    context.user_data["admin"] = "add"

    await q.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ"
    )


async def admin_remove(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin"] = "remove"

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

    context.user_data["admin"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    with closing(db()) as con:

        users = con.execute("""
        SELECT COUNT(*) c
        FROM users
        """).fetchone()["c"]

        games_count = con.execute("""
        SELECT COUNT(*) c
        FROM games
        """).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) c
        FROM games
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games_count}\n"
        f"⏳ فعال: {active}"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update, context):

    if update.effective_user.id != OWNER_ID:
        return

    action = context.user_data.get(
        "admin"
    )

    if not action:
        return

    text = digits(
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
            "💰 موجودی:\n"
            f"{money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin",
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

        ok = add_balance(
            target,
            amount
        )

    else:

        ok = add_balance(
            target,
            -amount
        )

    if ok:

        await update.message.reply_text(
            "✅ انجام شد.\n\n"
            f"💰 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

    context.user_data.pop(
        "admin",
        None
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback(update, context):

    data = update.callback_query.data

    if data == "membership":

        q = update.callback_query

        ok = await member_ok(
            context.bot,
            q.from_user.id
        )

        await q.answer(
            "✅ عضو هستی."
            if ok
            else
            "❌ هنوز عضو نیستی.",
            show_alert=True
        )

        return

    if data == "games":
        await games(update, context)

    elif data == "examples":
        await examples(update, context)

    elif data == "friends":
        await friends(update, context)

    elif data == "robot_help":
        await robot_help(update, context)

    elif data == "balance":
        await balance_button(update, context)

    elif data == "transfer":
        await transfer_button(update, context)

    elif data == "withdraw":
        await withdraw(update, context)

    elif data == "help":
        await help_button(update, context)

    elif data == "admin":
        await admin(update, context)

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
            reply_markup=keyboard(
                q.from_user.id
            )
        )

    elif data.startswith("join:"):
        await join_game(update, context)

    elif data.startswith("robot:"):
        await robot_game(update, context)

    elif data.startswith("cancel:"):
        await cancel_game(update, context)


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update, context):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    register(user)

    text = digits(
        msg.text.strip()
    )

    if (
        user.id == OWNER_ID
        and context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context
        )

        return

    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    parsed = parse_game(text)

    if parsed:

        await create_game(
            update,
            context
        )

        return


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    log.error(
        "BOT ERROR: %s",
        context.error,
        exc_info=True
    )


# =========================================================
# MAIN
# =========================================================

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
        .concurrent_updates(False)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # دریافت ایموجی‌های واقعی تلگرام
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

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

    log.info(
        "BET_BT BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
