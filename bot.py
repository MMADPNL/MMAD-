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

# اعتبار بازی داخلی؛ ارزش نقدی ندارد
START_BALANCE = 0.0

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
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
            referrer INTEGER DEFAULT NULL,
            captcha_ok INTEGER DEFAULT 0,
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
            opponent_id INTEGER,
            game_type TEXT,
            amount REAL,
            mode TEXT,
            status TEXT,
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
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender INTEGER,
            receiver INTEGER,
            amount REAL,
            created_at TEXT
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('enabled','1')"
    )

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


def normalize_digits(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def normalize_text(text):
    text = normalize_digits(text)
    text = text.replace("ي", "ی")
    text = text.replace("ك", "ک")
    return text.strip()


def parse_amount(value):
    try:
        value = normalize_digits(value)
        value = value.replace(",", ".")
        value = value.strip()

        if not re.fullmatch(r"\d+(?:\.\d+)?", value):
            return None

        amount = float(value)

        if amount <= 0:
            return None

        return round(amount, 8)

    except Exception:
        return None


def fmt(amount):
    return f"{float(amount):.8f}".rstrip("0").rstrip(".")


def is_enabled():
    con = db()
    row = con.execute(
        "SELECT value FROM settings WHERE key='enabled'"
    ).fetchone()
    con.close()

    return row and row[0] == "1"


def set_enabled(value):
    con = db()
    con.execute(
        "UPDATE settings SET value=? WHERE key='enabled'",
        ("1" if value else "0",)
    )
    con.commit()
    con.close()


def ensure_user(user, referrer=None):
    con = db()

    row = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if row is None:

        if referrer == user.id:
            referrer = None

        con.execute(
            """
            INSERT INTO users
            (user_id,username,first_name,balance,referrer,captcha_ok,created_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                START_BALANCE,
                referrer,
                0,
                now(),
            )
        )

    else:

        con.execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id,
            )
        )

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
        return 0

    return float(row[0])


def change_balance(user_id, amount):
    con = db()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:
        con.close()
        return False

    new_balance = round(
        float(row[0]) + float(amount),
        8
    )

    if new_balance < 0:
        con.close()
        return False

    con.execute(
        "UPDATE users SET balance=? WHERE user_id=?",
        (new_balance, user_id)
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


def add_admin(user_id):
    con = db()
    con.execute(
        "INSERT OR IGNORE INTO admins(user_id) VALUES(?)",
        (user_id,)
    )
    con.commit()
    con.close()


def remove_admin(user_id):
    if user_id == OWNER_ID:
        return False

    con = db()
    cur = con.execute(
        "DELETE FROM admins WHERE user_id=?",
        (user_id,)
    )
    con.commit()
    con.close()

    return cur.rowcount > 0


# =========================================================
# MENUS
# =========================================================


def main_menu():

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

    user = update.effective_user

    a = random.randint(1, 9)
    b = random.randint(1, 9)

    answer = a + b

    options = {answer}

    while len(options) < 4:
        options.add(random.randint(2, 18))

    options = list(options)
    random.shuffle(options)

    context.user_data["captcha_answer"] = answer

    keyboard = []

    for x in options:
        keyboard.append(
            [
                InlineKeyboardButton(
                    str(x),
                    callback_data=f"captcha:{x}"
                )
            ]
        )

    await update.effective_message.reply_text(
        f"🧩 کپچا\n\n"
        f"{a} + {b} = ؟\n\n"
        f"پاسخ را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================================================
# FORCE JOIN
# =========================================================


async def force_join(update, context):

    user = update.effective_user

    try:

        member = await context.bot.get_chat_member(
            FORCE_GROUP,
            user.id
        )

        if member.status in (
            "member",
            "administrator",
            "creator"
        ):
            return True

    except Exception as e:
        logger.warning("Join check: %s", e)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 عضویت در گپ",
                    url=f"https://t.me/{FORCE_GROUP.lstrip('@')}"
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ بررسی عضویت",
                    callback_data="check_join"
                )
            ]
        ]
    )

    await update.effective_message.reply_text(
        "🔒 ابتدا در گپ اجباری عضو شوید.",
        reply_markup=keyboard
    )

    return False


# =========================================================
# START
# =========================================================


async def start(update, context):

    user = update.effective_user

    referrer = None

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):

            raw = normalize_digits(
                arg[4:]
            )

            if raw.isdigit():
                referrer = int(raw)

    ensure_user(
        user,
        referrer
    )

    if not await force_join(
        update,
        context
    ):
        return

    con = db()
    row = con.execute(
        "SELECT captcha_ok FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()
    con.close()

    if not row or row[0] == 0:

        await captcha(
            update,
            context
        )

        return

    await update.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_menu()
    )


# =========================================================
# CALLBACKS
# =========================================================


async def callbacks(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    # CAPTCHA
    if query.data.startswith("captcha:"):

        selected = int(
            query.data.split(":")[1]
        )

        answer = context.user_data.get(
            "captcha_answer"
        )

        if selected == answer:

            con = db()

            con.execute(
                "UPDATE users SET captcha_ok=1 WHERE user_id=?",
                (user.id,)
            )

            con.commit()
            con.close()

            await query.message.reply_text(
                "✅ کپچا صحیح بود.",
                reply_markup=main_menu()
            )

        else:

            await query.answer(
                "❌ پاسخ اشتباه است.",
                show_alert=True
            )

        return

    # JOIN
    if query.data == "check_join":

        if await force_join(
            update,
            context
        ):

            await query.message.reply_text(
                "✅ عضویت تأیید شد.",
                reply_markup=main_menu()
            )

        return

    # GAME
    if query.data.startswith("game:"):

        parts = query.data.split(":")

        if len(parts) != 3:
            return

        game = parts[1]
        amount = float(parts[2])

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "👥 بازی با دوستان",
                        callback_data=f"friend:{game}:{amount}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🤖 بازی با ربات",
                        callback_data=f"botgame:{game}:{amount}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data="cancel_game"
                    )
                ]
            ]
        )

        await query.message.reply_text(
            f"🎮 {game}\n"
            f"💰 مقدار: {fmt(amount)}\n\n"
            f"نوع بازی را انتخاب کنید:",
            reply_markup=keyboard
        )

        return

    # FRIEND
    if query.data.startswith("friend:"):

        parts = query.data.split(":")

        game = parts[1]
        amount = float(parts[2])

        if get_balance(user.id) < amount:

            await query.message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        con = db()

        cur = con.execute(
            """
            INSERT INTO games
            (chat_id,message_id,creator_id,game_type,amount,mode,status,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                query.message.chat_id,
                query.message.message_id,
                user.id,
                game,
                amount,
                "friend",
                "waiting",
                now(),
            )
        )

        game_id = cur.lastrowid

        con.commit()
        con.close()

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🎮 ورود به بازی",
                        callback_data=f"join:{game_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ لغو بازی",
                        callback_data=f"cancel:{game_id}"
                    )
                ]
            ]
        )

        await query.message.edit_text(
            f"🎮 بازی {game}\n\n"
            f"💰 مقدار: {fmt(amount)}\n"
            f"👤 سازنده: {user.first_name}\n\n"
            f"منتظر بازیکن دوم...",
            reply_markup=keyboard
        )

        return

    # BOT GAME
    if query.data.startswith("botgame:"):

        parts = query.data.split(":")

        game = parts[1]
        amount = float(parts[2])

        if get_balance(user.id) < amount:

            await query.message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        await query.message.edit_text(
            "🤖 ربات در حال بازی است..."
        )

        # نتیجه نمونه برای بازی داخلی
        if game == "تاس":
            p1 = random.randint(1, 6)
            bot = random.randint(1, 6)

        elif game == "بولینگ":
            p1 = random.randint(0, 10)
            bot = random.randint(0, 10)

        elif game == "بسکتبال":
            p1 = random.randint(0, 5)
            bot = random.randint(0, 5)

        elif game == "دارت":
            p1 = random.randint(0, 60)
            bot = random.randint(0, 60)

        else:
            p1 = random.randint(1, 10)
            bot = random.randint(1, 10)

        # مساوی = دوباره
        while p1 == bot:

            if game == "تاس":
                p1 = random.randint(1, 6)
                bot = random.randint(1, 6)

            elif game == "بولینگ":
                p1 = random.randint(0, 10)
                bot = random.randint(0, 10)

            elif game == "بسکتبال":
                p1 = random.randint(0, 5)
                bot = random.randint(0, 5)

            elif game == "دارت":
                p1 = random.randint(0, 60)
                bot = random.randint(0, 60)

        if p1 > bot:

            result = "🏆 شما برنده شدید."

        else:

            result = "🤖 ربات برنده شد."

        await query.message.reply_text(
            f"🎮 {game}\n\n"
            f"👤 شما: {p1}\n"
            f"🤖 ربات: {bot}\n\n"
            f"{result}"
        )

        return

    # JOIN GAME
    if query.data.startswith("join:"):

        game_id = int(
            query.data.split(":")[1]
        )

        con = db()

        game = con.execute(
            "SELECT * FROM games WHERE id=?",
            (game_id,)
        ).fetchone()

        if not game:

            con.close()

            await query.answer(
                "بازی پیدا نشد.",
                show_alert=True
            )

            return

        # columns:
        # id, chat_id, message_id, creator_id,
        # opponent_id, game_type, amount, mode,
        # status, created_at

        if game[8] != "waiting":

            con.close()

            await query.answer(
                "این بازی فعال نیست.",
                show_alert=True
            )

            return

        if game[3] == user.id:

            con.close()

            await query.answer(
                "سازنده نمی‌تواند خودش وارد شود.",
                show_alert=True
            )

            return

        if get_balance(user.id) < game[6]:

            con.close()

            await query.answer(
                "موجودی کافی نیست.",
                show_alert=True
            )

            return

        con.execute(
            """
            UPDATE games
            SET opponent_id=?, status='playing'
            WHERE id=?
            """,
            (
                user.id,
                game_id
            )
        )

        con.commit()
        con.close()

        game_type = game[5]

        await query.message.edit_text(
            "🎮 بازی شروع شد..."
        )

        if game_type == "تاس":

            p1 = random.randint(1, 6)
            p2 = random.randint(1, 6)

        elif game_type == "بولینگ":

            p1 = random.randint(0, 10)
            p2 = random.randint(0, 10)

        elif game_type == "بسکتبال":

            p1 = random.randint(0, 5)
            p2 = random.randint(0, 5)

        elif game_type == "دارت":

            p1 = random.randint(0, 60)
            p2 = random.randint(0, 60)

        else:

            p1 = random.randint(1, 10)
            p2 = random.randint(1, 10)

        while p1 == p2:

            if game_type == "تاس":

                p1 = random.randint(1, 6)
                p2 = random.randint(1, 6)

            elif game_type == "بولینگ":

                p1 = random.randint(0, 10)
                p2 = random.randint(0, 10)

            elif game_type == "بسکتبال":

                p1 = random.randint(0, 5)
                p2 = random.randint(0, 5)

            elif game_type == "دارت":

                p1 = random.randint(0, 60)
                p2 = random.randint(0, 60)

        con = db()

        con.execute(
            "UPDATE games SET status='finished' WHERE id=?",
            (game_id,)
        )

        con.commit()
        con.close()

        if p1 > p2:
            result = "🏆 سازنده برنده شد."
        else:
            result = "🏆 بازیکن دوم برنده شد."

        await query.message.reply_text(
            f"🎮 {game_type}\n\n"
            f"👤 سازنده: {p1}\n"
            f"👥 بازیکن دوم: {p2}\n\n"
            f"{result}"
        )

        return

    # CANCEL
    if query.data == "cancel_game":

        await query.message.edit_text(
            "❌ بازی لغو شد."
        )

        return

    if query.data.startswith("cancel:"):

        game_id = int(
            query.data.split(":")[1]
        )

        con = db()

        game = con.execute(
            "SELECT creator_id,status FROM games WHERE id=?",
            (game_id,)
        ).fetchone()

        if game and game[0] == user.id:

            con.execute(
                "UPDATE games SET status='cancelled' WHERE id=?",
                (game_id,)
            )

            con.commit()

            await query.message.edit_text(
                "❌ بازی لغو شد."
            )

        con.close()

        return


