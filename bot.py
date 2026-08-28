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
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# BET_BT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

DATA_FILE = Path("bet_bt_data.json")

UNIT = "VTRX"
REF_REWARD = 0.05

# کانال/گروه جوین اجباری
REQUIRED_CHAT = "@zobxt"
REQUIRED_URL = "https://t.me/zobxt"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def new_db():
    return {
        "users": {},
        "games": {},
        "referrals": {},
    }


def load_db():
    if not DATA_FILE.exists():
        return new_db()

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return new_db()

        data.setdefault("users", {})
        data.setdefault("games", {})
        data.setdefault("referrals", {})

        return data

    except Exception as e:
        logger.error("DB load error: %s", e)
        return new_db()


db = load_db()


def save_db():
    tmp = DATA_FILE.with_suffix(".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            db,
            f,
            ensure_ascii=False,
            indent=2,
        )

    tmp.replace(DATA_FILE)


# ============================================================
# USERS
# ============================================================

def ensure_user(user):
    uid = str(user.id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "id": user.id,
            "name": user.full_name or "کاربر",
            "username": user.username or "",
            "balance": 0.0,
            "wins": 0,
            "losses": 0,
            "games": 0,
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


def get_balance(user_id):
    user = get_user(user_id)

    if not user:
        return 0.0

    return float(user.get("balance", 0.0))


def add_balance(user_id, amount):
    user = get_user(user_id)

    if user:
        user["balance"] = round(
            float(user.get("balance", 0.0)) + float(amount),
            8,
        )


def remove_balance(user_id, amount):
    user = get_user(user_id)

    if user:
        user["balance"] = round(
            float(user.get("balance", 0.0)) - float(amount),
            8,
        )


def fmt_amount(value):
    value = float(value)

    if value.is_integer():
        return f"{int(value)}"

    return f"{value:.8f}".rstrip("0").rstrip(".")


# ============================================================
# JOIN REQUIRED
# ============================================================

async def is_member(bot, user_id):
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
            "Join check failed: %s",
            e,
        )

        return False


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 عضویت در کانال",
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

    if await is_member(
        context.bot,
        user.id,
    ):
        return True

    text = (
        "🔒 برای استفاده از ربات ابتدا باید عضو "
        "کانال/گروه مربوطه شوی.\n\n"
        "بعد از عضویت روی «بررسی عضویت» بزن."
    )

    if update.callback_query:
        try:
            await update.callback_query.answer(
                "ابتدا عضو شوید.",
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

def main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📂 زیرمجموعه",
                    callback_data="referrals",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💰 موجودی",
                    callback_data="balance",
                ),
                InlineKeyboardButton(
                    "🔄 انتقال",
                    callback_data="transfer_help",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎮 مثال بازی",
                    callback_data="examples",
                ),
            ],
        ]
    )


# ============================================================
# GAMES MENU
# ============================================================

def games_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎲 تاس",
                    callback_data="game_dice",
                ),
                InlineKeyboardButton(
                    "🏀 بسکتبال",
                    callback_data="game_basketball",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎯 دارت",
                    callback_data="game_darts",
                ),
                InlineKeyboardButton(
                    "🎳 بولینگ",
                    callback_data="game_bowling",
                ),
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

    # Referral
    if context.args:
        code = context.args[0]

        try:
            referrer_id = int(code)
        except ValueError:
            referrer_id = None

        if (
            referrer_id
            and referrer_id != user.id
            and get_user(referrer_id)
            and not get_user(user.id).get("referrer")
        ):
            db["users"][str(user.id)]["referrer"] = referrer_id

            db["users"][
                str(referrer_id)
            ].setdefault("referrals", []).append(
                user.id
            )

            add_balance(
                referrer_id,
                REF_REWARD,
            )

            save_db()

    if not await require_join(
        update,
        context,
    ):
        return

    if update.effective_chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        await update.message.reply_text(
            "✅ BET_BT فعال است.\n\n"
            "برای استفاده از منوی ربات، "
            "در پیوی /start را بزن."
        )

        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "به ربات خوش آمدی.",
        reply_markup=main_menu(),
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):
    user = update.effective_user

    if not user:
        return

    if not await require_join(
        update,
        context,
    ):
        return

    ensure_user(user)

    amount = get_balance(user.id)

    text = (
        "💰 موجودی\n\n"
        f"🪙 {fmt_amount(amount)} {UNIT}"
    )

    if update.callback_query:
        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            text
        )

    else:
        await update.message.reply_text(
            text
        )


