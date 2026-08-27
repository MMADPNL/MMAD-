# ============================================================
# INTERNAL TRX GAME BOT
# Python 3.10+
# python-telegram-bot 21.6
#
# داخلی و غیرواقعی:
# TRX فقط واحد حساب داخل ربات است و به شبکه TRON متصل نیست.
# ============================================================

import os
import sqlite3
import asyncio
import logging
import threading
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

OWNER_ID = 8552447077

DB_FILE = "bot.db"

MIN_BET = 0.10

# سهم داخلی مالک؛ در پیام گروه نمایش داده نمی‌شود.
OWNER_GAME_FEE = 0.02

# بازی‌های فعال
GAMES = {
    "تاس": "dice",
    "بولینگ": "bowling",
    "دارت": "darts",
    "بسکتبال": "basketball",
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def db():
    return sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30,
    )


def init_db():
    with db_lock:
        conn = db()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance REAL DEFAULT 0,
                created_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS games (
                game_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                creator_id INTEGER,
                opponent_id INTEGER DEFAULT NULL,
                game_type TEXT,
                bet REAL,
                mode TEXT,
                status TEXT,
                created_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                kind TEXT,
                description TEXT,
                created_at TEXT
            )
            """
        )

        conn.commit()
        conn.close()


# ============================================================
# USER FUNCTIONS
# ============================================================

def ensure_user(user):
    if not user:
        return

    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO users
                (user_id, username, first_name, balance, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    user.username or "",
                    user.first_name or "",
                    0,
                    datetime.utcnow().isoformat(),
                ),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET username=?, first_name=?
                WHERE user_id=?
                """,
                (
                    user.username or "",
                    user.first_name or "",
                    user.id,
                ),
            )

        conn.commit()
        conn.close()


def get_balance(user_id):
    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        conn.close()

    if not row:
        return 0.0

    return float(row[0])


def change_balance(user_id, amount, kind, description):
    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT balance FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()

        if not row:
            conn.close()
            return False

        current = float(row[0])
        new_balance = round(current + amount, 4)

        if new_balance < 0:
            conn.close()
            return False

        conn.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, user_id),
        )

        conn.execute(
            """
            INSERT INTO transactions
            (user_id, amount, kind, description, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                kind,
                description,
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
        conn.close()

    return True


def add_balance(user_id, amount, description="شارژ داخلی"):
    return change_balance(
        user_id,
        amount,
        "credit",
        description,
    )


def remove_balance(user_id, amount, description="کسر بازی"):
    return change_balance(
        user_id,
        -abs(amount),
        "debit",
        description,
    )


# ============================================================
# TEXT HELPERS
# ============================================================

def money(value):
    return f"{float(value):.2f} TRX"


def user_name(user):
    if user.username:
        return f"@{user.username}"

    return user.first_name or str(user.id)


# ============================================================
# GAME STORAGE
# ============================================================

active_games = {}

game_lock = asyncio.Lock()


def create_game_db(
    chat_id,
    creator_id,
    game_type,
    bet,
    mode="waiting",
):
    with db_lock:
        conn = db()

        cur = conn.execute(
            """
            INSERT INTO games
            (chat_id, creator_id, game_type, bet, mode, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                creator_id,
                game_type,
                bet,
                mode,
                "waiting",
                datetime.utcnow().isoformat(),
            ),
        )

        game_id = cur.lastrowid

        conn.commit()
        conn.close()

    return game_id


def finish_game_db(game_id, status, opponent_id=None):
    with db_lock:
        conn = db()

        conn.execute(
            """
            UPDATE games
            SET status=?, opponent_id=?
            WHERE game_id=?
            """,
            (
                status,
                opponent_id,
                game_id,
            ),
        )

        conn.commit()
        conn.close()


# ============================================================
# GAME BUTTONS
# ============================================================

def game_keyboard(game_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 بازی با دوستان",
                    callback_data=f"friend:{game_id}",
                ),
                InlineKeyboardButton(
                    "🤖 بازی با ربات",
                    callback_data=f"bot:{game_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"cancel:{game_id}",
                )
            ],
        ]
    )


