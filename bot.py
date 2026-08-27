import os
import re
import sqlite3
import random
import logging
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = 8552447077

FORCE_GROUP = "@zobxt"

DB_FILE = "bot.db"

MIN_GAME = 0.1
MAX_THROWS = 4

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db():
    return sqlite3.connect(
        DB_FILE,
        check_same_thread=False
    )


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            captcha_ok INTEGER DEFAULT 0,
            referrer INTEGER DEFAULT NULL,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            creator_id INTEGER,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT,
            amount REAL,
            mode TEXT,
            status TEXT,
            creator_throw INTEGER DEFAULT 0,
            opponent_throw INTEGER DEFAULT 0,
            round_no INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            request_type TEXT,
            amount REAL,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS support (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        INSERT OR IGNORE INTO settings(key,value)
        VALUES('enabled','1')
    """)

    cur.execute(
        "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
        (OWNER_ID,)
    )

    con.commit()
    con.close()


# =========================================================
# HELPERS
# =========================================================

def now():
    return datetime.utcnow().isoformat()


def fa_to_en(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def clean(text):
    text = fa_to_en(text)
    text = text.replace("ي", "ی")
    text = text.replace("ك", "ک")
    return text.strip()


def amount_from(text):
    try:
        text = fa_to_en(str(text))
        text = text.replace(",", ".").strip()

        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            return None

        value = float(text)

        if value <= 0:
            return None

        return round(value, 8)

    except Exception:
        return None


def money(value):
    s = f"{float(value):.8f}"
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def ensure_user(user, referrer=None):
    con = db()

    row = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if row is None:
        if referrer == user.id:
            referrer = None

        con.execute("""
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                balance,
                captcha_ok,
                referrer,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            0,
            0,
            referrer,
            now()
        ))
    else:
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

    con.commit()
    con.close()


def balance(user_id):
    con = db()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    return float(row[0]) if row else 0.0


def change_balance(user_id, value):
    con = db()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        con.close()
        return False

    old = float(row[0])
    new = round(old + value, 8)

    if new < 0:
        con.close()
        return False

    con.execute(
        "UPDATE users SET balance=? WHERE user_id=?",
        (new, user_id)
    )

    con.commit()
    con.close()

    return True


def is_admin(user_id):
    con = db()

    row = con.execute(
        "SELECT user_id FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    return row is not None


def enabled():
    con = db()

    row = con.execute(
        "SELECT value FROM settings WHERE key='enabled'"
    ).fetchone()

    con.close()

    return bool(row and row[0] == "1")


def set_enabled(value):
    con = db()

    con.execute("""
        UPDATE settings
        SET value=?
        WHERE key='enabled'
    """, ("1" if value else "0",))

    con.commit()
    con.close()


# =========================================================
# PRIVATE KEYBOARD
# =========================================================

def private_menu():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "➕ واریز"],
            ["➖ برداشت", "🎮 مثال بازی"],
            ["👥 زیرمجموعه", "🆘 پشتیبانی"],
        ],
        resize_keyboard=True
    )


def admin_menu():
    return ReplyKeyboardMarkup(
        [
            ["👤 اضافه کردن ادمین"],
            ["❌ حذف ادمین"],
            ["👥 کاربران"],
            ["💰 آمار موجودی"],
            ["🟢 روشن کردن ربات"],
            ["🔴 خاموش کردن ربات"],
            ["🔙 بازگشت"],
        ],
        resize_keyboard=True
    )


# =========================================================
# CAPTCHA
# =========================================================