# ============================================================
# REFERRALS
# ============================================================

async def show_referrals(update, context):
    user = update.effective_user

    if not user:
        return

    if not await require_join(
        update,
        context,
    ):
        return

    ensure_user(user)

    data = get_user(user.id)

    referrals = data.get(
        "referrals",
        [],
    )

    bot_username = None

    try:
        me = await context.bot.get_me()
        bot_username = me.username
    except Exception:
        pass

    if bot_username:
        ref_link = (
            f"https://t.me/{bot_username}"
            f"?start={user.id}"
        )
    else:
        ref_link = str(user.id)

    text = (
        "📂 زیرمجموعه\n\n"
        f"👥 تعداد رِف: {len(referrals)}\n"
        f"🎁 پاداش هر رِف: {fmt_amount(REF_REWARD)} {UNIT}\n\n"
        f"🔗 لینک دعوت:\n"
        f"{ref_link}"
    )

    if update.callback_query:
        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            text
        )


# ============================================================
# EXAMPLES
# ============================================================

async def examples(update, context):
    if not await require_join(
        update,
        context,
    ):
        return

    text = (
        "🎮 مثال بازی\n\n"
        "فرمت دستور:\n"
        "۱ [بازی] [مبلغ]\n\n"
        "نمونه‌ها:\n\n"
        "🎲 ۱ تاس ۰.۵\n"
        "🏀 ۱ بسکتبال ۰.۵\n"
        "🎯 ۱ دارت ۰.۵\n"
        "🎳 ۱ بولینگ ۰.۵\n\n"
        "بعد از ساخت بازی:\n"
        "🎮 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "❌ لغو"
    )

    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        text
    )


# ============================================================
# TRANSFER HELP
# ============================================================

async def transfer_help(update, context):
    if not await require_join(
        update,
        context,
    ):
        return

    await update.callback_query.answer()

    await update.callback_query.message.reply_text(
        "🔄 انتقال\n\n"
        "داخل گپ روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 0.5\n\n"
        "مبلغ از موجودی فرستنده کم و به گیرنده "
        "اضافه می‌شود."
    )


# ============================================================
# GAME CREATION
# ============================================================

GAME_NAMES = {
    "dice": "🎲 تاس",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت",
    "bowling": "🎳 بولینگ",
}


def create_game(
    chat_id,
    creator_id,
    game_type,
    amount,
):
    game_id = uuid.uuid4().hex[:12]

    db["games"][game_id] = {
        "id": game_id,
        "chat_id": chat_id,
        "creator_id": creator_id,
        "opponent_id": None,
        "game_type": game_type,
        "amount": float(amount),
        "status": "waiting",
        "creator_value": None,
        "opponent_value": None,
    }

    return game_id


def game_join_keyboard(game_id):
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


# ============================================================
# GAME COMMAND
# ============================================================

def parse_game_command(text):
    if not text:
        return None

    parts = text.strip().split()

    if len(parts) < 3:
        return None

    if parts[0] not in (
        "۱",
        "1",
    ):
        return None

    game_map = {
        "تاس": "dice",
        "داس": "dice",
        "بسکتبال": "basketball",
        "دارت": "darts",
        "بولینگ": "bowling",
    }

    game_type = game_map.get(
        parts[1].lower()
    )

    if not game_type:
        return None

    try:
        amount = float(
            parts[2].replace(
                ",",
                ".",
            )
        )
    except ValueError:
        return None

    if amount <= 0:
        return None

    return game_type, amount