# ============================================================
# GAME ROLLS
# ============================================================

async def send_game_roll(bot, chat_id, game_type):
    """
    Returns Telegram dice message and numeric value.
    """

    emoji = {
        "dice": "🎲",
        "bowling": "🎳",
        "darts": "🎯",
        "basketball": "🏀",
    }.get(game_type, "🎲")

    message = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji,
    )

    value = message.dice.value

    return message, value


# ============================================================
# PLAY AGAINST BOT
# ============================================================

async def play_against_bot(
    query,
    game_id,
):
    async with game_lock:

        game = active_games.get(game_id)

        if not game:
            await query.answer(
                "این بازی دیگر فعال نیست.",
                show_alert=True,
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "ربات در حال بازی هست.",
                show_alert=True,
            )
            return

        game["status"] = "playing_bot"

        creator_id = game["creator_id"]
        bet = game["bet"]
        game_type = game["game_type"]
        chat_id = game["chat_id"]

        balance = get_balance(creator_id)

        if balance < bet:
            game["status"] = "waiting"

            await query.answer(
                "موجودی کافی نیست.",
                show_alert=True,
            )
            return

        # پول بازی از بازیکن کم می‌شود.
        if not remove_balance(
            creator_id,
            bet,
            "ورود به بازی",
        ):
            game["status"] = "waiting"

            await query.answer(
                "خطا در موجودی.",
                show_alert=True,
            )
            return

        await query.answer("بازی با ربات شروع شد!")

        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        await query.message.reply_text(
            f"🤖 بازی با ربات شروع شد!\n\n"
            f"🎮 بازی: {game['title']}\n"
            f"💰 مبلغ: {money(bet)}\n\n"
            f"⏳ اول سازنده بازی می‌ریزد..."
        )

        player_score = 0
        bot_score = 0

        while True:

            await asyncio.sleep(1)

            await query.message.reply_text(
                "🎲 نوبت سازنده بازی..."
            )

            _, player_score = await send_game_roll(
                query.bot,
                chat_id,
                game_type,
            )

            await asyncio.sleep(1)

            await query.message.reply_text(
                "🤖 نوبت ربات..."
            )

            _, bot_score = await send_game_roll(
                query.bot,
                chat_id,
                game_type,
            )

            await asyncio.sleep(1)

            if player_score == bot_score:
                await query.message.reply_text(
                    f"🤝 مساوی شد!\n"
                    f"عدد: {player_score}\n\n"
                    f"🔄 دوباره می‌ریزید..."
                )
                continue

            break

        if player_score > bot_score:
            winner = "player"

            # مبلغ برنده داخلی
            reward = max(
                0,
                round(
                    (bet * 2) - OWNER_GAME_FEE,
                    4,
                ),
            )

            add_balance(
                creator_id,
                reward,
                "برد بازی با ربات",
            )

            result = (
                f"🏆 {user_name(query.from_user)} برنده شد!\n\n"
                f"👤 شما: {player_score}\n"
                f"🤖 ربات: {bot_score}\n"
                f"💰 جایزه: {money(reward)}"
            )

        else:
            winner = "bot"

            result = (
                f"🤖 ربات برنده شد!\n\n"
                f"👤 شما: {player_score}\n"
                f"🤖 ربات: {bot_score}\n"
                f"💰 مبلغ بازی: {money(bet)}"
            )

        game["status"] = "finished"

        finish_game_db(
            game_id,
            "finished_bot",
        )

        await query.message.reply_text(result)

        active_games.pop(game_id, None)


# ============================================================
# FRIEND GAME
# ============================================================