async def captcha(update, context):
    a = random.randint(1, 9)
    b = random.randint(1, 9)

    answer = a + b

    choices = {answer}

    while len(choices) < 4:
        choices.add(random.randint(2, 18))

    choices = list(choices)
    random.shuffle(choices)

    context.user_data["captcha_answer"] = answer

    keyboard = []

    for x in choices:
        keyboard.append([
            InlineKeyboardButton(
                str(x),
                callback_data=f"captcha:{x}"
            )
        ])

    await update.effective_message.reply_text(
        f"🧩 کپچا\n\n"
        f"{a} + {b} = ؟\n\n"
        f"پاسخ را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# FORCE JOIN
# =========================================================

async def joined(update, context):
    try:
        member = await context.bot.get_chat_member(
            FORCE_GROUP,
            update.effective_user.id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception:
        return False


async def force_join(update, context):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در گپ",
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

    await update.effective_message.reply_text(
        "🔒 ابتدا در گپ اجباری عضو شوید.",
        reply_markup=keyboard
    )


# =========================================================
# START
# =========================================================

async def start(update, context):
    user = update.effective_user

    referrer = None

    if context.args:
        arg = context.args[0]

        if arg.startswith("ref_"):
            raw = fa_to_en(arg[4:])

            if raw.isdigit():
                referrer = int(raw)

    ensure_user(user, referrer)

    if not await joined(update, context):
        await force_join(update, context)
        return

    con = db()

    row = con.execute(
        "SELECT captcha_ok FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    con.close()

    if not row or row[0] == 0:
        await captcha(update, context)
        return

    await update.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=private_menu()
    )


# =========================================================
# GAME SCORES
# =========================================================

def game_score(game):
    if game == "تاس":
        return random.randint(1, 6)

    if game == "بولینگ":
        return random.randint(0, 10)

    if game == "بسکتبال":
        return random.randint(0, 5)

    if game == "دارت":
        return random.randint(0, 60)

    return 0


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(update, context, game, amount):
    user = update.effective_user

    if amount < MIN_GAME:
        await update.effective_message.reply_text(
            "❌ حداقل مبلغ بازی 0.1 TRX است."
        )
        return

    if balance(user.id) < amount:
        await update.effective_message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"robot:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="cancel"
            )
        ]
    ])

    await update.effective_message.reply_text(
        f"🎮 {game}\n\n"
        f"💰 مبلغ بازی: {money(amount)} TRX\n"
        f"👤 سازنده: {user.first_name}\n\n"
        "نوع بازی را انتخاب کنید:",
        reply_markup=keyboard
    )


# =========================================================
# CALLBACK
# =========================================================

