import os
import sqlite3
import logging
import asyncio
import secrets
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL_USERNAME = "@zobxt"
CHANNEL_URL = "https://t.me/zobxt"

REFERRAL_REWARD = 0.05
CURRENCY = "TRX"

DB_NAME = "BET_BT.sqlite3"


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DB_NAME,
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
                balance REAL DEFAULT 0,
                referrals INTEGER DEFAULT 0,
                referral_reward REAL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                games INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
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
                amount REAL NOT NULL,
                throws INTEGER DEFAULT 1,
                status TEXT DEFAULT 'waiting',
                creator_results TEXT DEFAULT '',
                opponent_results TEXT DEFAULT '',
                winner_id INTEGER DEFAULT NULL,
                settled INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                type TEXT NOT NULL,
                reference TEXT UNIQUE,
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
            INSERT OR IGNORE INTO settings(key,value)
            VALUES('enabled','1')
        """)

        conn.commit()


# =========================================================
# HELPERS
# =========================================================

def clean_number(value):

    if value is None:
        return ""

    return str(value).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789",
        )
    ).replace(",", ".")


def fmt(value):

    value = float(value)

    if value == int(value):
        return str(int(value))

    return f"{value:.8f}".rstrip("0").rstrip(".")


def is_owner(user_id):

    return int(user_id) == OWNER_ID


def get_setting(key, default=None):

    with closing(get_db()) as conn:

        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()

    if not row:
        return default

    return row["value"]


def set_setting(key, value):

    with closing(get_db()) as conn:

        conn.execute("""
            INSERT INTO settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (
            key,
            str(value),
        ))

        conn.commit()


def bot_enabled():

    return get_setting(
        "enabled",
        "1",
    ) == "1"


# =========================================================
# USERS
# =========================================================

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

    with closing(get_db()) as conn:

        return conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()


def get_balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0.0

    return float(
        row["balance"] or 0
    )


# =========================================================
# BALANCE
# =========================================================

def change_balance(
    user_id,
    amount,
    transaction_type,
    reference=None,
):

    amount = float(amount)

    if amount == 0:
        return False

    if reference is None:
        reference = secrets.token_hex(16)

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            row = conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()

            if not row:

                conn.rollback()
                return False

            balance = float(
                row["balance"] or 0
            )

            new_balance = (
                balance + amount
            )

            if new_balance < -0.00000001:

                conn.rollback()
                return False

            conn.execute(
                """
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """,
                (
                    round(
                        new_balance,
                        8,
                    ),
                    user_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES(?,?,?,?)
                """,
                (
                    user_id,
                    amount,
                    transaction_type,
                    reference,
                ),
            )

            conn.commit()

            return True

        except Exception:

            conn.rollback()

            logger.exception(
                "Balance error"
            )

            return False


def add_balance(
    user_id,
    amount,
):

    return change_balance(
        user_id,
        abs(float(amount)),
        "admin_add",
    )


def remove_balance(
    user_id,
    amount,
):

    return change_balance(
        user_id,
        -abs(float(amount)),
        "admin_remove",
    )


# =========================================================
# REFERRAL
# =========================================================

def process_referral(
    new_user_id,
    referrer_id,
):

    if not referrer_id:
        return False

    if new_user_id == referrer_id:
        return False

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            new_user = conn.execute(
                """
                SELECT referred_by
                FROM users
                WHERE user_id=?
                """,
                (new_user_id,),
            ).fetchone()

            referrer = conn.execute(
                """
                SELECT user_id
                FROM users
                WHERE user_id=?
                """,
                (referrer_id,),
            ).fetchone()

            if not new_user or not referrer:

                conn.rollback()
                return False

            if new_user["referred_by"] is not None:

                conn.rollback()
                return False

            conn.execute(
                """
                UPDATE users
                SET referred_by=?
                WHERE user_id=?
                """,
                (
                    referrer_id,
                    new_user_id,
                ),
            )

            conn.execute(
                """
                UPDATE users
                SET
                    referrals=referrals+1,
                    referral_reward=
                        referral_reward+?,
                    balance=balance+?
                WHERE user_id=?
                """,
                (
                    REFERRAL_REWARD,
                    REFERRAL_REWARD,
                    referrer_id,
                ),
            )

            reference = (
                f"ref_"
                f"{referrer_id}_"
                f"{new_user_id}"
            )

            conn.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES(?,?,?,?)
                """,
                (
                    referrer_id,
                    REFERRAL_REWARD,
                    "referral",
                    reference,
                ),
            )

            conn.commit()

            return True

        except Exception:

            conn.rollback()

            logger.exception(
                "Referral error"
            )

            return False


# =========================================================
# JOIN CHECK
# =========================================================

async def check_membership(
    context,
    user_id,
):

    if is_owner(user_id):
        return True

    try:

        member = await context.bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        logger.warning(
            "Join check failed: %s",
            e,
        )

        return False


def join_markup():

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
                callback_data="check_join",
            )
        ],
    ])