async def start_friend_game(query, game_id):
    async with game_lock:

        game = active_games.get(game_id)

        if not game:
            await query.answer(
                "بازی پیدا نشد.",
                show_alert=True,
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "این بازی قبلاً شروع شده.",
                show_alert=True,
            )
            return

        creator_id = game["creator_id"]

        if query.from_user.id == creator_id:
            await query.answer(
                "شما سازنده بازی هستید؛ منتظر بازیکن دوم باشید.",
                show_alert=True,
            )
            return

        opponent_id = query.from_user.id
        bet = game["bet"]

        if get_balance(creator_id) < bet:
            await query.answer(
                "موجودی سازنده بازی کافی نیست.",
                show_alert=True,
            )
            return

        if get_balance(opponent_id) < bet:
            await query.answer(
                "موجودی شما کافی نیست.",
                show_alert=True,
            )
            return

        # قفل بازی
        game["status"] = "playing_friend"
        game["opponent_id"] = opponent_id

        # کسر مبلغ از هر دو نفر
        if not remove_balance(
            creator_id,
            bet,
            "ورود به بازی دوستانه",
        ):
            game["status"] = "waiting"
            await query.answer("خطا.", show_alert=True)
            return

        if not remove_balance(
            opponent_id,
            bet,
            "ورود به بازی دوستانه",
        ):
            add_balance(
                creator_id,
                bet,
                "برگشت مبلغ بازی",
            )

            game["status"] = "waiting"

            await query.answer(
                "خطا در موجودی بازیکن دوم.",
                show_alert=True,
            )
            return

        finish_game_db(
            game_id,
            "playing_friend",
            opponent_id,
        )

        try:
            await query.edit_message_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        chat_id = game["chat_id"]
        game_type = game["game_type"]

        await query.answer("بازی شروع شد!")

        await query.message.reply_text(
            f"👥 بازی با دوستان شروع شد!\n\n"
            f"👤 سازنده: {game['creator_name']}\n"
            f"👤 بازیکن دوم: {user_name(query.from_user)}\n"
            f"🎮 بازی: {game['title']}\n"
            f"💰 مبلغ: {money(bet)}\n\n"
            f"⏳ اول سازنده بازی می‌ریزد..."
        )

        while True:

            await asyncio.sleep(1)

            await query.message.reply_text(
                f"🎮 نوبت {game['creator_name']}..."
            )

            _, first_score = await send_game_roll(
                query.bot,
                chat_id,
                game_type,
            )

            await asyncio.sleep(1)

            await query.message.reply_text(
                f"🎮 نوبت {user_name(query.from_user)}..."
            )

            _, second_score = await send_game_roll(
                query.bot,
                chat_id,
                game_type,
            )

            await asyncio.sleep(1)

            if first_score == second_score:

                await query.message.reply_text(
                    f"🤝 مساوی شد!\n"
                    f"هر دو: {first_score}\n\n"
                    f"🔄 دوباره می‌ریزید..."
                )

                continue

            break

        if first_score > second_score:

            winner_id = creator_id
            winner_name = game["creator_name"]

        else:

            winner_id = opponent_id
            winner_name = user_name(query.from_user)

        reward = max(
            0,
            round(
                (bet * 2) - OWNER_GAME_FEE,
                4,
            ),
        )

        add_balance(
            winner_id,
            reward,
            "برد بازی دوستانه",
        )

        finish_game_db(
            game_id,
            "finished_friend",
            opponent_id,
        )

        await query.message.reply_text(
            f"🏆 برنده: {winner_name}\n\n"
            f"🎮 {game['title']}\n"
            f"👤 سازنده: {first_score}\n"
            f"👤 بازیکن دوم: {second_score}\n\n"
            f"💰 جایزه: {money(reward)}"
        )

        active_games.pop(game_id, None)


# ============================================================
# CREATE GAME
# ============================================================