# =========================================================
# TEXT HANDLER
# =========================================================


async def messages(update, context):

    if not update.message:
        return

    user = update.effective_user
    text = update.message.text or ""

    ensure_user(user)

    if not await force_join(
        update,
        context
    ):
        return

    # captcha
    con = db()

    row = con.execute(
        "SELECT captcha_ok FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    con.close()

    if not row or row[0] == 0:

        await captcha(
            update,
            context
        )

        return

    # admin can always use panel
    if not is_enabled() and not is_admin(user.id):

        await update.message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    text = normalize_text(text)

    # =====================================================
    # ADMIN PANEL
    # =====================================================

    if text == "پنل مدیریت":

        if not is_admin(user.id):

            await update.message.reply_text(
                "❌ دسترسی ندارید."
            )

            return

        await update.message.reply_text(
            "🛡 پنل مدیریت",
            reply_markup=admin_menu()
        )

        return

    if text == "🔙 بازگشت":

        await update.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=main_menu()
        )

        return

    # =====================================================
    # BALANCE
    # =====================================================

    if text == "💰 موجودی":

        balance = get_balance(
            user.id
        )

        await update.message.reply_text(
            f"💰 موجودی شما:\n\n"
            f"{fmt(balance)} اعتبار"
        )

        return

    # =====================================================
    # GAME EXAMPLES
    # =====================================================

    if text == "🎮 مثال بازی":

        await update.message.reply_text(
            "🎮 فرمت بازی در گپ:\n\n"
            "🎲 1 تاس 0.1\n"
            "🎲 ۱ تاس ۰.۱\n"
            "🎳 1 بولینگ 0.1\n"
            "🏀 1 بسکتبال 0.1\n"
            "🎯 1 دارت 0.1\n\n"
            "اعداد فارسی و انگلیسی پذیرفته می‌شوند."
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

        count = row[0] if row else 0

        await update.message.reply_text(
            f"👥 زیرمجموعه\n\n"
            f"🔗 لینک شما:\n{link}\n\n"
            f"👤 تعداد: {count}\n"
            f"🎁 پاداش: {fmt(0.05)} اعتبار داخلی"
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

    if context.user_data.get("state") == "support":

        con = db()

        cur = con.execute(
            """
            INSERT INTO support(user_id,message,created_at)
            VALUES(?,?,?)
            """,
            (
                user.id,
                text,
                now()
            )
        )

        ticket_id = cur.lastrowid

        con.commit()
        con.close()

        admin_text = (
            "🆘 پیام پشتیبانی\n\n"
            f"👤 {user.first_name}\n"
            f"🆔 {user.id}\n"
            f"🎫 #{ticket_id}\n\n"
            f"{text}"
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💬 پاسخ به کاربر",
                        callback_data=f"support:{user.id}"
                    )
                ]
            ]
        )

        for admin_id in get_admin_list():

            try:

                await context.bot.send_message(
                    admin_id,
                    admin_text,
                    reply_markup=keyboard
                )

            except Exception as e:

                logger.warning(e)

        context.user_data["state"] = None

        await update.message.reply_text(
            "✅ پیام شما برای پشتیبانی ارسال شد."
        )

        return

    # =====================================================
    # TRANSFER IN GROUP
    # =====================================================

    if (
        text.startswith("انتقال ")
        and update.effective_chat.type
        in ("group", "supergroup")
    ):

        parts = text.split()

        if len(parts) != 2:

            await update.message.reply_text(
                "❌ فرمت صحیح:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱"
            )

            return

        amount = parse_amount(
            parts[1]
        )

        if amount is None:

            await update.message.reply_text(
                "❌ مبلغ معتبر نیست."
            )

            return

        reply = update.message.reply_to_message

        if not reply or not reply.from_user:

            await update.message.reply_text(
                "❌ باید روی پیام گیرنده ریپلای کنید."
            )

            return

        receiver = reply.from_user

        if receiver.id == user.id:

            await update.message.reply_text(
                "❌ نمی‌توانید به خودتان انتقال دهید."
            )

            return

        if get_balance(user.id) < amount:

            await update.message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        ensure_user(receiver)

        if not change_balance(
            user.id,
            -amount
        ):

            await update.message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

        change_balance(
            receiver.id,
            amount
        )

        con = db()

        con.execute(
            """
            INSERT INTO transfers
            (sender,receiver,amount,created_at)
            VALUES(?,?,?,?)
            """,
            (
                user.id,
                receiver.id,
                amount,
                now()
            )
        )

        con.commit()
        con.close()

        await update.message.reply_text(
            f"✅ انتقال انجام شد.\n\n"
            f"👤 گیرنده: {receiver.first_name}\n"
            f"💰 مقدار: {fmt(amount)}"
        )

        return

    # =====================================================
    # GAME COMMAND IN GROUP
    # =====================================================

    if update.effective_chat.type in (
        "group",
        "supergroup"
    ):

        match = re.match(
            r"^(\d+)\s+(تاس|بولینگ|بسکتبال|دارت)\s+(.+)$",
            text
        )

        if match:

            count = int(
                match.group(1)
            )

            game = match.group(2)

            amount = parse_amount(
                match.group(3)
            )

            if count != 1:

                await update.message.reply_text(
                    "❌ فرمت نمونه:\n"
                    "1 تاس 0.1"
                )

                return

            if amount is None:

                await update.message.reply_text(
                    "❌ مقدار بازی معتبر نیست."
                )

                return

            if get_balance(user.id) < amount:

                await update.message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👥 بازی با دوستان",
                            callback_data=f"game:{game}:{amount}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🤖 بازی با ربات",
                            callback_data=f"botgame:{game}:{amount}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ لغو",
                            callback_data="cancel_game"
                        )
                    ]
                ]
            )

            await update.message.reply_text(
                f"🎮 {game}\n\n"
                f"💰 مقدار: {fmt(amount)}\n"
                f"👤 سازنده: {user.first_name}",
                reply_markup=keyboard
            )

            return

    # =====================================================
    # ADMIN
    # =====================================================

    if text == "👥 کاربران":

        if not is_admin(user.id):
            return

        con = db()

        row = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()

        con.close()

        await update.message.reply_text(
            f"👥 تعداد کاربران: {row[0]}"
        )

        return

    if text == "💰 آمار موجودی":

        if not is_admin(user.id):
            return

        con = db()

        row = con.execute(
            "SELECT COALESCE(SUM(balance),0) FROM users"
        ).fetchone()

        con.close()

        await update.message.reply_text(
            f"💰 مجموع اعتبار کاربران:\n"
            f"{fmt(row[0])}"
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

    if text == "👤 اضافه کردن ادمین":

        if user.id != OWNER_ID:
            return

        context.user_data["state"] = "add_admin"

        await update.message.reply_text(
            "🆔 آیدی عددی ادمین جدید را ارسال کنید."
        )

        return

    if text == "❌ حذف ادمین":

        if user.id != OWNER_ID:
            return

        context.user_data["state"] = "remove_admin"

        await update.message.reply_text(
            "🆔 آیدی عددی ادمین را ارسال کنید."
        )

        return

    if context.user_data.get("state") == "add_admin":

        if user.id != OWNER_ID:
            return

        value = normalize_digits(
            text
        )

        if not value.isdigit():

            await update.message.reply_text(
                "❌ آیدی معتبر نیست."
            )

            return

        add_admin(
            int(value)
        )

        context.user_data["state"] = None

        await update.message.reply_text(
            "✅ ادمین اضافه شد."
        )

        return

    if context.user_data.get("state") == "remove_admin":

        if user.id != OWNER_ID:
            return

        value = normalize_digits(
            text
        )

        if not value.isdigit():

            await update.message.reply_text(
                "❌ آیدی معتبر نیست."
            )

            return

        if remove_admin(
            int(value)
        ):

            await update.message.reply_text(
                "✅ ادمین حذف شد."
            )

        else:

            await update.message.reply_text(
                "❌ امکان حذف این ادمین وجود ندارد."
            )

        context.user_data["state"] = None

        return

    # =====================================================
    # DEPOSIT / WITHDRAW - SAFE INTERNAL REQUEST
    # =====================================================

    if text == "➕ واریز":

        context.user_data["state"] = "deposit"

        await update.message.reply_text(
            "➕ مقدار اعتبار درخواستی را وارد کنید.\n\n"
            "این نسخه پرداخت واقعی TRX انجام نمی‌دهد."
        )

        return

    if text == "➖ برداشت":

        await update.message.reply_text(
            "➖ برداشت واقعی TRX در این نسخه فعال نیست."
        )

        return

    if context.user_data.get("state") == "deposit":

        amount = parse_amount(text)

        if amount is None:

            await update.message.reply_text(
                "❌ مقدار معتبر نیست."
            )

            return

        await update.message.reply_text(
            "ℹ️ درخواست پرداخت واقعی در این نسخه انجام نمی‌شود."
        )

        context.user_data["state"] = None

        return


# =========================================================
# ADMIN LIST
# =========================================================


def get_admin_list():

    con = db()

    rows = con.execute(
        "SELECT user_id FROM admins"
    ).fetchall()

    con.close()

    return [
        row[0]
        for row in rows
    ]


# =========================================================
# ERROR HANDLER
# =========================================================


async def error_handler(
    update,
    context
):

    logger.exception(
        "BOT ERROR",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================


def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN در GitHub Secrets تنظیم نشده است."
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
            callbacks
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            messages
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