async def create_game_from_message(
    update,
    context,
):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return False

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return False

    if not await is_member(
        context.bot,
        user.id,
    ):
        await require_join(
            update,
            context,
        )

        return True

    parsed = parse_game_command(
        message.text
    )

    if not parsed:
        return False

    game_type, amount = parsed

    ensure_user(user)

    balance = get_balance(user.id)

    if balance < amount:
        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {fmt_amount(balance)} {UNIT}\n"
            f"🪙 شرط: {fmt_amount(amount)} {UNIT}"
        )

        return True

    # رزرو مبلغ
    remove_balance(
        user.id,
        amount,
    )

    game_id = create_game(
        message.chat.id,
        user.id,
        game_type,
        amount,
    )

    save_db()

    await message.reply_text(
        f"{GAME_NAMES[game_type]} بازی ساخته شد!\n\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🪙 شرط: {fmt_amount(amount)} {UNIT}\n\n"
        "یک گزینه را انتخاب کن:",
        reply_markup=game_join_keyboard(
            game_id
        ),
    )

    return True


# ============================================================
# TELEGRAM DICE
# ============================================================

async def throw_game(
    bot,
    chat_id,
    game_type,
):
    emoji = {
        "dice": "🎲",
        "basketball": "🏀",
        "darts": "🎯",
        "bowling": "🎳",
    }[game_type]

    msg = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji,
    )

    return int(msg.dice.value)


# ============================================================
# FRIEND GAME
# ============================================================