async def callback(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    data = query.data

    # -------------------------
    # CAPTCHA
    # -------------------------

    if data.startswith("captcha:"):

        selected = int(data.split(":")[1])
        answer = context.user_data.get("captcha_answer")

        if selected != answer:
            await query.answer(
                "❌ پاسخ اشتباه است.",
                show_alert=True
            )
            return

        con = db()

        con.execute(
            "UPDATE users SET captcha_ok=1 WHERE user_id=?",
            (user.id,)
        )

        con.commit()
        con.close()

        await query.message.reply_text(
            "✅ کپچا صحیح بود.",
            reply_markup=private_menu()
        )

        return

    # -------------------------
    # JOIN
    # -------------------------

    if data == "check_join":

        if await joined(update, context):

            await query.message.reply_text(
                "✅ عضویت شما تأیید شد.",
                reply_markup=private_menu()
            )

        else:

            await query.message.reply_text(
                "❌ هنوز عضو گپ نیستید."
            )

        return

    # -------------------------
    # CANCEL
    # -------------------------

    if data == "cancel":

        await query.message.edit_text(
            "❌ بازی لغو شد."
        )

        return

    # -------------------------
    # FRIEND GAME
    # -------------------------

    if data.startswith("friend:"):

        parts = data.split(":")

        game = parts[1]
        amount = float(parts[2])

        if balance(user.id) < amount:
            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )
            return

        con = db()

        cur = con.execute("""
            INSERT INTO games
            (
                chat_id,
                message_id,
                creator_id,
                game_type,
                amount,
                mode,
                status,
                round_no,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            query.message.chat_id,
            query.message.message_id,
            user.id,
            game,
            amount,
            "friend",
            "waiting",
            1,
            now()
        ))

        game_id = cur.lastrowid

        con.commit()
        con.close()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎮 ورود به بازی",
                    callback_data=f"join:{game_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو بازی",
                    callback_data=f"cancelgame:{game_id}"
                )
            ]
        ])

        await query.message.edit_text(
            f"🎮 بازی {game}\n\n"
            f"💰 مبلغ: {money(amount)} TRX\n"
            f"👤 سازنده: {user.first_name}\n\n"
            "👥 یک نفر دیگر می‌تواند وارد بازی شود.",
            reply_markup=keyboard
        )

        return

    # -------------------------
    # JOIN FRIEND GAME
    # -------------------------

    if data.startswith("join:"):

        game_id = int(data.split(":")[1])

        con = db()

        game = con.execute("""
            SELECT
                creator_id,
                opponent_id,
                game_type,
                amount,
                status
            FROM games
            WHERE id=?
        """, (game_id,)).fetchone()

        con.close()

        if not game:
            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )
            return

        creator_id = game[0]
        opponent_id = game[1]
        game_type = game[2]
        amount = float(game[3])
        status = game[4]

        if status != "waiting" or opponent_id:
            await query.answer(
                "❌ یک نفر قبلاً وارد این بازی شده.",
                show_alert=True
            )
            return

        if creator_id == user.id:
            await query.answer(
                "❌ خودت سازنده بازی هستی.",
                show_alert=True
            )
            return

        if balance(user.id) < amount:
            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )
            return

        con = db()

        con.execute("""
            UPDATE games
            SET opponent_id=?,
                status='playing',
                round_no=1
            WHERE id=?
        """, (
            user.id,
            game_id
        ))

        con.commit()
        con.close()

        # هیچ کیبوردی در گپ ساخته نمی‌شود
        await query.message.edit_text(
            f"🎮 بازی {game_type}\n\n"
            f"👤 بازیکن اول: {creator_id}\n"
            f"👥 بازیکن دوم: {user.first_name}\n\n"
            "▶️ بازی شروع شد.\n"
            "اول بازیکن اول باید پرتاب کند."
        )

        await send_turn(
            context,
            query.message.chat_id,
            game_id
        )

        return

    # -------------------------
    # ROBOT GAME
    # -------------------------

    if data.startswith("robot:"):

        parts = data.split(":")

        game = parts[1]
        amount = float(parts[2])

        if balance(user.id) < amount:
            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )
            return

        await query.message.edit_text(
            "🤖 ربات در حال بازی است...\n\n"
            "⏳ در حال انجام پرتاب‌ها..."
        )

        player = game_score(game)
        bot = game_score(game)

        round_no = 1

        while player == bot and round_no < MAX_THROWS:
            round_no += 1
            player = game_score(game)
            bot = game_score(game)

        if player > bot:
            result = "🏆 شما برنده شدید."
        elif bot > player:
            result = "🤖 ربات برنده شد."
        else:
            result = "🤝 بازی مساوی شد."

        await query.message.reply_text(
            f"🎮 {game}\n\n"
            f"👤 شما: {player}\n"
            f"🤖 ربات: {bot}\n"
            f"🔄 پرتاب: {round_no}/{MAX_THROWS}\n\n"
            f"{result}"
        )

        return

    # -------------------------
    # CANCEL FRIEND GAME
    # -------------------------

    if data.startswith("cancelgame:"):

        game_id = int(data.split(":")[1])

        con = db()

        game = con.execute("""
            SELECT creator_id,status
            FROM games
            WHERE id=?
        """, (game_id,)).fetchone()

        if game and game[0] == user.id and game[1] == "waiting":

            con.execute("""
                UPDATE games
                SET status='cancelled'
                WHERE id=?
            """, (game_id,))

            con.commit()

            await query.message.edit_text(
                "❌ بازی لغو شد."
            )

        con.close()

        return


# =========================================================
# GAME TURN
# =========================================================

async def send_turn(context, chat_id, game_id):

    con = db()

    game = con.execute("""
        SELECT
            creator_id,
            opponent_id,
            game_type,
            round_no,
            creator_throw,
            opponent_throw,
            status
        FROM games
        WHERE id=?
    """, (game_id,)).fetchone()

    con.close()

    if not game:
        return

    creator_id = game[0]
    opponent_id = game[1]
    game_type = game[2]
    round_no = game[3]
    creator_throw = game[4]
    opponent_throw = game[5]
    status = game[6]

    if status != "playing":
        return

    # اول بازیکن اول
    if creator_throw == 0:

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎲 پرتاب",
                    callback_data=f"throw1:{game_id}"
                )
            ]
        ])

        await context.bot.send_message(
            chat_id,
            f"🎮 دور {round_no}/{MAX_THROWS}\n\n"
            f"👤 بازیکن اول باید {game_type} را پرتاب کند.",
            reply_markup=keyboard
        )

        return

    # سپس بازیکن دوم
    if opponent_throw == 0:

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🎲 پرتاب",
                    callback_data=f"throw2:{game_id}"
                )
            ]
        ])

        await context.bot.send_message(
            chat_id,
            f"🎮 دور {round_no}/{MAX_THROWS}\n\n"
            f"👥 بازیکن دوم باید {game_type} را پرتاب کند.",
            reply_markup=keyboard
        )

        return


# =========================================================
# THROW CALLBACK
# =========================================================

async def throw_callback(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    parts = data.split(":")

    if len(parts) != 2:
        return

    throw_type = parts[0]
    game_id = int(parts[1])

    con = db()

    game = con.execute("""
        SELECT
            creator_id,
            opponent_id,
            game_type,
            amount,
            round_no,
            creator_throw,
            opponent_throw,
            status
        FROM games
        WHERE id=?
    """, (game_id,)).fetchone()

    con.close()

    if not game:
        return

    creator_id = game[0]
    opponent_id = game[1]
    game_type = game[2]
    amount = float(game[3])
    round_no = game[4]
    creator_throw = game[5]
    opponent_throw = game[6]
    status = game[7]

    if status != "playing":
        return

    # بازیکن اول
    if throw_type == "throw1":

        if user.id != creator_id:
            await query.answer(
                "❌ نوبت شما نیست.",
                show_alert=True
            )
            return

        if creator_throw != 0:
            return

        score = game_score(game_type)

        con = db()

        con.execute("""
            UPDATE games
            SET creator_throw=?
            WHERE id=?
        """, (
            score,
            game_id
        ))

        con.commit()
        con.close()

        await query.message.edit_text(
            f"🎮 {game_type}\n\n"
            f"👤 بازیکن اول پرتاب کرد: {score}\n\n"
            "⏳ حالا نوبت بازیکن دوم است."
        )

        await send_turn(
            context,
            query.message.chat_id,
            game_id
        )

        return

    # بازیکن دوم
    if throw_type == "throw2":

        if user.id != opponent_id:
            await query.answer(
                "❌ نوبت شما نیست.",
                show_alert=True
            )
            return

        if opponent_throw != 0:
            return

        score = game_score(game_type)

        con = db()

        con.execute("""
            UPDATE games
            SET opponent_throw=?
            WHERE id=?
        """, (
            score,
            game_id
        ))

        con.commit()
        con.close()

        # نتیجه دور
        if creator_throw > score:
            winner = "👤 بازیکن اول"
        elif score > creator_throw:
            winner = "👥 بازیکن دوم"
        else:
            winner = "🤝 مساوی"

        await query.message.edit_text(
            f"🎮 {game_type}\n\n"
            f"👤 بازیکن اول: {creator_throw}\n"
            f"👥 بازیکن دوم: {score}\n\n"
            f"نتیجه این دور: {winner}"
        )

        # مساوی
        if creator_throw == score:

            if round_no >= MAX_THROWS:

                con = db()

                con.execute("""
                    UPDATE games
                    SET status='finished'
                    WHERE id=?
                """, (game_id,))

                con.commit()
                con.close()

                await query.message.reply_text(
                    "🤝 بعد از ۴ پرتاب هنوز مساوی شد.\n"
                    "بازی بدون برنده تمام شد."
                )

                return

            con = db()

            con.execute("""
                UPDATE games
                SET
                    creator_throw=0,
                    opponent_throw=0,
                    round_no=round_no+1
                WHERE id=?
            """, (game_id,))

            con.commit()
            con.close()

            await query.message.reply_text(
                f"🔄 مساوی شد!\n"
                f"دور بعدی: {round_no + 1}/{MAX_THROWS}"
            )

            await send_turn(
                context,
                query.message.chat_id,
                game_id
            )

            return

        # برنده مشخص شد
        if creator_throw > score:
            winner_id = creator_id
            winner_text = "👤 بازیکن اول برنده شد!"
        else:
            winner_id = opponent_id
            winner_text = "👥 بازیکن دوم برنده شد!"

        # این نسخه فقط امتیاز داخلی را ثبت می‌کند.
        # برای جلوگیری از شرط‌بندی واقعی، انتقال TRX واقعی وجود ندارد.

        con = db()

        con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
        """, (game_id,))

        con.commit()
        con.close()

        await query.message.reply_text(
            f"🏆 {winner_text}\n\n"
            f"💰 مبلغ اعلام‌شده بازی: {money(amount)} TRX\n"
            f"🔄 تعداد دور: {round_no}/{MAX_THROWS}"
        )


