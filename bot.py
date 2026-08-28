import os
import json
import uuid
import asyncio
import logging
from pathlib import Path

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
# BET_BT
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

# آیدی مالک قبلی پروژه
OWNER_ID = 8552447077

# اگر چند ادمین خواستی:
ADMIN_IDS = {OWNER_ID}

DB_FILE = Path("bet_bt_data.json")

# واحد مجازی
UNIT = "VTRX"

# پاداش هر رفرال
REF_REWARD = 0.05

# جوین اجباری
REQUIRED_CHAT = "@zobxt"
REQUIRED_URL = "https://t.me/zobxt"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def default_db():
    return {
        "users": {},
        "games": {},
        "settings": {
            "enabled": True
        }
    }


def load_db():
    if not DB_FILE.exists():
        return default_db()

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default_db()

        data.setdefault("users", {})
        data.setdefault("games", {})
        data.setdefault("settings", {})
        data["settings"].setdefault("enabled", True)

        return data

    except Exception as e:
        logger.error("DB ERROR: %s", e)
        return default_db()


db = load_db()


def save_db():
    temp = DB_FILE.with_suffix(".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(
            db,
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp.replace(DB_FILE)


# ============================================================
# HELPERS
# ============================================================

def fmt(value):
    value = float(value)

    if value.is_integer():
        return str(int(value))

    return (
        f"{value:.8f}"
        .rstrip("0")
        .rstrip(".")
    )


def is_admin(user_id):
    return int(user_id) in ADMIN_IDS


def ensure_user(user):
    uid = str(user.id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "id": user.id,
            "name": user.full_name or "کاربر",
            "username": user.username or "",
            "balance": 0.0,
            "games": 0,
            "wins": 0,
            "losses": 0,
            "referrer": None,
            "referrals": [],
        }

    else:
        db["users"][uid]["name"] = (
            user.full_name
            or db["users"][uid].get("name", "کاربر")
        )

        db["users"][uid]["username"] = (
            user.username
            or db["users"][uid].get("username", "")
        )

    return db["users"][uid]


def get_user(user_id):
    return db["users"].get(str(user_id))


def balance(user_id):
    user = get_user(user_id)

    if not user:
        return 0.0

    return float(
        user.get("balance", 0.0)
    )


def add_balance(user_id, amount):
    user = get_user(user_id)

    if not user:
        return False

    user["balance"] = round(
        balance(user_id) + float(amount),
        8,
    )

    return True


def remove_balance(user_id, amount):
    user = get_user(user_id)

    if not user:
        return False

    if balance(user_id) < float(amount):
        return False

    user["balance"] = round(
        balance(user_id) - float(amount),
        8,
    )

    return True


# ============================================================
# BOT ENABLE/DISABLE
# ============================================================

def bot_enabled():
    return bool(
        db["settings"].get(
            "enabled",
            True,
        )
    )


# ============================================================
# JOIN CHECK
# ============================================================

async def check_membership(bot, user_id):
    """
    اگر ربات دسترسی بررسی عضویت داشته باشد،
    وضعیت واقعی را بررسی می‌کند.

    اگر تلگرام اجازه بررسی ندهد، False برمی‌گردد.
    """

    try:
        member = await bot.get_chat_member(
            REQUIRED_CHAT,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:
        logger.warning(
            "JOIN CHECK ERROR: %s",
            e,
        )

        return False


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 عضویت",
                    url=REQUIRED_URL,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ بررسی عضویت",
                    callback_data="check_join",
                )
            ],
        ]
    )


async def require_join(update, context):
    user = update.effective_user

    if not user:
        return False

    if is_admin(user.id):
        return True

    ok = await check_membership(
        context.bot,
        user.id,
    )

    if ok:
        return True

    text = (
        "🔒 برای استفاده از BET_BT ابتدا عضو شوید.\n\n"
        "بعد از عضویت روی دکمه بررسی عضویت بزن."
    )

    if update.callback_query:
        try:
            await update.callback_query.answer(
                "❌ هنوز عضویت تأیید نشده.",
                show_alert=True,
            )

            await update.callback_query.message.reply_text(
                text,
                reply_markup=join_keyboard(),
            )
        except Exception:
            pass

    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=join_keyboard(),
        )

    return False