async def require_join(
    update,
    context,
):

    user = update.effective_user

    if not user:
        return False

    if is_owner(user.id):
        return True

    if await check_membership(
        context,
        user.id,
    ):
        return True

    text = (
        "🔒 برای استفاده از ربات "
        "ابتدا عضو کانال شو.\n\n"
        f"📢 {CHANNEL_USERNAME}\n\n"
        "بعد از عضویت روی "
        "«بررسی عضویت» بزن."
    )

    if update.callback_query:

        try:

            await update.callback_query.answer(
                "❌ ابتدا عضو کانال شوید.",
                show_alert=True,
            )

            await update.callback_query.message.reply_text(
                text,
                reply_markup=join_markup(),
            )

        except Exception:
            pass

    elif update.message:

        await update.message.reply_text(
            text,
            reply_markup=join_markup(),
        )

    return False


# =========================================================
# MAIN MENU
# =========================================================

def main_menu(user_id):

    buttons = [
        [
            InlineKeyboardButton(
                "📂 زیرمجموعه",
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
                callback_data="transfer_info",
            ),
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
                callback_data="admin_panel",
            )
        ])

    return InlineKeyboardMarkup(
        buttons
    )


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

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
                callback_data="admin_enable",
            ),
            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_disable",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home",
            )
        ],
    ])


# =========================================================
# START
# =========================================================

async def start(
    update,
    context,
):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if context.args:

        try:

            referrer_id = int(
                clean_number(
                    context.args[0]
                )
            )

        except Exception:

            referrer_id = None

        if referrer_id:

            process_referral(
                user.id,
                referrer_id,
            )

    if is_owner(user.id):

        await update.message.reply_text(
            "👑 BET_BT\n\n"
            "خوش آمدی مالک.",
            reply_markup=main_menu(
                user.id
            ),
        )

        return

    if not await require_join(
        update,
        context,
    ):
        return

    if not bot_enabled():

        await update.message.reply_text(
            "🔴 ربات در حال حاضر خاموش است."
        )

        return

    await update.message.reply_text(
        "🎮 به BET_BT خوش آمدی.",
        reply_markup=main_menu(
            user.id
        ),
    )


# =========================================================
# ADMIN
# =========================================================

