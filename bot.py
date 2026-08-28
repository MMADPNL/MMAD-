import os
import re
import sqlite3
import secrets
import logging
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = 8552447077
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", str(OWNER_ID)).split(",")
    if x.strip().isdigit()
}
ADMIN_IDS.add(OWNER_ID)

FORCE_CHANNEL = "@zobxt"

# Use /data when available so a persistent volume can keep the DB.
DB_PATH = "/data/bot.db" if os.path.isdir("/data") and os.access("/data", os.W_OK) else "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bot")

# In-memory active game state only; balances are always in SQLite.
pending_games = {}       # game_id -> dict
user_sessions = {}       # user_id -> dict


# =========================
# DATABASE
# =========================
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_key TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                kind TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_key TEXT NOT NULL UNIQUE,
                game TEXT NOT NULL,
                mode TEXT NOT NULL,
                player1 INTEGER NOT NULL,
                player2 INTEGER,
                bet REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                address TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
    finally:
        con.close()


def ensure_user(user):
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO users(user_id, name, created_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET name=excluded.name
            """,
            (user.id, user.full_name or "", datetime.utcnow().isoformat())
        )
    finally:
        con.close()


def get_balance(user_id):
    con = connect()
    try:
        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        return float(row["balance"]) if row else 0.0
    finally:
        con.close()


def money(v):
    return f"{float(v):.6f}".rstrip("0").rstrip(".")


def atomic_balance_change(user_id, amount, kind, tx_key):
    """
    One SQLite transaction:
    - tx_key is unique, so the same operation cannot be applied twice.
    - negative amount is rejected when it would make balance negative.
    """
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        existing = con.execute(
            "SELECT id FROM transactions WHERE tx_key=?", (tx_key,)
        ).fetchone()
        if existing:
            con.execute("ROLLBACK")
            return False

        row = con.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            con.execute(
                "INSERT INTO users(user_id,name,created_at) VALUES(?,?,?)",
                (user_id, "", datetime.utcnow().isoformat())
            )
            current = 0.0
        else:
            current = float(row["balance"])

        new_balance = round(current + amount, 6)
        if new_balance < -1e-9:
            con.execute("ROLLBACK")
            return False

        con.execute(
            "UPDATE users SET balance=? WHERE user_id=?",
            (new_balance, user_id)
        )
        con.execute(
            """
            INSERT INTO transactions(tx_key,user_id,amount,kind,created_at)
            VALUES(?,?,?,?,?)
            """,
            (tx_key, user_id, amount, kind, datetime.utcnow().isoformat())
        )
        con.execute("COMMIT")
        return True
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()


# =========================
# ACCESS / CAPTCHA
# =========================
def captcha_keyboard(a, b):
    correct = a + b
    choices = [correct, correct + 1, max(0, correct - 1), correct + 2]
    # Unique choices
    choices = list(dict.fromkeys(choices))
    while len(choices) < 4:
        choices.append(correct + secrets.randbelow(10) + 3)
        choices = list(dict.fromkeys(choices))
    import random
    random.shuffle(choices)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(str(x), callback_data=f"captcha:{x}:{correct}")]
        for x in choices
    ])


async def is_joined(user_id, bot):
    try:
        member = await bot.get_chat_member(FORCE_CHANNEL, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def access_check(update, context, require_captcha=True):
    user = update.effective_user
    if not user:
        return False

    ensure_user(user)

    if not await is_joined(user.id, context.bot):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 عضویت در کانال", url="https://t.me/zobxt")],
            [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join")]
        ])
        target = update.effective_message
        if target:
            await target.reply_text(
                "❌ برای استفاده از ربات ابتدا عضو کانال شوید.",
                reply_markup=kb
            )
        return False

    con = connect()
    try:
        row = con.execute(
            "SELECT verified FROM users WHERE user_id=?", (user.id,)
        ).fetchone()
        verified = bool(row["verified"]) if row else False
    finally:
        con.close()

    if require_captcha and not verified:
        a = secrets.randbelow(9) + 1
        b = secrets.randbelow(9) + 1
        user_sessions[user.id] = {"captcha": a + b}
        target = update.effective_message
        if target:
            await target.reply_text(
                f"🔐 کپچا\n\n{a} + {b} = ؟",
                reply_markup=captcha_keyboard(a, b)
            )
        return False

    return True


# =========================
# KEYBOARDS
# =========================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["🔄 انتقال", "💸 برداشت"],
        ],
        resize_keyboard=True
    )


def game_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🎲 تاس", "🎳 بولینگ"],
            ["🎯 دارت", "🏀 بسکتبال"],
            ["👥 بازی با دوستان", "🤖 بازی با ربات"],
            ["❌ لغو"],
        ],
        resize_keyboard=True
    )


def admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 روشن", callback_data="admin:on"),
         InlineKeyboardButton("🔴 خاموش", callback_data="admin:off")],
        [InlineKeyboardButton("💰 موجودی کاربران", callback_data="admin:balances")],
        [InlineKeyboardButton("➕ افزایش موجودی", callback_data="admin:add")],
        [InlineKeyboardButton("➖ کسر موجودی", callback_data="admin:sub")],
        [InlineKeyboardButton("📋 برداشت‌ها", callback_data="admin:withdrawals")],
    ])


# =========================
# GAME HELPERS
# =========================
GAME_ALIASES = {
    "تاس": "dice", "dice": "dice", "🎲": "dice",
    "بولینگ": "bowling", "bowling": "bowling", "🎳": "bowling",
    "دارت": "dart", "dart": "dart", "🎯": "dart",
    "بسکتبال": "basketball", "basketball": "basketball", "🏀": "basketball",
}

GAME_EMOJI = {
    "dice": "🎲",
    "bowling": "🎳",
    "dart": "🎯",
    "basketball": "🏀",
}

GAME_PERSIAN = {
    "dice": "تاس",
    "bowling": "بولینگ",
    "dart": "دارت",
    "basketball": "بسکتبال",
}

TELEGRAM_DICE_MAP = {
    "dice": "🎲",
    "bowling": "🎳",
    "dart": "🎯",
    "basketball": "🏀",
}


def parse_game_command(text):
    """
    Accepted:
      1 تاس 0.1
      1 بولینگ 0.1
      1 دارت 0.1
      1 بسکتبال 0.1
      1 dice 0.1
      1 bowling 0.1
      1 dart 0.1
      1 basketball 0.1

    Also accepts:
      تاس 0.1
      dice 0.1
    """
    s = (text or "").strip().lower().replace(",", ".")
    pattern = re.compile(
        r"^(?:(\d+)\s+)?(تاس|بولینگ|دارت|بسکتبال|dice|bowling|dart|basketball)\s+([0-9]+(?:\.[0-9]+)?)$"
    )
    m = pattern.match(s)
    if not m:
        return None
    count = int(m.group(1) or 1)
    if count < 1:
        return None
    game = GAME_ALIASES[m.group(2)]
    bet = round(float(m.group(3)), 6)
    if bet <= 0:
        return None
    return game, bet, count


async def send_game_roll(bot, chat_id, game):
    return await bot.send_dice(chat_id=chat_id, emoji=TELEGRAM_DICE_MAP[game])


def reward_amount(bet):
    # User receives 1.8x the stake; 0.2x is retained by owner.
    return round(bet * 1.8, 6)


def owner_fee(bet):
    return round(bet * 0.2, 6)


async def settle_win(user_id, bet, game_key):
    # The stake was already reserved. Winner gets 1.8x total.
    return atomic_balance_change(
        user_id, reward_amount(bet), "game_win",
        f"gamewin:{game_key}:{user_id}"
    )


async def reserve_bet(user_id, bet, game_key):
    return atomic_balance_change(
        user_id, -bet, "game_bet",
        f"gamebet:{game_key}:{user_id}"
    )


async def return_bet(user_id, bet, game_key):
    return atomic_balance_change(
        user_id, bet, "game_draw_return",
        f"gamedraw:{game_key}:{user_id}"
    )


# =========================
# START / CAPTCHA
# =========================
async def start(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("ربات را در پی‌وی استفاده کنید.")
        return
    if not await access_check(update, context):
        return
    await update.message.reply_text(
        "✅ خوش آمدید.",
        reply_markup=main_keyboard()
    )


async def callback_handler(update, context):
    q = update.callback_query
    await q.answer()
    user = q.from_user
    ensure_user(user)

    if q.data == "check_join":
        if await is_joined(user.id, context.bot):
            a = secrets.randbelow(9) + 1
            b = secrets.randbelow(9) + 1
            user_sessions[user.id] = {"captcha": a + b}
            await q.message.reply_text(
                f"🔐 کپچا\n\n{a} + {b} = ؟",
                reply_markup=captcha_keyboard(a, b)
            )
        else:
            await q.message.reply_text("❌ هنوز عضویت شما تأیید نشده است.")
        return

    if q.data.startswith("captcha:"):
        _, answer, correct = q.data.split(":")
        if int(answer) == int(correct) and await is_joined(user.id, context.bot):
            con = connect()
            try:
                con.execute("UPDATE users SET verified=1 WHERE user_id=?", (user.id,))
            finally:
                con.close()
            user_sessions.pop(user.id, None)
            await q.message.reply_text(
                "✅ تأیید شد.",
                reply_markup=main_keyboard()
            )
        else:
            await q.message.reply_text("❌ پاسخ نادرست یا عضویت تأیید نشده.")
        return

    if q.data.startswith("join:"):
        game_id = q.data.split(":", 1)[1]
        await join_friend_game(q, context, game_id)
        return

    if q.data.startswith("cancelgame:"):
        game_id = q.data.split(":", 1)[1]
        await cancel_friend_game(q, context, game_id)
        return

    if q.data.startswith("admin:"):
        await admin_callback(q, context)
        return

    if q.data.startswith("wd:"):
        await withdrawal_callback(q, context)
        return


# =========================
# GAME FLOW
# =========================
async def begin_game(update, context, game, bet, mode):
    user = update.effective_user
    if not await access_check(update, context):
        return

    if not context.user_data.get("verified", True):
        pass

    if bet < 0.000001:
        await update.message.reply_text("❌ مبلغ نامعتبر است.")
        return

    if get_balance(user.id) + 1e-9 < bet:
        await update.message.reply_text("❌ موجودی کافی نیست.")
        return

    game_id = secrets.token_hex(8)
    reserved = await reserve_bet(user.id, bet, game_id)
    if not reserved:
        await update.message.reply_text("❌ موجودی کافی نیست یا تراکنش تکراری است.")
        return

    if mode == "bot":
        pending_games[game_id] = {
            "mode": "bot", "game": game, "bet": bet,
            "p1": user.id, "stage": "p1"
        }
        context.user_data["game_id"] = game_id
        await update.message.reply_text(
            f"{GAME_EMOJI[game]} بازی با ربات\n"
            f"شرط: {money(bet)} TRX\n\n"
            f"ابتدا شما {GAME_EMOJI[game]} را بفرستید."
        )
    else:
        pending_games[game_id] = {
            "mode": "friends", "game": game, "bet": bet,
            "p1": user.id, "p2": None, "stage": "waiting"
        }
        await update.message.reply_text(
            f"{GAME_EMOJI[game]} بازی با دوستان ساخته شد.\n"
            f"شرط: {money(bet)} TRX\n\n"
            "فقط یک نفر دیگر می‌تواند وارد شود.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 ورود به بازی", callback_data=f"join:{game_id}")],
                [InlineKeyboardButton("❌ لغو", callback_data=f"cancelgame:{game_id}")]
            ])
        )


async def process_game_dice(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    game_id = context.user_data.get("game_id")
    if not game_id:
        return
    game = pending_games.get(game_id)
    if not game or game["mode"] != "bot" or game["p1"] != user.id:
        return

    # Require actual Telegram game emoji.
    expected = GAME_EMOJI[game["game"]]
    if update.message.dice is None or update.message.dice.emoji != expected:
        await update.message.reply_text(f"❌ باید {expected} را ارسال کنید.")
        return

    p1_score = update.message.dice.value
    bot_msg = await send_game_roll(context.bot, update.effective_chat.id, game["game"])
    p2_score = bot_msg.dice.value

    if p1_score > p2_score:
        await settle_win(user.id, game["bet"], game_id)
        text = (
            f"{expected} نتیجه\n\n"
            f"👤 شما: {p1_score}\n"
            f"🤖 ربات: {p2_score}\n\n"
            f"🏆 شما برنده شدید!\n"
            f"🎁 جایزه: {money(reward_amount(game['bet']))} TRX"
        )
    elif p1_score < p2_score:
        text = (
            f"{expected} نتیجه\n\n"
            f"👤 شما: {p1_score}\n"
            f"🤖 ربات: {p2_score}\n\n"
            "❌ شما باختید."
        )
    else:
        await return_bet(user.id, game["bet"], game_id)
        text = (
            f"{expected} نتیجه\n\n"
            f"👤 شما: {p1_score}\n"
            f"🤖 ربات: {p2_score}\n\n"
            "🤝 مساوی شد؛ مبلغ شرط برگشت داده شد."
        )

    pending_games.pop(game_id, None)
    context.user_data.pop("game_id", None)
    await update.message.reply_text(text)


async def join_friend_game(q, context, game_id):
    user = q.from_user
    game = pending_games.get(game_id)
    if not game or game["mode"] != "friends" or game["stage"] != "waiting":
        await q.message.reply_text("❌ این بازی دیگر قابل ورود نیست.")
        return
    if game["p1"] == user.id:
        await q.message.reply_text("❌ سازنده نمی‌تواند خودش وارد شود.")
        return
    if not await is_joined(user.id, context.bot):
        await q.message.reply_text("❌ ابتدا عضو کانال شوید.")
        return

    # Verify captcha for callback-based joining.
    con = connect()
    try:
        row = con.execute("SELECT verified FROM users WHERE user_id=?", (user.id,)).fetchone()
        verified = bool(row["verified"]) if row else False
    finally:
        con.close()
    if not verified:
        await q.message.reply_text("❌ ابتدا /start را بزنید و کپچا را تکمیل کنید.")
        return

    if get_balance(user.id) + 1e-9 < game["bet"]:
        await q.message.reply_text("❌ موجودی کافی نیست.")
        return

    reserve_key = f"{game_id}:p2"
    if not await reserve_bet(user.id, game["bet"], reserve_key):
        await q.message.reply_text("❌ موجودی کافی نیست.")
        return

    game["p2"] = user.id
    game["stage"] = "p1"
    pending_games[game_id] = game

    await q.message.reply_text(
        f"✅ وارد بازی شدید.\n"
        f"{GAME_EMOJI[game['game']]} ابتدا سازنده بازی می‌اندازد."
    )
    try:
        await context.bot.send_message(
            game["p1"],
            f"👥 حریف وارد شد.\n{GAME_EMOJI[game['game']]} نوبت شماست؛ ایموجی {GAME_EMOJI[game['game']]} را بفرستید."
        )
    except Exception:
        pass


async def cancel_friend_game(q, context, game_id):
    user = q.from_user
    game = pending_games.get(game_id)
    if not game:
        await q.message.reply_text("❌ بازی پیدا نشد.")
        return
    if game["stage"] != "waiting" or game["p1"] != user.id:
        await q.message.reply_text("❌ امکان لغو این بازی وجود ندارد.")
        return

    await return_bet(game["p1"], game["bet"], game_id)
    pending_games.pop(game_id, None)
    await q.message.reply_text("✅ بازی لغو شد و شرط برگشت داده شد.")


async def process_friend_dice(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    user = update.effective_user
    expected = update.message.dice.emoji if update.message.dice else None

    if not expected:
        return

    for game_id, game in list(pending_games.items()):
        if game["mode"] != "friends":
            continue

        if game["game"] not in TELEGRAM_DICE_MAP:
            continue
        if expected != GAME_EMOJI[game["game"]]:
            continue

        if game["stage"] == "p1" and game["p1"] == user.id:
            game["p1_score"] = update.message.dice.value
            game["stage"] = "p2"
            pending_games[game_id] = game
            await update.message.reply_text(
                f"👤 امتیاز شما: {game['p1_score']}\n"
                f"⏳ نوبت حریف است؛ {expected} را بفرستد."
            )
            try:
                await context.bot.send_message(
                    game["p2"],
                    f"🎮 نوبت شماست؛ {expected} را بفرستید."
                )
            except Exception:
                pass
            return

        if game["stage"] == "p2" and game["p2"] == user.id:
            game["p2_score"] = update.message.dice.value
            s1, s2 = game["p1_score"], game["p2_score"]

            if s1 > s2:
                winner, loser = game["p1"], game["p2"]
            elif s2 > s1:
                winner, loser = game["p2"], game["p1"]
            else:
                # Both stakes are returned on a draw, and the same game can be repeated.
                await return_bet(game["p1"], game["bet"], f"{game_id}:draw1")
                await return_bet(game["p2"], game["bet"], f"{game_id}:draw2")
                game["stage"] = "p1"
                game.pop("p1_score", None)
                game.pop("p2_score", None)
                pending_games[game_id] = game
                await update.message.reply_text(
                    f"{expected} مساوی شد ({s1}-{s2}).\n"
                    "🔄 دوباره هر دو بازیکن بیندازند."
                )
                try:
                    await context.bot.send_message(
                        game["p1"], f"🔄 مساوی شد؛ دوباره {expected} را بفرستید."
                    )
                except Exception:
                    pass
                return

            await settle_win(winner, game["bet"], f"{game_id}:winner")
            text = (
                f"{expected} نتیجه\n\n"
                f"👤 سازنده: {s1}\n"
                f"👥 حریف: {s2}\n\n"
                f"🏆 برنده: {winner}\n"
                f"🎁 جایزه: {money(reward_amount(game['bet']))} TRX"
            )
            try:
                await context.bot.send_message(game["p1"], text)
            except Exception:
                pass
            if game["p2"] != game["p1"]:
                try:
                    await context.bot.send_message(game["p2"], text)
                except Exception:
                    pass
            pending_games.pop(game_id, None)
            return


# =========================
# TRANSFER / WITHDRAW
# =========================
TRANSFER_RE = re.compile(
    r"^(?:انتقال|transfer)\s+([0-9]+(?:[.,][0-9]+)?)$",
    re.IGNORECASE
)


async def do_transfer(update, amount):
    msg = update.message
    sender = update.effective_user

    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        await msg.reply_text("❌ برای انتقال باید روی پیام کاربر Reply کنید.")
        return

    receiver = msg.reply_to_message.from_user
    if receiver.is_bot or receiver.id == sender.id:
        await msg.reply_text("❌ انتقال به این کاربر ممکن نیست.")
        return
    if amount <= 0:
        await msg.reply_text("❌ مبلغ نامعتبر است.")
        return

    ensure_user(receiver)

    # Atomic debit first, then credit. A unique transfer key prevents duplicate processing.
    key = f"transfer:{sender.id}:{receiver.id}:{msg.message_id}"
    ok = atomic_balance_change(sender.id, -amount, "transfer_out", key + ":out")
    if not ok:
        await msg.reply_text("❌ موجودی کافی نیست یا انتقال قبلاً انجام شده.")
        return

    try:
        atomic_balance_change(receiver.id, amount, "transfer_in", key + ":in")
    except Exception:
        # Compensate if credit unexpectedly fails.
        atomic_balance_change(sender.id, amount, "transfer_rollback", key + ":rollback")
        await msg.reply_text("❌ انتقال انجام نشد.")
        return

    await msg.reply_text(
        f"✅ انتقال انجام شد.\n"
        f"💰 مبلغ: {money(amount)} TRX"
    )


async def start_withdraw(update):
    user_sessions[update.effective_user.id] = {"withdraw_step": "amount"}
    await update.message.reply_text("💸 مقدار برداشت را وارد کنید.\nحداقل برداشت: 2 TRX")


TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")


async def handle_withdraw_text(update, context):
    uid = update.effective_user.id
    session = user_sessions.get(uid, {})
    text = (update.message.text or "").strip()

    if session.get("withdraw_step") == "amount":
        try:
            amount = round(float(text.replace(",", ".")), 6)
        except ValueError:
            await update.message.reply_text("❌ مقدار نامعتبر است.")
            return
        if amount < 2:
            await update.message.reply_text("❌ حداقل برداشت 2 TRX است.")
            return
        if get_balance(uid) + 1e-9 < amount:
            await update.message.reply_text("❌ موجودی کافی نیست.")
            return
        session["amount"] = amount
        session["withdraw_step"] = "address"
        user_sessions[uid] = session
        await update.message.reply_text("📥 آدرس TRON (TRC20) را ارسال کنید.")
        return

    if session.get("withdraw_step") == "address":
        address = text
        if not TRON_RE.match(address):
            await update.message.reply_text("❌ آدرس TRC20 نامعتبر است.")
            return
        amount = session["amount"]
        wid = secrets.token_hex(10)

        # Reserve the funds atomically; if the same request is retried it cannot debit twice.
        ok = atomic_balance_change(
            uid, -amount, "withdraw_pending", f"withdraw:{wid}:reserve"
        )
        if not ok:
            await update.message.reply_text("❌ موجودی کافی نیست.")
            return

        con = connect()
        try:
            cur = con.execute(
                """
                INSERT INTO withdrawals(user_id,amount,address,status,created_at)
                VALUES(?,?,?,?,?)
                """,
                (uid, amount, address, "pending", datetime.utcnow().isoformat())
            )
            withdrawal_id = cur.lastrowid
        finally:
            con.close()

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید", callback_data=f"wd:approve:{withdrawal_id}")],
            [InlineKeyboardButton("❌ رد", callback_data=f"wd:reject:{withdrawal_id}")]
        ])

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id,
                    f"💸 درخواست برداشت #{withdrawal_id}\n\n"
                    f"👤 کاربر: {uid}\n"
                    f"💰 مقدار: {money(amount)} TRX\n"
                    f"📍 آدرس: {address}",
                    reply_markup=buttons
                )
            except Exception:
                log.exception("Could not notify admin %s", admin_id)

        user_sessions.pop(uid, None)
        await update.message.reply_text("✅ درخواست برداشت برای مالک/ادمین ارسال شد.")
        return


async def withdrawal_callback(q, context):
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    parts = q.data.split(":")
    if len(parts) != 3:
        return
    action, wid_s = parts[1], parts[2]
    try:
        wid = int(wid_s)
    except ValueError:
        return

    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT * FROM withdrawals WHERE id=?", (wid,)
        ).fetchone()
        if not row or row["status"] != "pending":
            con.execute("ROLLBACK")
            await q.answer("این درخواست قبلاً تعیین تکلیف شده.", show_alert=True)
            return

        if action == "approve":
            con.execute(
                "UPDATE withdrawals SET status='approved' WHERE id=?", (wid,)
            )
            con.execute("COMMIT")
            status_text = "✅ برداشت شما تأیید شد."
        else:
            con.execute(
                "UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,)
            )
            con.execute("COMMIT")
            # Return reserved funds exactly once.
            atomic_balance_change(
                row["user_id"], row["amount"], "withdraw_rejected_return",
                f"withdraw:{wid}:return"
            )
            status_text = "❌ درخواست برداشت شما رد شد و مبلغ به موجودی برگشت."
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()

    try:
        await context.bot.send_message(row["user_id"], status_text)
    except Exception:
        pass
    await q.edit_message_reply_markup(reply_markup=None)
    await q.message.reply_text(
        f"درخواست #{wid} {'تأیید شد' if action == 'approve' else 'رد شد'}."
    )


# =========================
# ADMIN
# =========================
async def admin_callback(q, context):
    uid = q.from_user.id
    if uid not in ADMIN_IDS:
        await q.answer("دسترسی ندارید.", show_alert=True)
        return

    action = q.data.split(":", 1)[1]

    if action == "on":
        context.application.bot_data["enabled"] = True
        await q.message.reply_text("🟢 ربات روشن شد.")
    elif action == "off":
        context.application.bot_data["enabled"] = False
        await q.message.reply_text("🔴 ربات خاموش شد.")
    elif action == "balances":
        con = connect()
        try:
            rows = con.execute(
                "SELECT user_id,name,balance FROM users ORDER BY balance DESC LIMIT 50"
            ).fetchall()
        finally:
            con.close()
        if not rows:
            await q.message.reply_text("کاربری وجود ندارد.")
            return
        lines = ["💰 موجودی کاربران:"]
        for r in rows:
            lines.append(f"{r['user_id']} | {r['name'][:20]} | {money(r['balance'])} TRX")
        await q.message.reply_text("\n".join(lines))
    elif action == "add":
        user_sessions[uid] = {"admin_action": "add"}
        await q.message.reply_text("➕ به شکل زیر ارسال کنید:\nآیدی مبلغ")
    elif action == "sub":
        user_sessions[uid] = {"admin_action": "sub"}
        await q.message.reply_text("➖ به شکل زیر ارسال کنید:\nآیدی مبلغ")
    elif action == "withdrawals":
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT id,user_id,amount,address,status
                FROM withdrawals ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
        finally:
            con.close()
        if not rows:
            await q.message.reply_text("برداشتی ثبت نشده.")
            return
        lines = ["📋 آخرین برداشت‌ها:"]
        for r in rows:
            lines.append(
                f"#{r['id']} | {r['user_id']} | {money(r['amount'])} TRX | {r['status']}"
            )
        await q.message.reply_text("\n".join(lines))


async def admin_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("👑 پنل مدیریت", reply_markup=admin_keyboard())


async def handle_admin_text(update):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        return False
    session = user_sessions.get(uid, {})
    action = session.get("admin_action")
    if action not in ("add", "sub"):
        return False

    m = re.match(r"^\s*(\d+)\s+([0-9]+(?:[.,][0-9]+)?)\s*$", update.message.text or "")
    if not m:
        await update.message.reply_text("❌ فرمت صحیح: آیدی مبلغ")
        return True

    target = int(m.group(1))
    amount = round(float(m.group(2).replace(",", ".")), 6)
    if amount <= 0:
        await update.message.reply_text("❌ مبلغ نامعتبر است.")
        return True

    ensure_user(type("U", (), {"id": target, "full_name": ""})())

    signed = amount if action == "add" else -amount
    ok = atomic_balance_change(
        target, signed, f"admin_{action}",
        f"admin:{uid}:{action}:{target}:{update.message.message_id}"
    )
    if not ok:
        await update.message.reply_text("❌ کسر موجودی ممکن نیست.")
        return True

    await update.message.reply_text(
        f"✅ انجام شد.\nموجودی جدید: {money(get_balance(target))} TRX"
    )
    user_sessions.pop(uid, None)
    return True


# =========================
# MESSAGE ROUTER
# =========================
async def message_router(update, context):
    msg = update.message
    if not msg:
        return

    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    # Group: only balance and transfer are accepted.
    if update.effective_chat.type != ChatType.PRIVATE:
        text = (msg.text or "").strip()
        if text in ("موجودی", "/balance", "balance", "💰 موجودی"):
            await msg.reply_text(f"💰 موجودی: {money(get_balance(user.id))} TRX")
            return
        if text:
            m = TRANSFER_RE.match(text)
            if m and msg.reply_to_message:
                await do_transfer(update, round(float(m.group(1).replace(",", ".")), 6))
            return

    # Private only from here.
    if not await access_check(update, context):
        return

    text = (msg.text or "").strip()

    # Admin actions first.
    if await handle_admin_text(update):
        return

    # Actual Telegram game dice must be handled before ordinary text parsing.
    if msg.dice:
        if context.user_data.get("game_id"):
            await process_game_dice(update, context)
        else:
            await process_friend_dice(update, context)
        return

    # Exact game commands FIRST, before generic amount parsing.
    parsed = parse_game_command(text)
    if parsed:
        game, bet, count = parsed
        if count != 1:
            await msg.reply_text("ℹ️ فعلاً تعداد بازی قابل اجرا در هر نوبت ۱ است.")
            return
        # If a selected mode exists, use it; otherwise show mode buttons.
        mode = context.user_data.pop("game_mode", None)
        if mode in ("bot", "friends"):
            await begin_game(update, context, game, bet, mode)
        else:
            context.user_data["pending_game"] = {"game": game, "bet": bet}
            await msg.reply_text(
                f"{GAME_EMOJI[game]} {GAME_PERSIAN[game]} | شرط {money(bet)} TRX\n"
                "حالت بازی را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👥 بازی با دوستان", callback_data=f"mode:friends")],
                    [InlineKeyboardButton("🤖 بازی با ربات", callback_data=f"mode:bot")],
                ])
            )
        return

    if text in ("💰 موجودی", "موجودی", "/balance", "balance"):
        await msg.reply_text(f"💰 موجودی شما: {money(get_balance(user.id))} TRX")
        return

    if text in ("🎮 بازی", "بازی"):
        await msg.reply_text("🎮 بازی را انتخاب کنید:", reply_markup=game_keyboard())
        return

    if text in ("🤖 بازی با ربات", "بازی با ربات"):
        context.user_data["game_mode"] = "bot"
        await msg.reply_text(
            "🎮 بازی را انتخاب کنید و بعد مبلغ را بنویسید.\nمثال:\n1 تاس 0.1"
        )
        return

    if text in ("👥 بازی با دوستان", "بازی با دوستان"):
        context.user_data["game_mode"] = "friends"
        await msg.reply_text(
            "🎮 بازی را انتخاب کنید و بعد مبلغ را بنویسید.\nمثال:\n1 تاس 0.1"
        )
        return

    if text == "❌ لغو":
        context.user_data.clear()
        await msg.reply_text("❌ لغو شد.", reply_markup=main_keyboard())
        return

    if text in ("🔄 انتقال", "انتقال"):
        await msg.reply_text("🔄 روی پیام کاربر Reply کنید و بنویسید:\nانتقال 0.1")
        return

    tm = TRANSFER_RE.match(text)
    if tm and msg.reply_to_message:
        await do_transfer(update, round(float(tm.group(1).replace(",", ".")), 6))
        return

    if text in ("💸 برداشت", "برداشت"):
        await start_withdraw(update)
        return

    # Withdrawal text has to be before generic unknown handling.
    if user_sessions.get(user.id, {}).get("withdraw_step"):
        await handle_withdraw_text(update, context)
        return

    if text in ("👑 پنل مدیریت", "/admin"):
        if user.id in ADMIN_IDS:
            await admin_command(update, context)
        return

    # Game menu button selection.
    if text in ("🎲 تاس", "🎳 بولینگ", "🎯 دارت", "🏀 بسکتبال"):
        game = GAME_ALIASES[text]
        context.user_data["selected_game"] = game
        await msg.reply_text(
            f"{GAME_EMOJI[game]} برای شروع این قالب را ارسال کنید:\n"
            f"1 {GAME_PERSIAN[game]} 0.1"
        )
        return

    await msg.reply_text("❌ دستور نامعتبر است.")


# Mode callback is kept separate from admin/game callback.
async def mode_callback(update, context):
    q = update.callback_query
    await q.answer()
    if not await is_joined(q.from_user.id, context.bot):
        await q.message.reply_text("❌ ابتدا عضو کانال شوید.")
        return
    if q.from_user.id not in user_sessions:
        pass

    pending = context.user_data.get("pending_game")
    if not pending:
        await q.message.reply_text("❌ بازی منقضی شده است.")
        return
    mode = q.data.split(":", 1)[1]
    if mode not in ("bot", "friends"):
        return
    game, bet = pending["game"], pending["bet"]
    context.user_data.pop("pending_game", None)
    await begin_game_from_callback(q, context, game, bet, mode)


async def begin_game_from_callback(q, context, game, bet, mode):
    user = q.from_user
    if get_balance(user.id) + 1e-9 < bet:
        await q.message.reply_text("❌ موجودی کافی نیست.")
        return

    game_id = secrets.token_hex(8)
    if not await reserve_bet(user.id, bet, game_id):
        await q.message.reply_text("❌ موجودی کافی نیست.")
        return

    if mode == "bot":
        pending_games[game_id] = {
            "mode": "bot", "game": game, "bet": bet,
            "p1": user.id, "stage": "p1"
        }
        context.user_data["game_id"] = game_id
        await q.message.reply_text(
            f"{GAME_EMOJI[game]} بازی با ربات شروع شد.\n"
            f"شرط: {money(bet)} TRX\n"
            f"ابتدا شما {GAME_EMOJI[game]} را بفرستید."
        )
    else:
        pending_games[game_id] = {
            "mode": "friends", "game": game, "bet": bet,
            "p1": user.id, "p2": None, "stage": "waiting"
        }
        await q.message.reply_text(
            f"{GAME_EMOJI[game]} بازی با دوستان ساخته شد.\n"
            f"شرط: {money(bet)} TRX",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 ورود به بازی", callback_data=f"join:{game_id}")],
                [InlineKeyboardButton("❌ لغو", callback_data=f"cancelgame:{game_id}")]
            ])
        )


async def unified_callback(update, context):
    q = update.callback_query
    if q.data.startswith("mode:"):
        await mode_callback(update, context)
    else:
        await callback_handler(update, context)


# =========================
# ERROR HANDLER / MAIN
# =========================
async def error_handler(update, context):
    log.exception("Unhandled error", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("⚠️ خطایی رخ داد؛ دوباره تلاش کنید.")
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["enabled"] = True

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(unified_callback))
    # Dice handler first, then the single text router.
    app.add_handler(MessageHandler(filters.Dice.ALL, message_router))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_router))
    app.add_error_handler(error_handler)

    log.info("BOT STARTED | DB=%s", DB_PATH)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