# ============================================================
# MAIN MENU
# ============================================================

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
                callback_data="transfer",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎮 مثال بازی",
                callback_data="examples",
            )
        ],
    ]

    if is_admin(user_id):
        buttons.append(
            [
                InlineKeyboardButton(
                    "⚙️ پنل مدیریت",
                    callback_data="admin",
                )
            ]
        )

    return InlineKeyboardMarkup(buttons)


# ============================================================
# ADMIN MENU
# ============================================================

def admin_menu():
    return InlineKeyboardMarkup(
        [
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
                InlineKeyboardButton(
                    "📊 آمار",
                    callback_data="admin_stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔴 خاموش کردن ربات",
                    callback_data="admin_disable",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 برگشت",
                    callback_data="home",
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # referral
    if context.args:
        code = context.args[0]

        try:
            referrer_id = int(code)
        except Exception:
            referrer_id = None

        if (
            referrer_id
            and referrer_id != user.id
            and get_user(referrer_id)
            and not get_user(user.id).get("referrer")
        ):
            db["users"][str(user.id)]["referrer"] = (
                referrer_id
            )

            db["users"][
                str(referrer_id)
            ].setdefault(
                "referrals",
                [],
            ).append(
                user.id
            )

            add_balance(
                referrer_id,
                REF_REWARD,
            )

            save_db()

    # مدیر همیشه بتواند وارد پنل شود
    if is_admin(user.id):
        await update.message.reply_text(
            "👑 پنل BET_BT\n\n"
            "ربات آماده است.",
            reply_markup=main_menu(user.id),
        )
        return

    # جوین اجباری
    if not await require_join(
        update,
        context,
    ):
        return

    if not bot_enabled():
        await update.message.reply_text(
            "🔴 ربات موقتاً خاموش است."
        )
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی.",
        reply_markup=main_menu(user.id),
    )


# ============================================================
# HOME
# ============================================================

async def home(update, context):
    query = update.callback_query
    user = update.effective_user

    await query.answer()

    if not await require_join(
        update,
        context,
    ):
        return

    await query.message.reply_text(
        "🎮 BET_BT",
        reply_markup=main_menu(user.id),
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):
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
        "💰 موجودی شما\n\n"
        f"🪙 {fmt(balance(user.id))} {UNIT}"
    )


# ============================================================
# REFERRALS
# ============================================================

async def show_referrals(update, context):
    query = update.callback_query
    user = update.effective_user

    if not await require_join(
        update,
        context,
    ):
        return

    data = ensure_user(user)

    referrals = data.get(
        "referrals",
        [],
    )

    try:
        me = await context.bot.get_me()
        username = me.username
    except Exception:
        username = None

    if username:
        link = (
            f"https://t.me/{username}"
            f"?start={user.id}"
        )
    else:
        link = "لینک بعد از تنظیم username ربات نمایش داده می‌شود."

    await query.answer()

    await query.message.reply_text(
        "📂 زیرمجموعه\n\n"
        f"👥 تعداد: {len(referrals)}\n"
        f"🎁 هر رِف: {fmt(REF_REWARD)} {UNIT}\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# ============================================================
# EXAMPLES
# ============================================================

async def examples(update, context):
    query = update.callback_query

    if not await require_join(
        update,
        context,
    ):
        return

    await query.answer()

    await query.message.reply_text(
        "🎮 مثال بازی\n\n"
        "🎲 ۱ تاس ۰.۵\n"
        "🏀 ۱ بسکتبال ۰.۵\n"
        "🎯 ۱ دارت ۰.۵\n"
        "🎳 ۱ بولینگ ۰.۵\n\n"
        "بعد از ساخت:\n"
        "🎮 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "❌ لغو"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        "⚙️ پنل مدیریت\n\n"
        "یکی از گزینه‌ها را انتخاب کن:",
        reply_markup=admin_menu(),
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    total_users = len(db["users"])

    total_balance = sum(
        float(x.get("balance", 0))
        for x in db["users"].values()
    )

    total_games = len(db["games"])

    finished = sum(
        1
        for x in db["games"].values()
        if x.get("status") == "finished"
    )

    await query.answer()

    await query.message.reply_text(
        "📊 آمار ربات\n\n"
        f"👥 کاربران: {total_users}\n"
        f"🎮 کل بازی‌ها: {total_games}\n"
        f"✅ بازی‌های تمام‌شده: {finished}\n"
        f"💰 مجموع موجودی: {fmt(total_balance)} {UNIT}\n"
        f"🔌 وضعیت: "
        f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}"
    )


# ============================================================
# ADMIN ADD / REMOVE HELP
# ============================================================

async def admin_add_help(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "/addbalance USER_ID AMOUNT\n\n"
        "مثال:\n"
        "/addbalance 123456789 10"
    )


async def admin_remove_help(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "فرمت:\n"
        "/removebalance USER_ID AMOUNT\n\n"
        "مثال:\n"
        "/removebalance 123456789 10"
    )


async def admin_balance_help(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        "💰 موجودی کاربر\n\n"
        "فرمت:\n"
        "/userbalance USER_ID"
    )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def addbalance(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "فرمت:\n"
            "/addbalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = float(
            context.args[1].replace(
                ",",
                ".",
            )
        )
    except Exception:
        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )
        return

    target = get_user(target_id)

    if not target:
        await update.message.reply_text(
            "❌ کاربر در دیتابیس پیدا نشد."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    add_balance(
        target_id,
        amount,
    )

    save_db()

    await update.message.reply_text(
        "✅ موجودی افزایش یافت.\n\n"
        f"👤 {target.get('name', target_id)}\n"
        f"➕ {fmt(amount)} {UNIT}\n"
        f"💰 موجودی جدید: "
        f"{fmt(balance(target_id))} {UNIT}"
    )


async def removebalance(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "فرمت:\n"
            "/removebalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(context.args[0])
        amount = float(
            context.args[1].replace(
                ",",
                ".",
            )
        )
    except Exception:
        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )
        return

    target = get_user(target_id)

    if not target:
        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    if balance(target_id) < amount:
        await update.message.reply_text(
            "❌ موجودی کاربر کمتر از این مقدار است."
        )
        return

    remove_balance(
        target_id,
        amount,
    )

    save_db()

    await update.message.reply_text(
        "✅ موجودی کسر شد.\n\n"
        f"👤 {target.get('name', target_id)}\n"
        f"➖ {fmt(amount)} {UNIT}\n"
        f"💰 موجودی جدید: "
        f"{fmt(balance(target_id))} {UNIT}"
    )


async def userbalance(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "فرمت:\n"
            "/userbalance USER_ID"
        )
        return

    try:
        target_id = int(context.args[0])
    except Exception:
        await update.message.reply_text(
            "❌ آیدی نامعتبر."
        )
        return

    target = get_user(target_id)

    if not target:
        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )
        return

    await update.message.reply_text(
        "💰 اطلاعات کاربر\n\n"
        f"👤 {target.get('name', '-')}\n"
        f"🆔 {target_id}\n"
        f"💰 {fmt(balance(target_id))} {UNIT}\n"
        f"🎮 بازی: {target.get('games', 0)}\n"
        f"🏆 برد: {target.get('wins', 0)}\n"
        f"❌ باخت: {target.get('losses', 0)}"
    )


# ============================================================
# ADMIN ENABLE / DISABLE
# ============================================================

async def admin_disable(update, context):
    query = update.callback_query
    user = update.effective_user

    if not is_admin(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    db["settings"]["enabled"] = False

    save_db()

    await query.answer(
        "ربات خاموش شد."
    )

    await query.message.reply_text(
        "🔴 ربات خاموش شد.\n\n"
        "ادمین همچنان می‌تواند وارد پنل شود.",
        reply_markup=admin_menu(),
    )


async def admin_enable_command(update, context):
    user = update.effective_user

    if not is_admin(user.id):
        return

    db["settings"]["enabled"] = True

    save_db()

    await update.message.reply_text(
        "🟢 ربات روشن شد."
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(update, context):
    query = update.callback_query
    data = query.data or ""

    if data == "check_join":

        user = update.effective_user

        if is_admin(user.id):
            await query.answer(
                "✅ ادمین تأیید شد."
            )

            await query.message.reply_text(
                "🎮 BET_BT",
                reply_markup=main_menu(user.id),
            )

            return

        ok = await check_membership(
            context.bot,
            user.id,
        )

        if ok:
            await query.answer(
                "✅ عضویت تأیید شد."
            )

            await query.message.reply_text(
                "✅ عضویت تأیید شد.",
                reply_markup=main_menu(user.id),
            )

        else:
            await query.answer(
                "❌ هنوز عضو نشده‌ای.",
                show_alert=True,
            )

        return

    if data == "home":
        await home(update, context)
        return

    if data == "balance":
        await show_balance(update, context)
        return

    if data == "referrals":
        await show_referrals(update, context)
        return

    if data == "examples":
        await examples(update, context)
        return

    if data == "transfer":

        await query.answer()

        await query.message.reply_text(
            "🔄 انتقال در گپ\n\n"
            "روی پیام کاربر Reply کن و بنویس:\n\n"
            "انتقال 0.5"
        )

        return

    # ---------------- ADMIN ----------------

    if data == "admin":
        await admin_panel(update, context)
        return

    if data == "admin_add":
        await admin_add_help(update, context)
        return

    if data == "admin_remove":
        await admin_remove_help(update, context)
        return

    if data == "admin_balance":
        await admin_balance_help(update, context)
        return

    if data == "admin_stats":
        await admin_stats(update, context)
        return

    if data == "admin_disable":
        await admin_disable(update, context)
        return

    await query.answer()


# ============================================================
# TRANSFER IN GROUP
# ============================================================

async def group_transfer(update, context):
    message = update.message
    sender = update.effective_user

    if not message.reply_to_message:
        await message.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )
        return

    target = message.reply_to_message.from_user

    if not target:
        return

    if target.is_bot:
        await message.reply_text(
            "❌ انتقال به ربات ممکن نیست."
        )
        return

    if target.id == sender.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return

    parts = message.text.split()

    if len(parts) != 2:
        await message.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 0.5"
        )
        return

    try:
        amount = float(
            parts[1].replace(
                ",",
                ".",
            )
        )
    except Exception:
        await message.reply_text(
            "❌ مبلغ نامعتبر."
        )
        return

    if amount <= 0:
        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    if not is_admin(sender.id):

        if not await check_membership(
            context.bot,
            sender.id,
        ):
            return

    ensure_user(sender)
    ensure_user(target)

    if balance(sender.id) < amount:
        await message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return

    remove_balance(
        sender.id,
        amount,
    )

    add_balance(
        target.id,
        amount,
    )

    save_db()

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {target.full_name}\n"
        f"🪙 مبلغ: {fmt(amount)} {UNIT}\n"
        f"💰 موجودی فرستنده: "
        f"{fmt(balance(sender.id))} {UNIT}"
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_MAP = {
    "تاس": "dice",
    "بسکتبال": "basketball",
    "دارت": "darts",
    "بولینگ": "bowling",
}

GAME_TITLE = {
    "dice": "🎲 تاس",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت",
    "bowling": "🎳 بولینگ",
}


def parse_game(text):
    if not text:
        return None

    parts = text.strip().split()

    if len(parts) != 3:
        return None

    if parts[0] not in (
        "۱",
        "1",
    ):
        return None

    game = GAME_MAP.get(
        parts[1].lower()
    )

    if not game:
        return None

    try:
        amount = float(
            parts[2].replace(
                ",",
                ".",
            )
        )
    except Exception:
        return None

    if amount <= 0:
        return None

    return game, amount


# ============================================================
# GAME CREATION
# ============================================================

def game_buttons(game_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎮 بازی با دوستان",
                    callback_data=f"friend:{game_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🤖 بازی با ربات",
                    callback_data=f"bot:{game_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"cancel:{game_id}",
                )
            ],
        ]
    )