async def admin_command(
    update,
    context,
):

    user = update.effective_user

    if not user:
        return

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    await update.message.reply_text(
        "👑 پنل مدیریت BET_BT",
        reply_markup=admin_menu(),
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_button(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not await require_join(
        update,
        context,
    ):
        return

    ensure_user(user)

    await query.answer()

    await query.message.reply_text(
        "💰 موجودی شما:\n\n"
        f"🪙 {fmt(get_balance(user.id))} "
        f"{CURRENCY}"
    )


async def balance_text(
    update,
    context,
):

    user = update.effective_user

    if not user:
        return

    if not is_owner(user.id):

        if not await require_join(
            update,
            context,
        ):
            return

    ensure_user(user)

    await update.message.reply_text(
        "💰 موجودی شما:\n\n"
        f"🪙 {fmt(get_balance(user.id))} "
        f"{CURRENCY}"
    )


# =========================================================
# REFERRALS
# =========================================================

async def referrals_button(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not await require_join(
        update,
        context,
    ):
        return

    ensure_user(user)

    row = get_user(user.id)

    try:

        bot_user = await context.bot.get_me()

        link = (
            f"https://t.me/"
            f"{bot_user.username}"
            f"?start={user.id}"
        )

    except Exception:

        link = "خطا در ساخت لینک."

    await query.answer()

    await query.message.reply_text(
        "📂 زیرمجموعه\n\n"
        f"👥 تعداد: {row['referrals']}\n"
        f"🎁 هر نفر: "
        f"{fmt(REFERRAL_REWARD)} {CURRENCY}\n"
        f"💰 مجموع پاداش: "
        f"{fmt(row['referral_reward'])} {CURRENCY}\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# =========================================================
# EXAMPLES
# =========================================================

async def examples_button(
    update,
    context,
):

    query = update.callback_query

    if not await require_join(
        update,
        context,
    ):
        return

    await query.answer()

    await query.message.reply_text(
        "🎮 مثال بازی\n\n"
        "🎲 تاس:\n"
        "1 تاس 100\n\n"
        "🏀 بسکتبال:\n"
        "1 بسکتبال 100\n\n"
        "🎯 دارت:\n"
        "1 دارت 100\n\n"
        "🎳 بولینگ:\n"
        "1 بولینگ 100\n\n"
        "♾️ تعداد پرتاب محدودیت ندارد."
    )


# =========================================================
# TRANSFER INFO
# =========================================================

async def transfer_button(
    update,
    context,
):

    query = update.callback_query

    if not await require_join(
        update,
        context,
    ):
        return

    await query.answer()

    await query.message.reply_text(
        "🔄 انتقال موجودی\n\n"
        "داخل گپ روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 10\n\n"
        "مثال:\n"
        "انتقال 25"
    )


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    await query.answer()

    await query.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_menu(),
    )


async def admin_add(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    context.user_data.clear()

    context.user_data["admin_action"] = "add"
    context.user_data["admin_step"] = "user_id"

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "آیدی عددی کاربر را بفرست:"
    )


async def admin_remove(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    context.user_data.clear()

    context.user_data["admin_action"] = "remove"
    context.user_data["admin_step"] = "user_id"

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "آیدی عددی کاربر را بفرست:"
    )


async def admin_balance(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    context.user_data.clear()

    context.user_data["admin_action"] = "balance"
    context.user_data["admin_step"] = "user_id"

    await query.answer()

    await query.message.reply_text(
        "💰 موجودی کاربر\n\n"
        "آیدی عددی کاربر را بفرست:"
    )


async def admin_stats(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    with closing(get_db()) as conn:

        users = conn.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        games = conn.execute(
            "SELECT COUNT(*) c FROM games"
        ).fetchone()["c"]

        balance = conn.execute(
            """
            SELECT COALESCE(
                SUM(balance),
                0
            ) b
            FROM users
            """
        ).fetchone()["b"]

    await query.answer()

    await query.message.reply_text(
        "📊 آمار BET_BT\n\n"
        f"👥 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"💰 مجموع موجودی: "
        f"{fmt(balance)} {CURRENCY}\n"
        f"🔌 وضعیت: "
        f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}"
    )


async def admin_enable(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    set_setting(
        "enabled",
        "1",
    )

    await query.answer(
        "🟢 روشن شد."
    )

    await query.message.reply_text(
        "🟢 ربات روشن شد.",
        reply_markup=admin_menu(),
    )


async def admin_disable(
    update,
    context,
):

    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    set_setting(
        "enabled",
        "0",
    )

    await query.answer(
        "🔴 خاموش شد."
    )

    await query.message.reply_text(
        "🔴 ربات خاموش شد.\n\n"
        "مالک همچنان به پنل دسترسی دارد.",
        reply_markup=admin_menu(),
    )


# =========================================================
# ADMIN TEXT FLOW
# =========================================================

async def admin_text_flow(
    update,
    context,
):

    user = update.effective_user
    message = update.message

    if not user or not message:
        return False

    if not is_owner(user.id):
        return False

    action = context.user_data.get(
        "admin_action"
    )

    step = context.user_data.get(
        "admin_step"
    )

    if not action or not step:
        return False

    text = message.text.strip()

    if step == "user_id":

        try:

            target_id = int(
                clean_number(text)
            )

        except Exception:

            await message.reply_text(
                "❌ آیدی نامعتبر است."
            )

            return True

        target = get_user(
            target_id
        )

        if not target:

            await message.reply_text(
                "❌ این کاربر هنوز ربات را استارت نکرده."
            )

            context.user_data.clear()

            return True

        context.user_data["target_id"] = (
            target_id
        )

        if action == "balance":

            await message.reply_text(
                "💰 اطلاعات کاربر\n\n"
                f"👤 {target['name']}\n"
                f"🆔 {target_id}\n"
                f"💰 موجودی: "
                f"{fmt(target['balance'])} "
                f"{CURRENCY}"
            )

            context.user_data.clear()

            return True

        context.user_data["admin_step"] = (
            "amount"
        )

        if action == "add":

            await message.reply_text(
                "➕ مبلغ افزایش را بفرست:"
            )

        else:

            await message.reply_text(
                "➖ مبلغ کسر را بفرست:"
            )

        return True

    if step == "amount":

        try:

            amount = float(
                clean_number(text)
            )

        except Exception:

            await message.reply_text(
                "❌ مبلغ نامعتبر است."
            )

            return True

        if amount <= 0:

            await message.reply_text(
                "❌ مبلغ باید بیشتر از صفر باشد."
            )

            return True

        target_id = context.user_data.get(
            "target_id"
        )

        if action == "add":

            ok = add_balance(
                target_id,
                amount,
            )

            if ok:

                await message.reply_text(
                    "✅ افزایش موجودی انجام شد.\n\n"
                    f"🆔 {target_id}\n"
                    f"➕ {fmt(amount)} {CURRENCY}\n"
                    f"💰 موجودی جدید: "
                    f"{fmt(get_balance(target_id))} "
                    f"{CURRENCY}",
                    reply_markup=admin_menu(),
                )

            else:

                await message.reply_text(
                    "❌ افزایش موجودی انجام نشد."
                )

        else:

            if get_balance(target_id) < amount:

                await message.reply_text(
                    "❌ موجودی کاربر کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{fmt(get_balance(target_id))} "
                    f"{CURRENCY}"
                )

                context.user_data.clear()

                return True

            ok = remove_balance(
                target_id,
                amount,
            )

            if ok:

                await message.reply_text(
                    "✅ کسر موجودی انجام شد.\n\n"
                    f"🆔 {target_id}\n"
                    f"➖ {fmt(amount)} {CURRENCY}\n"
                    f"💰 موجودی جدید: "
                    f"{fmt(get_balance(target_id))} "
                    f"{CURRENCY}",
                    reply_markup=admin_menu(),
                )

            else:

                await message.reply_text(
                    "❌ کسر موجودی انجام نشد."
                )

        context.user_data.clear()

        return True

    return False


# =========================================================
# GAMES
# =========================================================

GAME_CONFIG = {

    "تاس": {
        "emoji": "🎲",
    },

    "بسکتبال": {
        "emoji": "🏀",
    },

    "دارت": {
        "emoji": "🎯",
    },

    "بولینگ": {
        "emoji": "🎳",
    },
}


def parse_game(text):

    parts = text.strip().split()

    if len(parts) != 3:
        return None

    try:

        throws = int(
            clean_number(parts[0])
        )

    except Exception:

        return None

    game_type = parts[1]

    if game_type not in GAME_CONFIG:
        return None

    try:

        amount = float(
            clean_number(parts[2])
        )

    except Exception:

        return None

    if throws <= 0:
        return None

    if amount <= 0:
        return None

    return {
        "throws": throws,
        "game_type": game_type,
        "amount": amount,
        "emoji": GAME_CONFIG[
            game_type
        ]["emoji"],
    }


def game_markup(game_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎮 بازی با دوستان",
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
                "❌ لغو بازی",
                callback_data=f"cancel:{game_id}",
            )
        ],
    ])


def get_game(game_id):

    with closing(get_db()) as conn:

        return conn.execute(
            "SELECT * FROM games WHERE game_id=?",
            (game_id,),
        ).fetchone()


def update_game(
    game_id,
    **fields,
):

    if not fields:
        return

    keys = list(fields.keys())

    query = (
        "UPDATE games SET "
        + ", ".join(
            f"{key}=?"
            for key in keys
        )
        + " WHERE game_id=?"
    )

    values = [
        fields[key]
        for key in keys
    ]

    values.append(game_id)

    with closing(get_db()) as conn:

        conn.execute(
            query,
            values,
        )

        conn.commit()


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(
    update,
    context,
    parsed,
):

    message = update.message
    user = update.effective_user

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        await message.reply_text(
            "❌ بازی‌ها فقط داخل گپ هستند."
        )

        return

    if not is_owner(user.id):

        if not bot_enabled():
            return

        if not await check_membership(
            context,
            user.id,
        ):

            await message.reply_text(
                "🔒 ابتدا عضو کانال شو.",
                reply_markup=join_markup(),
            )

            return

    ensure_user(user)

    amount = float(
        parsed["amount"]
    )

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{fmt(get_balance(user.id))} "
            f"{CURRENCY}\n"
            f"🪙 شرط: "
            f"{fmt(amount)} {CURRENCY}"
        )

        return

    game_id = secrets.token_hex(6)

    reserve_reference = (
        "game_reserve_"
        + game_id
        + "_creator"
    )

    reserved = change_balance(
        user.id,
        -amount,
        "game_reserve",
        reserve_reference,
    )

    if not reserved:

        await message.reply_text(
            "❌ رزرو مبلغ انجام نشد."
        )

        return

    with closing(get_db()) as conn:

        conn.execute(
            """
            INSERT INTO games(
                game_id,
                chat_id,
                creator_id,
                game_type,
                amount,
                throws,
                status
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                game_id,
                message.chat.id,
                user.id,
                parsed["game_type"],
                amount,
                parsed["throws"],
                "waiting",
            ),
        )

        conn.commit()

    await message.reply_text(
        f"{parsed['emoji']} بازی ساخته شد.\n\n"
        f"🎮 نوع: {parsed['game_type']}\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🎯 پرتاب: {parsed['throws']}\n"
        f"🪙 شرط: {fmt(amount)} {CURRENCY}\n\n"
        "یک نفر وارد بازی شود:",
        reply_markup=game_markup(game_id),
    )


# =========================================================
# TELEGRAM DICE ROLL
# =========================================================

async def roll_game(
    context,
    chat_id,
    emoji,
):

    message = await context.bot.send_dice(
        chat_id=chat_id,
        emoji=emoji,
    )

    if not message.dice:
        raise RuntimeError(
            "Dice result unavailable"
        )

    return int(
        message.dice.value
    )


# =========================================================
# RUN GAME
# =========================================================

async def run_game(
    game_id,
    context,
    robot=False,
):

    game = get_game(game_id)

    if not game:
        return

    chat_id = game["chat_id"]
    creator_id = game["creator_id"]
    opponent_id = game["opponent_id"]

    amount = float(
        game["amount"]
    )

    throws = int(
        game["throws"]
    )

    game_type = game["game_type"]

    emoji = GAME_CONFIG[
        game_type
    ]["emoji"]

    creator = get_user(
        creator_id
    )

    if not creator:
        return

    if robot:

        opponent_name = "🤖 ربات"

    else:

        opponent = get_user(
            opponent_id
        )

        if not opponent:
            return

        opponent_name = opponent["name"]

    try:

        # -----------------------------------------
        # CREATOR ROLLS
        # -----------------------------------------

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{emoji} نوبت "
                f"{creator['name']}"
            ),
        )

        creator_results = []

        for _ in range(throws):

            value = await roll_game(
                context,
                chat_id,
                emoji,
            )

            creator_results.append(
                value
            )

            await asyncio.sleep(
                0.6
            )

        # -----------------------------------------
        # OPPONENT ROLLS
        # -----------------------------------------

        await asyncio.sleep(
            0.8
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{emoji} نوبت "
                f"{opponent_name}"
            ),
        )

        opponent_results = []

        for _ in range(throws):

            value = await roll_game(
                context,
                chat_id,
                emoji,
            )

            opponent_results.append(
                value
            )

            await asyncio.sleep(
                0.6
            )

        creator_score = sum(
            creator_results
        )

        opponent_score = sum(
            opponent_results
        )

        # -----------------------------------------
        # SETTLEMENT
        # -----------------------------------------

        with closing(get_db()) as conn:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            row = conn.execute(
                """
                SELECT settled
                FROM games
                WHERE game_id=?
                """,
                (game_id,),
            ).fetchone()

            if not row or row["settled"]:

                conn.rollback()
                return

            if creator_score == opponent_score:

                conn.execute(
                    """
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """,
                    (
                        amount,
                        creator_id,
                    ),
                )

                if not robot:

                    conn.execute(
                        """
                        UPDATE users
                        SET balance=balance+?
                        WHERE user_id=?
                        """,
                        (
                            amount,
                            opponent_id,
                        ),
                    )

                conn.execute(
                    """
                    UPDATE users
                    SET
                        games=games+1,
                        draws=draws+1
                    WHERE user_id=?
                    """,
                    (creator_id,),
                )

                if not robot:

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            games=games+1,
                            draws=draws+1
                        WHERE user_id=?
                        """,
                        (opponent_id,),
                    )

                winner_id = None

                result_text = (
                    "🤝 مساوی شد.\n"
                    "مبلغ شرط برگشت داده شد."
                )

            elif creator_score > opponent_score:

                prize = amount * 2

                conn.execute(
                    """
                    UPDATE users
                    SET
                        balance=balance+?,
                        games=games+1,
                        wins=wins+1
                    WHERE user_id=?
                    """,
                    (
                        prize,
                        creator_id,
                    ),
                )

                if not robot:

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            games=games+1,
                            losses=losses+1
                        WHERE user_id=?
                        """,
                        (opponent_id,),
                    )

                winner_id = creator_id

                result_text = (
                    f"🏆 برنده: {creator['name']}\n"
                    f"💰 جایزه: "
                    f"{fmt(prize)} {CURRENCY}"
                )

            else:

                winner_id = 0 if robot else opponent_id

                if robot:

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            games=games+1,
                            losses=losses+1
                        WHERE user_id=?
                        """,
                        (creator_id,),
                    )

                    result_text = (
                        "🤖 ربات برنده شد."
                    )

                else:

                    prize = amount * 2

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            balance=balance+?,
                            games=games+1,
                            wins=wins+1
                        WHERE user_id=?
                        """,
                        (
                            prize,
                            opponent_id,
                        ),
                    )

                    conn.execute(
                        """
                        UPDATE users
                        SET
                            games=games+1,
                            losses=losses+1
                        WHERE user_id=?
                        """,
                        (creator_id,),
                    )

                    result_text = (
                        f"🏆 برنده: "
                        f"{opponent_name}\n"
                        f"💰 جایزه: "
                        f"{fmt(prize)} {CURRENCY}"
                    )

            creator_results_text = ",".join(
                str(x)
                for x in creator_results
            )

            opponent_results_text = ",".join(
                str(x)
                for x in opponent_results
            )

            conn.execute(
                """
                UPDATE games
                SET
                    creator_results=?,
                    opponent_results=?,
                    winner_id=?,
                    status='finished',
                    settled=1
                WHERE game_id=?
                """,
                (
                    creator_results_text,
                    opponent_results_text,
                    winner_id,
                    game_id,
                ),
            )

            conn.commit()

        creator_rolls = " + ".join(
            str(x)
            for x in creator_results
        )

        opponent_rolls = " + ".join(
            str(x)
            for x in opponent_results
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{emoji} نتیجه بازی\n\n"
                f"👤 {creator['name']}\n"
                f"🎯 {creator_rolls}\n"
                f"📊 مجموع: {creator_score}\n\n"
                f"👤 {opponent_name}\n"
                f"🎯 {opponent_rolls}\n"
                f"📊 مجموع: {opponent_score}\n\n"
                f"{result_text}\n\n"
                f"🪙 شرط: "
                f"{fmt(amount)} {CURRENCY}"
            ),
        )

    except Exception:

        logger.exception(
            "Game error"
        )

        # -----------------------------------------
        # REFUND
        # -----------------------------------------

        with closing(get_db()) as conn:

            try:

                conn.execute(
                    "BEGIN IMMEDIATE"
                )

                row = conn.execute(
                    """
                    SELECT settled
                    FROM games
                    WHERE game_id=?
                    """,
                    (game_id,),
                ).fetchone()

                if row and not row["settled"]:

                    conn.execute(
                        """
                        UPDATE users
                        SET balance=balance+?
                        WHERE user_id=?
                        """,
                        (
                            amount,
                            creator_id,
                        ),
                    )

                    if not robot:

                        conn.execute(
                            """
                            UPDATE users
                            SET balance=balance+?
                            WHERE user_id=?
                            """,
                            (
                                amount,
                                opponent_id,
                            ),
                        )

                    conn.execute(
                        """
                        UPDATE games
                        SET
                            status='refunded',
                            settled=1
                        WHERE game_id=?
                        """,
                        (game_id,),
                    )

                    conn.commit()

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ بازی به دلیل خطا متوقف شد.\n\n"
                            "🪙 مبلغ شرط برگشت داده شد."
                        ),
                    )

                else:

                    conn.rollback()

            except Exception:

                conn.rollback()

                logger.exception(
                    "Refund error"
                )


# =========================================================
# JOIN GAME
# =========================================================

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

    game = get_game(game_id)

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی شروع شده یا تمام شده.",
            show_alert=True,
        )

        return

    if game["creator_id"] == user.id:

        await query.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True,
        )

        return

    if not is_owner(user.id):

        if not await check_membership(
            context,
            user.id,
        ):

            await query.answer(
                "❌ ابتدا عضو کانال شو.",
                show_alert=True,
            )

            return

    ensure_user(user)

    amount = float(
        game["amount"]
    )

    if get_balance(user.id) < amount:

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    reference = (
        "game_join_"
        + game_id
        + "_"
        + str(user.id)
    )

    ok = change_balance(
        user.id,
        -amount,
        "game_reserve",
        reference,
    )

    if not ok:

        await query.answer(
            "❌ برداشت مبلغ انجام نشد.",
            show_alert=True,
        )

        return

    update_game(
        game_id,
        opponent_id=user.id,
        status="playing",
    )

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.edit_message_text(
        "🎮 نفر دوم وارد شد.\n\n"
        "⏳ بازی در حال شروع..."
    )

    await asyncio.sleep(
        1
    )

    await run_game(
        game_id,
        context,
        robot=False,
    )


# =========================================================
# ROBOT GAME
# =========================================================

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

    game = get_game(game_id)

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده بازی می‌تواند.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True,
        )

        return

    update_game(
        game_id,
        opponent_id=0,
        status="playing",
    )

    await query.answer()

    await query.edit_message_text(
        "🤖 بازی با ربات انتخاب شد.\n\n"
        "⏳ بازی شروع می‌شود..."
    )

    await asyncio.sleep(
        1
    )

    await run_game(
        game_id,
        context,
        robot=True,
    )


# =========================================================
# CANCEL GAME
# =========================================================

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

    game = get_game(game_id)

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده می‌تواند لغو کند.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی قابل لغو نیست.",
            show_alert=True,
        )

        return

    amount = float(
        game["amount"]
    )

    update_game(
        game_id,
        status="cancelled",
    )

    reference = (
        "game_cancel_"
        + game_id
    )

    change_balance(
        user.id,
        amount,
        "game_refund",
        reference,
    )

    await query.answer(
        "❌ بازی لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        f"🪙 {fmt(amount)} {CURRENCY} "
        "برگشت داده شد."
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_handler(
    update,
    context,
):

    message = update.message
    sender = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 10"
        )

        return

    target = (
        message.reply_to_message.from_user
    )

    if not target:
        return

    if target.is_bot:

        await message.reply_text(
            "❌ به ربات نمی‌توان انتقال داد."
        )

        return

    if target.id == sender.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if not is_owner(sender.id):

        if not bot_enabled():
            return

        if not await check_membership(
            context,
            sender.id,
        ):
            return

    parts = message.text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 10"
        )

        return

    try:

        amount = float(
            clean_number(parts[1])
        )

    except Exception:

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
    ensure_user(target)

    reference = (
        "transfer_"
        + str(sender.id)
        + "_"
        + str(target.id)
        + "_"
        + secrets.token_hex(8)
    )

    with closing(get_db()) as conn:

        try:

            conn.execute(
                "BEGIN IMMEDIATE"
            )

            sender_row = conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (sender.id,),
            ).fetchone()

            if not sender_row:

                conn.rollback()

                await message.reply_text(
                    "❌ فرستنده پیدا نشد."
                )

                return

            sender_balance = float(
                sender_row["balance"]
            )

            if sender_balance < amount:

                conn.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{fmt(sender_balance)} "
                    f"{CURRENCY}"
                )

                return

            conn.execute(
                """
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
                """,
                (
                    amount,
                    sender.id,
                ),
            )

            conn.execute(
                """
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """,
                (
                    amount,
                    target.id,
                ),
            )

            conn.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES(?,?,?,?)
                """,
                (
                    sender.id,
                    -amount,
                    "transfer_send",
                    reference + "_send",
                ),
            )

            conn.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    amount,
                    type,
                    reference
                )
                VALUES(?,?,?,?)
                """,
                (
                    target.id,
                    amount,
                    "transfer_receive",
                    reference + "_receive",
                ),
            )

            conn.commit()

        except Exception:

            conn.rollback()

            logger.exception(
                "Transfer error"
            )

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {target.full_name}\n"
        f"🪙 مبلغ: {fmt(amount)} {CURRENCY}\n"
        f"💰 موجودی شما: "
        f"{fmt(get_balance(sender.id))} "
        f"{CURRENCY}"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update,
    context,
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    text = message.text.strip()

    # -----------------------------------------
    # ADMIN FLOW
    # -----------------------------------------

    if is_owner(user.id):

        handled = await admin_text_flow(
            update,
            context,
        )

        if handled:
            return

    # -----------------------------------------
    # BALANCE IN GROUP
    # -----------------------------------------

    if text.lower() in (
        "موجودی",
        "موجودی من",
        "بالانس",
        "balance",
    ):

        await balance_text(
            update,
            context,
        )

        return

    # -----------------------------------------
    # TRANSFER
    # -----------------------------------------

    if text.startswith(
        "انتقال"
    ):

        if message.chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):

            await transfer_handler(
                update,
                context,
            )

        return

    # -----------------------------------------
    # GAMES ONLY IN GROUP
    # -----------------------------------------

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    parsed = parse_game(text)

    if not parsed:
        return

    await create_game(
        update,
        context,
        parsed,
    )


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update,
    context,
):

    query = update.callback_query
    data = query.data or ""

    # -----------------------------------------
    # JOIN CHECK
    # -----------------------------------------

    if data == "check_join":

        user = update.effective_user

        if is_owner(user.id):

            await query.answer(
                "✅ تأیید شد."
            )

            await query.message.reply_text(
                "🎮 BET_BT",
                reply_markup=main_menu(
                    user.id
                ),
            )

            return

        if await check_membership(
            context,
            user.id,
        ):

            await query.answer(
                "✅ عضویت تأیید شد."
            )

            await query.message.reply_text(
                "✅ عضویت تأیید شد.",
                reply_markup=main_menu(
                    user.id
                ),
            )

        else:

            await query.answer(
                "❌ هنوز عضو کانال نیستی.",
                show_alert=True,
            )

        return

    # -----------------------------------------
    # HOME
    # -----------------------------------------

    if data == "home":

        await query.answer()

        await query.message.reply_text(
            "🎮 BET_BT",
            reply_markup=main_menu(
                update.effective_user.id
            ),
        )

        return

    # -----------------------------------------
    # USER BUTTONS
    # -----------------------------------------

    if data == "balance":

        await balance_button(
            update,
            context,
        )

        return

    if data == "referrals":

        await referrals_button(
            update,
            context,
        )

        return

    if data == "examples":

        await examples_button(
            update,
            context,
        )

        return

    if data == "transfer_info":

        await transfer_button(
            update,
            context,
        )

        return

    # -----------------------------------------
    # ADMIN
    # -----------------------------------------

    if data == "admin_panel":

        await admin_panel(
            update,
            context,
        )

        return

    if data == "admin_add":

        await admin_add(
            update,
            context,
        )

        return

    if data == "admin_remove":

        await admin_remove(
            update,
            context,
        )

        return

    if data == "admin_balance":

        await admin_balance(
            update,
            context,
        )

        return

    if data == "admin_stats":

        await admin_stats(
            update,
            context,
        )

        return

    if data == "admin_enable":

        await admin_enable(
            update,
            context,
        )

        return

    if data == "admin_disable":

        await admin_disable(
            update,
            context,
        )

        return

    # -----------------------------------------
    # GAME
    # -----------------------------------------

    if data.startswith("join:"):

        await join_game(
            update,
            context,
        )

        return

    if data.startswith("robot:"):

        await robot_game(
            update,
            context,
        )

        return

    if data.startswith("cancel:"):

        await cancel_game(
            update,
            context,
        )

        return

    await query.answer()


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context,
):

    logger.exception(
        "Unhandled error: %s",
        context.error,
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

    application = (
        Application
        .builder()
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
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