async def join_friend_game(
    update,
    context,
):
    query = update.callback_query
    user = update.effective_user

    if not user:
        return

    if not await is_member(
        context.bot,
        user.id,
    ):
        await query.answer(
            "ابتدا باید عضو شوید.",
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
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True,
        )

        return

    if game["creator_id"] == user.id:
        await query.answer(
            "❌ سازنده نمی‌تواند وارد بازی خودش شود.",
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

    # فقط یک نفر وارد شود
    game["opponent_id"] = user.id
    game["status"] = "playing"

    remove_balance(
        user.id,
        amount,
    )

    save_db()

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.edit_message_text(
        "🎮 بازیکن دوم وارد شد.\n\n"
        f"{GAME_NAMES[game['game_type']]}\n"
        f"🪙 شرط: {fmt_amount(amount)} {UNIT}\n\n"
        "⏳ بازی شروع می‌شود..."
    )

    await asyncio.sleep(1)

    await play_match(
        game,
        context,
    )


# ============================================================
# BOT GAME
# ============================================================

async def play_with_bot(
    update,
    context,
):
    query = update.callback_query
    user = update.effective_user

    if not user:
        return

    if not await is_member(
        context.bot,
        user.id,
    ):
        await query.answer(
            "ابتدا باید عضو شوید.",
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
            "❌ بازی دیگر قابل اجرا نیست.",
            show_alert=True,
        )

        return

    if game["creator_id"] != user.id:
        await query.answer(
            "❌ فقط سازنده می‌تواند با ربات بازی کند.",
            show_alert=True,
        )

        return

    game["opponent_id"] = 0
    game["status"] = "playing"

    save_db()

    await query.answer()

    await query.edit_message_text(
        f"{GAME_NAMES[game['game_type']]}\n\n"
        "🤖 بازی با ربات انتخاب شد.\n\n"
        "⏳ بازی شروع می‌شود..."
    )

    await asyncio.sleep(1)

    await play_match(
        game,
        context,
        against_bot=True,
    )


# ============================================================
# PLAY MATCH
# سازنده همیشه اول می‌اندازد
# ============================================================

async def play_match(
    game,
    context,
    against_bot=False,
):
    chat_id = game["chat_id"]
    game_type = game["game_type"]

    creator_id = game["creator_id"]
    opponent_id = game["opponent_id"]

    creator = get_user(
        creator_id
    )

    if not creator:
        return

    amount = float(
        game["amount"]
    )

    try:
        # ----------------------------------------------------
        # سازنده اول
        # ----------------------------------------------------

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{GAME_NAMES[game_type]}\n\n"
                f"👤 {creator['name']} اول بازی می‌کند..."
            ),
        )

        creator_value = await throw_game(
            context.bot,
            chat_id,
            game_type,
        )

        game["creator_value"] = creator_value

        await asyncio.sleep(1)

        # ----------------------------------------------------
        # نفر دوم / ربات
        # ----------------------------------------------------

        if against_bot:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{GAME_NAMES[game_type]}\n\n"
                    "🤖 حالا ربات بازی می‌کند..."
                ),
            )

            bot_value = await throw_game(
                context.bot,
                chat_id,
                game_type,
            )

            game["opponent_value"] = bot_value

        else:

            opponent = get_user(
                opponent_id
            )

            if not opponent:
                return

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{GAME_NAMES[game_type]}\n\n"
                    f"👤 {opponent['name']} "
                    "حالا بازی می‌کند..."
                ),
            )

            opponent_value = await throw_game(
                context.bot,
                chat_id,
                game_type,
            )

            game["opponent_value"] = opponent_value

        # ----------------------------------------------------
        # RESULT
        # ----------------------------------------------------

        v1 = int(
            game["creator_value"]
        )

        v2 = int(
            game["opponent_value"]
        )

        if v1 > v2:

            creator["wins"] += 1
            creator["games"] += 1

            if not against_bot:
                opponent = get_user(
                    opponent_id
                )

                if opponent:
                    opponent["losses"] += 1
                    opponent["games"] += 1

            # در حالت بازی با ربات، مبلغ جایزه
            # صرفاً داخل سیستم مجازی حساب می‌شود.
            creator["balance"] = round(
                creator["balance"] + amount * 2,
                8,
            )

            winner_text = (
                f"🏆 برنده: {creator['name']}"
            )

        elif v2 > v1:

            creator["losses"] += 1
            creator["games"] += 1

            if not against_bot:
                opponent = get_user(
                    opponent_id
                )

                if opponent:
                    opponent["wins"] += 1
                    opponent["games"] += 1

                    opponent["balance"] = round(
                        opponent["balance"]
                        + amount * 2,
                        8,
                    )

                winner_text = (
                    f"🏆 برنده: "
                    f"{opponent['name']}"
                )

            else:
                winner_text = "🤖 ربات برنده شد."

        else:

            creator["games"] += 1
            creator["balance"] = round(
                creator["balance"] + amount,
                8,
            )

            if not against_bot:
                opponent = get_user(
                    opponent_id
                )

                if opponent:
                    opponent["games"] += 1
                    opponent["balance"] = round(
                        opponent["balance"] + amount,
                        8,
                    )

            winner_text = (
                "🤝 مساوی شد؛ "
                "مبلغ بازی برگشت داده شد."
            )

        game["status"] = "finished"

        save_db()

        opponent_name = (
            "🤖 ربات"
        )

        if not against_bot:
            opponent = get_user(
                opponent_id
            )

            if opponent:
                opponent_name = opponent["name"]

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{GAME_NAMES[game_type]} "
                "نتیجه بازی\n\n"

                f"👤 {creator['name']}: "
                f"{v1}\n"

                f"👤 {opponent_name}: "
                f"{v2}\n\n"

                f"{winner_text}\n\n"

                f"🪙 شرط: "
                f"{fmt_amount(amount)} {UNIT}"
            ),
        )

    except Exception as e:

        logger.error(
            "Match error: %s",
            e,
        )

        # اگر بازی به خطا خورد،
        # مبلغ سازنده و نفر دوم برگردانده شود.
        if game.get("status") == "playing":

            add_balance(
                creator_id,
                amount,
            )

            if (
                not against_bot
                and opponent_id
            ):
                add_balance(
                    opponent_id,
                    amount,
                )

            game["status"] = "refunded"

            save_db()

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ بازی با خطا متوقف شد.\n\n"
                        "🪙 مبلغ بازی برگشت داده شد."
                    ),
                )
            except Exception:
                pass


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(
    update,
    context,
):
    query = update.callback_query
    user = update.effective_user

    if not user:
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
        "بازی لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        f"🪙 {fmt_amount(amount)} {UNIT} "
        "به موجودی سازنده برگشت داده شد."
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(
    update,
    context,
):
    query = update.callback_query
    data = query.data or ""

    user = update.effective_user

    if data == "check_join":

        if await is_member(
            context.bot,
            user.id,
        ):
            await query.answer(
                "✅ عضویت تأیید شد."
            )

            await query.message.reply_text(
                "✅ حالا می‌توانی از BET_BT استفاده کنی.",
                reply_markup=main_menu(),
            )

        else:
            await query.answer(
                "❌ هنوز عضو نشده‌ای.",
                show_alert=True,
            )

        return

    if data == "balance":
        await show_balance(
            update,
            context,
        )
        return

    if data == "referrals":
        await show_referrals(
            update,
            context,
        )
        return

    if data == "transfer_help":
        await transfer_help(
            update,
            context,
        )
        return

    if data == "examples":
        await examples(
            update,
            context,
        )
        return

    if data.startswith("friend:"):
        await join_friend_game(
            update,
            context,
        )
        return

    if data.startswith("bot:"):
        await play_with_bot(
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

    if data.startswith("game_"):

        await query.answer()

        game_type = data.replace(
            "game_",
            "",
        )

        names = {
            "dice": "🎲 تاس",
            "basketball": "🏀 بسکتبال",
            "darts": "🎯 دارت",
            "bowling": "🎳 بولینگ",
        }

        await query.message.reply_text(
            f"{names.get(game_type, '🎮 بازی')}\n\n"
            "برای ساخت بازی داخل گپ بنویس:\n\n"
            f"۱ {names.get(game_type, 'بازی').split(' ', 1)[-1]} ۰.۵"
        )

        return

    await query.answer()


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context,
):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    text = (
        message.text
        or ""
    ).strip()

    ensure_user(user)

    # --------------------------------------------------------
    # PRIVATE
    # --------------------------------------------------------

    if message.chat.type == ChatType.PRIVATE:

        if text in (
            "موجودی",
            "💰 موجودی",
        ):
            await show_balance(
                update,
                context,
            )

            return

        if text in (
            "زیرمجموعه",
            "📂 زیرمجموعه",
        ):
            await show_referrals(
                update,
                context,
            )

            return

        if text in (
            "انتقال",
            "🔄 انتقال",
        ):
            await transfer_help(
                update,
                context,
            )

            return

        if text in (
            "مثال بازی",
            "🎮 مثال بازی",
        ):
            await examples(
                update,
                context,
            )

            return

        return

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if message.chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        # game command
        handled = await create_game_from_message(
            update,
            context,
        )

        if handled:
            return

        # transfer command
        if text.startswith(
            "انتقال"
        ):

            await group_transfer(
                update,
                context,
            )

            return

        if text == "موجودی":

            if not await is_member(
                context.bot,
                user.id,
            ):
                return

            await show_balance(
                update,
                context,
            )

            return