async def create_game(update, context):
    message = update.message
    user = update.effective_user

    parsed = parse_game(
        message.text
    )

    if not parsed:
        return False

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return False

    if not bot_enabled() and not is_admin(user.id):
        return True

    if not is_admin(user.id):

        if not await check_membership(
            context.bot,
            user.id,
        ):
            await message.reply_text(
                "🔒 ابتدا باید عضو @zobxt شوی."
            )
            return True

    game_type, amount = parsed

    ensure_user(user)

    if balance(user.id) < amount:
        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {fmt(balance(user.id))} {UNIT}\n"
            f"🪙 شرط: {fmt(amount)} {UNIT}"
        )
        return True

    # مبلغ سازنده رزرو می‌شود
    remove_balance(
        user.id,
        amount,
    )

    game_id = uuid.uuid4().hex[:12]

    db["games"][game_id] = {
        "id": game_id,
        "chat_id": message.chat.id,
        "creator_id": user.id,
        "opponent_id": None,
        "game_type": game_type,
        "amount": amount,
        "status": "waiting",
        "creator_value": None,
        "opponent_value": None,
    }

    save_db()

    await message.reply_text(
        f"{GAME_TITLE[game_type]} بازی ساخته شد.\n\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🪙 مبلغ: {fmt(amount)} {UNIT}\n\n"
        "انتخاب کن:",
        reply_markup=game_buttons(game_id),
    )

    return True