async def create_game_from_message(
    update,
    game_title,
    game_type,
    bet,
):

    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if bet < MIN_BET:

        await update.message.reply_text(
            f"❌ حداقل مبلغ بازی {money(MIN_BET)} است."
        )

        return

    balance = get_balance(user.id)

    if balance < bet:

        await update.message.reply_text(
            f"❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی شما: {money(balance)}\n"
            f"🎮 مبلغ بازی: {money(bet)}"
        )

        return

    game_id = create_game_db(
        chat.id,
        user.id,
        game_type,
        bet,
        "waiting",
    )

    active_games[game_id] = {
        "game_id": game_id,
        "chat_id": chat.id,
        "creator_id": user.id,
        "creator_name": user_name(user),
        "game_type": game_type,
        "title": game_title,
        "bet": bet,
        "status": "waiting",
        "opponent_id": None,
    }

    await update.message.reply_text(
        f"🎮 بازی جدید ساخته شد!\n\n"
        f"👤 سازنده: {user_name(user)}\n"
        f"🎮 بازی: {game_title}\n"
        f"💰 مبلغ: {money(bet)}\n\n"
        f"یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=game_keyboard(game_id),
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🤖 ربات بازی فعال است.\n\n"
        "💰 موجودی شما با واحد TRX داخلی محاسبه می‌شود.\n\n"
        "دستورها:\n"
        "/balance - موجودی\n"
        "/games - راهنمای بازی\n"
        "/help - راهنما"
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    ensure_user(user)

    bal = get_balance(user.id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"**{money(bal)}**",
        parse_mode="Markdown",
    )


async def games_help(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🎮 بازی‌های موجود:\n\n"
        "🎲 تاس\n"
        "🎳 بولینگ\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n\n"
        "مثال:\n"
        "`1 تاس 0.1`\n"
        "`1 بولینگ 0.1`\n"
        "`1 دارت 0.1`\n"
        "`1 بسکتبال 0.1`\n\n"
        f"حداقل مبلغ: {money(MIN_BET)}",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "📚 راهنما\n\n"
        "/start\n"
        "/balance\n"
        "/games\n"
        "/help\n\n"
        "در گروه می‌توانید بنویسید:\n"
        "`1 تاس 0.1`\n"
        "`1 بولینگ 0.1`\n"
        "`1 دارت 0.1`\n"
        "`1 بسکتبال 0.1`",
        parse_mode="Markdown",
    )


# ============================================================
# ADMIN PANEL
# ============================================================

def is_owner(user_id):
    return user_id == OWNER_ID


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 آمار",
                    callback_data="admin:stats",
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 افزایش موجودی",
                    callback_data="admin:add",
                ),
                InlineKeyboardButton(
                    "➖ کاهش موجودی",
                    callback_data="admin:remove",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👥 کاربران",
                    callback_data="admin:users",
                )
            ],
        ]
    )

    await update.message.reply_text(
        "👑 پنل مدیریت\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=keyboard,
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(query, context):

    if not is_owner(query.from_user.id):

        await query.answer(
            "دسترسی ندارید.",
            show_alert=True,
        )

        return

    action = query.data.split(":", 1)[1]

    if action == "stats":

        with db_lock:
            conn = db()

            users = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = conn.execute(
                "SELECT COALESCE(SUM(balance),0) FROM users"
            ).fetchone()[0]

            games = conn.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

            conn.close()

        await query.answer()

        await query.message.reply_text(
            f"📊 آمار ربات\n\n"
            f"👥 کاربران: {users}\n"
            f"🎮 بازی‌ها: {games}\n"
            f"💰 مجموع موجودی داخلی: {money(total)}"
        )

        return

    if action == "users":

        with db_lock:
            conn = db()

            rows = conn.execute(
                """
                SELECT user_id, username, first_name, balance
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
            for row in rows:
                uid, username, first_name, bal = row

                name = (
                    f"@{username}"
                    if username
                    else first_name or str(uid)
                )

                text += (
                    f"👤 {name}\n"
                    f"🆔 `{uid}`\n"
                    f"💰 {money(bal)}\n\n"
                )

        await query.answer()

        await query.message.reply_text(
            text,
            parse_mode="Markdown",
        )

        return

    if action in ("add", "remove"):

        context.user_data["admin_action"] = action

        await query.answer()

        await query.message.reply_text(
            "فرمت را ارسال کنید:\n\n"
            "`USER_ID AMOUNT`\n\n"
            "مثال:\n"
            "`123456789 10`",
            parse_mode="Markdown",
        )

        return


# ============================================================
# ADMIN TEXT INPUT
# ============================================================

async def admin_text(update, context):

    user = update.effective_user

    if not is_owner(user.id):
        return

    action = context.user_data.get("admin_action")

    if not action:
        return

    parts = update.message.text.strip().split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت اشتباه است.\n"
            "مثال: `123456789 10`",
            parse_mode="Markdown",
        )

        return

    try:
        target_id = int(parts[0])
        amount = float(parts[1])

        if amount <= 0:
            raise ValueError

    except Exception:

        await update.message.reply_text(
            "❌ مقدار نامعتبر است."
        )

        return

    if action == "add":

        ok = add_balance(
            target_id,
            amount,
            "شارژ توسط مالک",
        )

        if ok:

            new_bal = get_balance(target_id)

            await update.message.reply_text(
                f"✅ موجودی اضافه شد.\n\n"
                f"👤 کاربر: `{target_id}`\n"
                f"➕ مبلغ: {money(amount)}\n"
                f"💰 موجودی جدید: {money(new_bal)}",
                parse_mode="Markdown",
            )

        else:

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

    elif action == "remove":

        ok = remove_balance(
            target_id,
            amount,
            "کاهش موجودی توسط مالک",
        )

        if ok:

            new_bal = get_balance(target_id)

            await update.message.reply_text(
                f"✅ موجودی کم شد.\n\n"
                f"👤 کاربر: `{target_id}`\n"
                f"➖ مبلغ: {money(amount)}\n"
                f"💰 موجودی جدید: {money(new_bal)}",
                parse_mode="Markdown",
            )

        else:

            await update.message.reply_text(
                "❌ کاربر پیدا نشد یا موجودی کافی نیست."
            )

    context.user_data.pop("admin_action", None)


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    data = query.data

    if data.startswith("admin:"):

        await admin_callback(
            query,
            context,
        )

        return

    try:

        action, raw_id = data.split(":", 1)

        game_id = int(raw_id)

    except Exception:

        await query.answer(
            "داده نامعتبر است.",
            show_alert=True,
        )

        return

    if action == "cancel":

        async with game_lock:

            game = active_games.get(game_id)

            if not game:

                await query.answer(
                    "بازی پیدا نشد.",
                    show_alert=True,
                )

                return

            if query.from_user.id != game["creator_id"]:

                await query.answer(
                    "فقط سازنده می‌تواند بازی را لغو کند.",
                    show_alert=True,
                )

                return

            game["status"] = "cancelled"

            finish_game_db(
                game_id,
                "cancelled",
            )

            active_games.pop(
                game_id,
                None,
            )

            await query.answer(
                "بازی لغو شد."
            )

            try:
                await query.edit_message_text(
                    "❌ این بازی توسط سازنده لغو شد."
                )
            except Exception:
                pass

        return

    if action == "friend":

        await start_friend_game(
            query,
            game_id,
        )

        return

    if action == "bot":

        await play_against_bot(
            query,
            game_id,
        )

        return


# ============================================================
# GROUP GAME PARSER
# ============================================================

def parse_game(text):

    if not text:
        return None

    text = text.strip()

    parts = text.split()

    if len(parts) != 3:
        return None

    # فقط فرمت:
    # 1 تاس 0.1
    if parts[0] != "1":
        return None

    game_name = parts[1]

    try:
        bet = float(parts[2])
    except Exception:
        return None

    if game_name not in GAMES:
        return None

    if bet <= 0:
        return None

    return (
        game_name,
        GAMES[game_name],
        bet,
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    text = update.message.text or ""

    # پنل ادمین
    if update.effective_chat.type == ChatType.PRIVATE:

        if is_owner(user.id):

            if context.user_data.get("admin_action"):

                await admin_text(
                    update,
                    context,
                )

                return

    # بازی فقط در گروه
    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    parsed = parse_game(text)

    if not parsed:
        return

    game_name, game_type, bet = parsed

    await create_game_from_message(
        update,
        game_name,
        game_type,
        bet,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Unhandled exception:",
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

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance,
        )
    )

    application.add_handler(
        CommandHandler(
            "games",
            games_help,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "INTERNAL TRX GAME BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