# ============================================================
# GROUP TRANSFER
# ============================================================

async def group_transfer(
    update,
    context,
):
    message = update.message
    user = update.effective_user

    if not message.reply_to_message:
        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )

        return

    target = (
        message.reply_to_message.from_user
    )

    if not target:
        return

    if target.is_bot:
        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    if target.id == user.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    parts = message.text.split()

    if len(parts) < 2:
        await message.reply_text(
            "❌ مبلغ را وارد کن.\n\n"
            "مثال:\n"
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

    if not await is_member(
        context.bot,
        user.id,
    ):
        return

    ensure_user(user)
    ensure_user(target)

    if get_balance(user.id) < amount:
        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    remove_balance(
        user.id,
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
        f"🪙 مبلغ: {fmt_amount(amount)} {UNIT}\n"
        f"💰 موجودی جدید: "
        f"{fmt_amount(get_balance(user.id))} {UNIT}"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):
    logger.error(
        "Unhandled error: %s",
        context.error,
    )


# ============================================================
# APP
# ============================================================

def main():

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_handler,
        )
    )

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
        "BET_BT started"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


# ============================================================
# HELP COMMAND
# ============================================================

async def help_command(
    update,
    context,
):
    if not await require_join(
        update,
        context,
    ):
        return

    await update.message.reply_text(
        "ℹ️ راهنما\n\n"
        "🎮 مثال بازی:\n"
        "۱ تاس ۰.۵\n"
        "۱ بسکتبال ۰.۵\n"
        "۱ دارت ۰.۵\n"
        "۱ بولینگ ۰.۵\n\n"
        "🔄 انتقال در گپ با Reply:\n"
        "انتقال ۰.۵"
    )


if __name__ == "__main__":
    main()