# ============================================================
# TELEGRAM GAME THROW
# ============================================================

async def throw_game(bot, chat_id, game_type):
    emoji = {
        "dice": "🎲",
        "basketball": "🏀",
        "darts": "🎯",
        "bowling": "🎳",
    }[game_type]

    result = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji,
    )

    return int(
        result.dice.value
    )


# ============================================================
# FRIEND JOIN
# ============================================================

async def friend_game(update, context):
    query = update.callback_query
    user = update.effective_user

    if not await check_membership(
        context.bot,
        user.id,
    ) and not is_admin(user.id):
        await query.answer(
            "ابتدا عضو @zobxt شو.",
            show_alert=True,
        )
        return

    game_id = query.data.split(
        ":",
        1,
    )[1]

    game = db["games"].get(
        game_id
    )

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
            "❌ خودت سازنده این بازی هستی.",
            show_alert=True,
        )
        return

    ensure_user(user)

    amount = float(
        game["amount"]
    )

    if balance(user.id) < amount:
        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )
        return

    # نفر دوم
    remove_balance(
        user.id,
        amount,
    )

    game["opponent_id"] = user.id
    game["status"] = "playing"

    save_db()

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.edit_message_text(
        f"{GAME_TITLE[game['game_type']]}\n\n"
        "🎮 نفر دوم وارد شد.\n"
        "⏳ بازی شروع می‌شود..."
    )

    await asyncio.sleep(1)

    await play_game(
        game,
        context,
        bot_mode=False,
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(update, context):
    query = update.callback_query
    user = update.effective_user

    game_id = query.data.split(
        ":",
        1,
    )[1]

    game = db["games"].get(
        game_id
    )

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )
        return

    if game["creator_id"] != user.id:
        await query.answer(
            "❌ فقط سازنده می‌تواند با ربات بازی کند.",
            show_alert=True,
        )
        return

    if game["status"] != "waiting":
        await query.answer(
            "❌ بازی دیگر قابل اجرا نیست.",
            show_alert=True,
        )
        return

    game["opponent_id"] = 0
    game["status"] = "playing"

    save_db()

    await query.answer()

    await query.edit_message_text(
        f"{GAME_TITLE[game['game_type']]}\n\n"
        "🤖 بازی با ربات انتخاب شد.\n"
        "⏳ شروع..."
    )

    await asyncio.sleep(1)

    await play_game(
        game,
        context,
        bot_mode=True,
    )