# =========================================================
# PRIVATE TEXT
# =========================================================

async def text_handler(update, context):

    if not update.message:
        return

    user = update.effective_user
    text = clean(update.message.text)

    ensure_user(user)

    # =====================================================
    # GROUP
    # =====================================================

    if update.effective_chat.type in (
        "group",
        "supergroup"
    ):

        # هیچ ReplyKeyboard در گروه ارسال نمی‌کنیم

        if not enabled() and not is_admin(user.id):
            return

        # ------------------------------
        # انتقال - فقط پیام راهنما
        # ------------------------------

        if text.startswith("انتقال "):

            parts = text.split()

            if len(parts) != 2:
                await update.message.reply_text(
                    "❌ فرمت صحیح:\n"
                    "انتقال 0.1\n"
                    "انتقال ۰.۱"
                )
                return

            amount = amount_from(parts[1])

            if amount is None:
                await update.message.reply_text(
                    "❌ مقدار صحیح نیست."
                )
                return

            # انتقال واقعی/قابل برداشت انجام نمی‌شود
            await update.message.reply_text(
                f"ℹ️ درخواست انتقال {money(amount)} TRX "
                "در این نسخه انتقال واقعی انجام نمی‌دهد."
            )

            return

        # ------------------------------
        # GAME COMMAND
        # ------------------------------

        match = re.match(
            r"^1\s+(تاس|بولینگ|بسکتبال|دارت|بسکتبال)\s+(.+)$",
            text
        )

        if match:

            game = match.group(1)
            amount = amount_from(match.group(2))

            if amount is None:
                await update.message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )
                return

            await create_game(
                update,
                context,
                game,
                amount
            )

            return

        # هیچ منوی پیوی در گروه نشان داده نمی‌شود
        return

    # =====================================================
    # PRIVATE
    # =====================================================

    if not await joined(update, context):
        await force_join(update, context)
        return

    # CAPTCHA
    con = db()

    row = con.execute(
        "SELECT captcha_ok FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    con.close()

    if not row or row[0] == 0:
        await captcha(update, context)
        return

    # BOT OFF
    if not enabled() and not is_admin(user.id):
        await update.message.reply_text(
            "🔴 ربات خاموش است."
        )
        return

    # =====================================================
    # BALANCE
    # =====================================================

    if text == "💰 موجودی":

        await update.message.reply_text(
            "💰 موجودی شما:\n\n"
            f"{money(balance(user.id))} TRX"
        )

        return

    # =====================================================
    # DEPOSIT REQUEST
    # =====================================================

    if text == "➕ واریز":

        context.user_data["state"] = "deposit"

        await update.message.reply_text(
            "➕ مقدار درخواست را وارد کنید.\n\n"
            "حداقل: 0.5 TRX\n\n"
            "مثال:\n"
            "0.5\n"
            "۰.۵"
        )

        return

    # =====================================================
    # WITHDRAW REQUEST
    # =====================================================

    if text == "➖ برداشت":

        await update.message.reply_text(
            "➖ برداشت واقعی TRX در این نسخه فعال نیست."
        )

        return

    # =====================================================
    # GAME EXAMPLE
    # =====================================================

    if text == "🎮 مثال بازی":

        await update.message.reply_text(
            "🎮 دستورات بازی در گپ:\n\n"
            "1 تاس 0.1\n"
            "1 تاس ۰.۱\n"
            "1 بولینگ 0.1\n"
            "1 بسکتبال 0.1\n"
            "1 دارت 0.1\n\n"
            "اعداد فارسی و انگلیسی قبول است."
        )

        return

    # =====================================================
    # REFERRAL
    # =====================================================

    if text == "👥 زیرمجموعه":

        me = await context.bot.get_me()

        link = (
            f"https://t.me/{me.username}"
            f"?start=ref_{user.id}"
        )

        con = db()

        row = con.execute(
            "SELECT COUNT(*) FROM users WHERE referrer=?",
            (user.id,)
        ).fetchone()

        con.close()

        await update.message.reply_text(
            "👥 زیرمجموعه\n\n"
            f"🔗 لینک شما:\n{link}\n\n"
            f"👤 تعداد: {row[0]}\n"
            "🎁 پاداش نمایشی هر نفر: 0.05 TRX"
        )

        return

    # =====================================================
    # SUPPORT
    # =====================================================

    if text == "🆘 پشتیبانی":

        context.user_data["state"] = "support"

        await update.message.reply_text(
            "🆘 پیام خود را ارسال کنید."
        )

        return

    # =====================================================
    # ADMIN
    # =====================================================

    if text == "پنل مدیریت":

        if user.id != OWNER_ID:
            await update.message.reply_text(
                "❌ دسترسی ندارید."
            )
            return

        await update.message.reply_text(
            "🛡 پنل مدیریت",
            reply_markup=admin_menu()
        )

        return

    if text == "👥 کاربران":

        if not is_admin(user.id):
            return

        con = db()

        count = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        con.close()

        await update.message.reply_text(
            f"👥 کاربران: {count}"
        )

        return

    if text == "💰 آمار موجودی":

        if not is_admin(user.id):
            return

        con = db()

        total = con.execute(
            "SELECT COALESCE(SUM(balance),0) FROM users"
        ).fetchone()[0]

        con.close()

        await update.message.reply_text(
            f"💰 مجموع موجودی داخلی:\n"
            f"{money(total)} TRX"
        )

        return

    if text == "🟢 روشن کردن ربات":

        if user.id != OWNER_ID:
            return

        set_enabled(True)

        await update.message.reply_text(
            "🟢 ربات روشن شد."
        )

        return

    if text == "🔴 خاموش کردن ربات":

        if user.id != OWNER_ID:
            return

        set_enabled(False)

        await update.message.reply_text(
            "🔴 ربات خاموش شد."
        )

        return

    if text == "🔙 بازگشت":

        await update.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=private_menu()
        )

        return

    # =====================================================
    # DEPOSIT STATE
    # =====================================================

    if context.user_data.get("state") == "deposit":

        amount = amount_from(text)

        if amount is None:
            await update.message.reply_text(
                "❌ مقدار صحیح نیست."
            )
            return

        if amount < 0.5:
            await update.message.reply_text(
                "❌ حداقل مقدار 0.5 TRX است."
            )
            return

        con = db()

        cur = con.execute("""
            INSERT INTO requests
            (
                user_id,
                request_type,
                amount,
                status,
                created_at
            )
            VALUES(?,?,?,?,?)
        """, (
            user.id,
            "deposit",
            amount,
            "pending",
            now()
        ))

        request_id = cur.lastrowid

        con.commit()
        con.close()

        context.user_data["state"] = None

        # فقط اطلاع‌رسانی درخواست؛ واریز واقعی انجام نمی‌شود
        for admin_id in get_admins():

            try:
                await context.bot.send_message(
                    admin_id,
                    "➕ درخواست واریز داخلی\n\n"
                    f"👤 کاربر: {user.id}\n"
                    f"💰 مقدار: {money(amount)} TRX\n"
                    f"🔢 درخواست: #{request_id}\n\n"
                    "این نسخه انتقال واقعی TRX ندارد."
                )
            except Exception:
                pass

        await update.message.reply_text(
            "✅ درخواست ثبت شد.\n\n"
            f"💰 {money(amount)} TRX\n"
            f"🔢 #{request_id}\n\n"
            "برای بررسی به ادمین ارسال شد.",
            reply_markup=private_menu()
        )

        return

    # =====================================================
    # SUPPORT STATE
    # =====================================================

    if context.user_data.get("state") == "support":

        con = db()

        cur = con.execute("""
            INSERT INTO support
            (
                user_id,
                message,
                created_at
            )
            VALUES(?,?,?)
        """, (
            user.id,
            update.message.text,
            now()
        ))

        ticket = cur.lastrowid

        con.commit()
        con.close()

        for admin_id in get_admins():

            try:
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💬 پاسخ به کاربر",
                            callback_data=f"reply:{user.id}"
                        )
                    ]
                ])

                await context.bot.send_message(
                    admin_id,
                    "🆘 پیام پشتیبانی\n\n"
                    f"👤 {user.first_name}\n"
                    f"🆔 {user.id}\n"
                    f"🎫 #{ticket}\n\n"
                    f"{update.message.text}",
                    reply_markup=keyboard
                )

            except Exception:
                pass

        context.user_data["state"] = None

        await update.message.reply_text(
            "✅ پیام شما برای ادمین ارسال شد.",
            reply_markup=private_menu()
        )

        return


# =========================================================
# ADMINS
# =========================================================

def get_admins():
    con = db()

    rows = con.execute(
        "SELECT user_id FROM admins"
    ).fetchall()

    con.close()

    return [x[0] for x in rows]


# =========================================================
# ERRORS
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "ERROR",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN در GitHub Secrets پیدا نشد."
        )

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
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
            throw_callback,
            pattern=r"^throw[12]:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info("BOT STARTED")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