# ============================================================
# PLAY GAME
# سازنده همیشه اول
# ============================================================

async def play_game(
    game,
    context,
    bot_mode=False,
):
    chat_id = game["chat_id"]
    game_type = game["game_type"]

    creator_id = game["creator_id"]
    opponent_id = game["opponent_id"]

    amount = float(
        game["amount"]
    )

    creator = get_user(
        creator_id
    )

    if not creator:
        return

    try:

        # سازنده اول
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{GAME_TITLE[game_type]}\n\n"
                f"👤 {creator['name']} اول می‌اندازد..."
            ),
        )

        creator_value = await throw_game(
            context.bot,
            chat_id,
            game_type,
        )

        game["creator_value"] = creator_value

        await asyncio.sleep(1)

        # نفر دوم
        if bot_mode:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{GAME_TITLE[game_type]}\n\n"
                    "🤖 ربات می‌اندازد..."
                ),
            )

            opponent_value = await throw_game(
                context.bot,
                chat_id,
                game_type,
            )

        else:

            opponent = get_user(
                opponent_id
            )

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{GAME_TITLE[game_type]}\n\n"
                    f"👤 {opponent['name']} "
                    "می‌اندازد..."
                ),
            )

            opponent_value = await throw_game(
                context.bot,
                chat_id,
                game_type,
            )

        game["opponent_value"] = opponent_value

        # نتیجه
        if creator_value > opponent_value:

            creator["wins"] += 1
            creator["games"] += 1

            if not bot_mode:

                opponent = get_user(
                    opponent_id
                )

                if opponent:
                    opponent["losses"] += 1
                    opponent["games"] += 1

            # مجموع دو شرط
            add_balance(
                creator_id,
                amount * 2,
            )

            result = (
                f"🏆 برنده: {creator['name']}"
            )

        elif opponent_value > creator_value:

            creator["losses"] += 1
            creator["games"] += 1

            if bot_mode:

                result = "🤖 ربات برنده شد."

            else:

                opponent = get_user(
                    opponent_id
                )

                if opponent:

                    opponent["wins"] += 1
                    opponent["games"] += 1

                    add_balance(
                        opponent_id,
                        amount * 2,
                    )

                    result = (
                        f"🏆 برنده: "
                        f"{opponent['name']}"
                    )

        else:

            creator["games"] += 1

            add_balance(
                creator_id,
                amount,
            )

            if not bot_mode:

                opponent = get_user(
                    opponent_id
                )

                if opponent:

                    opponent["games"] += 1

                    add_balance(
                        opponent_id,
                        amount,
                    )

            result = (
                "🤝 مساوی شد.\n"
                "مبلغ شرط برگشت داده شد."
            )

        game["status"] = "finished"

        save_db()

        second_name = (
            "🤖 ربات"
        )

        if not bot_mode:

            opponent = get_user(
                opponent_id
            )

            if opponent:
                second_name = opponent["name"]

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{GAME_TITLE[game_type]} "
                "نتیجه\n\n"
                f"👤 {creator['name']}: "
                f"{creator_value}\n"
                f"👤 {second_name}: "
                f"{opponent_value}\n\n"
                f"{result}\n\n"
                f"🪙 شرط: {fmt(amount)} {UNIT}"
            ),
        )

    except Exception as e:

        logger.exception(
            "GAME ERROR"
        )

        # بازگشت موجودی در خطا
        add_balance(
            creator_id,
            amount,
        )

        if not bot_mode and opponent_id:
            add_balance(
                opponent_id,
                amount,
            )

        game["status"] = "refunded"

        save_db()

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "⚠️ بازی با خطا متوقف شد.\n\n"
                "🪙 مبلغ شرط به بازیکنان برگشت داده شد."
            ),
        )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):
    query = update.callback_query
    user = update.effective_user

    game_id = query.data.split(
        ":",
        1,
    )[1]

    game = db["games"].get(
        game_id
    )

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
            "❌ این بازی دیگر قابل لغو نیست.",
            show_alert=True,
        )
        return

    amount = float(
        game["amount"]
    )

    add_balance(
        user.id,
        amount,
    )

    game["status"] = "cancelled"

    save_db()

    await query.answer(
        "لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        f"🪙 {fmt(amount)} {UNIT} "
        "برگشت داده شد."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    text = (
        message.text or ""
    ).strip()

    # ---------------- GROUP ----------------

    if message.chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        # انتقال
        if text.startswith("انتقال"):

            await group_transfer(
                update,
                context,
            )

            return

        # بازی
        if parse_game(text):

            await create_game(
                update,
                context,
            )

            return

        # موجودی در گپ
        if text == "موجودی":

            if not is_admin(user.id):

                if not await check_membership(
                    context.bot,
                    user.id,
                ):
                    return

            await message.reply_text(
                "💰 موجودی\n\n"
                f"{fmt(balance(user.id))} {UNIT}"
            )

            return


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):
    logger.exception(
        "UNHANDLED ERROR: %s",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    app = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    # START
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # HELP
    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    # ADMIN
    app.add_handler(
        CommandHandler(
            "addbalance",
            addbalance,
        )
    )

    app.add_handler(
        CommandHandler(
            "removebalance",
            removebalance,
        )
    )

    app.add_handler(
        CommandHandler(
            "userbalance",
            userbalance,
        )
    )

    app.add_handler(
        CommandHandler(
            "enable",
            admin_enable_command,
        )
    )

    # BUTTONS
    app.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

    # TEXT
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "🎲 ۱ تاس ۰.۵\n"
        "🏀 ۱ بسکتبال ۰.۵\n"
        "🎯 ۱ دارت ۰.۵\n"
        "🎳 ۱ بولینگ ۰.۵\n\n"
        "🔄 انتقال در گپ:\n"
        "روی پیام کاربر Reply کن و بنویس:\n"
        "انتقال ۰.۵"
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
