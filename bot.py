import os
import re
import logging
import sqlite3
import random
from datetime import datetime
from typing import Dict, Optional, Tuple, List

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, Message
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telegram.constants import ParseMode

# === Configuration ===
OWNER_ID = 8552447077
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set.")

DATABASE_FILE = "bot.db"
DEPOSIT_MIN = 0.5
WITHDRAW_MIN = 2.5

GAME_EMOJIS = {
    "تاس": "🎲",
    "بولینگ": "🎳",
    "دارت": "🎯",
    "بسکتبال": "🏀"
}

# Conversation states
ADMIN_MAIN, ADMIN_GET_USER_ID, ADMIN_GET_AMOUNT, ADMIN_GET_USER_ID_FOR_BLOCK, ADMIN_GET_USER_ID_FOR_UNBLOCK = range(5)
DEPOSIT_AMOUNT, WITHDRAW_AMOUNT = range(6, 8)
OWNER_DEPOSIT_AMOUNT, OWNER_WITHDRAW_AMOUNT = range(8, 10)

# In-memory state
pending_games = {}          # key: (chat_id, message_id) -> game_data
active_games = {}           # key: chat_id -> game_session
bot_busy = False            # flag for bot games

# === Database ===
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0.0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount REAL,
            type TEXT,
            description TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player1_id INTEGER,
            player2_id INTEGER,
            game_type TEXT,
            bet_amount REAL,
            result TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS deposit_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            comment TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            status TEXT DEFAULT 'pending',
            admin_id INTEGER,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
            comment TEXT
        )
    ''')
    conn.commit()
    conn.close()

# === Helper Functions ===
def get_user(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "balance": row[3],
            "is_blocked": row[4],
            "created_at": row[5]
        }
    return None

def create_user(user_id: int, username: str = None, first_name: str = None):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (user_id, username, first_name)
    )
    conn.commit()
    conn.close()

def update_user_balance(user_id: int, delta: float) -> bool:
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return False
    new_balance = row[0] + delta
    if new_balance < 0:
        conn.close()
        return False
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    conn.commit()
    conn.close()
    return True

def add_transaction(sender_id: int, receiver_id: int, amount: float, ttype: str, desc: str = ""):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, type, description) VALUES (?, ?, ?, ?, ?)",
        (sender_id, receiver_id, amount, ttype, desc)
    )
    conn.commit()
    conn.close()

def get_balance(user_id: int) -> float:
    user = get_user(user_id)
    return user["balance"] if user else 0.0

def is_blocked(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user["is_blocked"]) if user else False

def set_block(user_id: int, block: bool):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (1 if block else 0, user_id))
    conn.commit()
    conn.close()

def persian_to_english(s: str) -> str:
    persian_map = {
        '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
        '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9'
    }
    for p, e in persian_map.items():
        s = s.replace(p, e)
    return s

def parse_amount(text: str) -> Optional[float]:
    text = persian_to_english(text)
    match = re.search(r'(\d+(?:\.\d+)?)', text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None

def format_amount(amount: float) -> str:
    return f"{amount:.2f}"

def get_user_name(user_id: int) -> str:
    user = get_user(user_id)
    if user:
        return user["first_name"] or user["username"] or str(user_id)
    return str(user_id)

def get_dice_emoji(game_type: str) -> str:
    return GAME_EMOJIS.get(game_type, "🎲")

# === Game Logic ===
def create_game_session(chat_id: int, creator_id: int, game_type: str, bet_amount: float, mode: str):
    session = {
        "creator": creator_id,
        "game_type": game_type,
        "bet_amount": bet_amount,
        "mode": mode,
        "players": [creator_id],          # for friends, second added later
        "scores": [],
        "current_index": 0,
        "finished": False,
        "winner": None,
        "tie": False,
        "game_message_id": None,
        "roll_message_id": None,
        "player_paid": {creator_id: True} # creator already paid at start
    }
    active_games[chat_id] = session
    return session

def get_game_session(chat_id: int):
    return active_games.get(chat_id)

def end_game_session(chat_id: int):
    if chat_id in active_games:
        del active_games[chat_id]

def record_game_result(player1_id: int, player2_id: int, game_type: str, bet_amount: float, result: str):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO games (player1_id, player2_id, game_type, bet_amount, result) VALUES (?, ?, ?, ?, ?)",
        (player1_id, player2_id, game_type, bet_amount, result)
    )
    conn.commit()
    conn.close()

# === Handlers ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user.id, user.username, user.first_name)
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return

    welcome_text = (
        "🎮 به ربات بازی‌های گروهی خوش آمدید!\n\n"
        "برای شروع بازی در گروه، پیامی به این فرمت ارسال کنید:\n"
        "`1 تاس 0.1`  یا `1 بولینگ 0.1` و ...\n\n"
        "💰 برای مشاهده موجودی: `موجودی` یا `موجودی من`\n"
        "💸 انتقال به دیگران: روی پیام شخص مورد نظر Reply کنید و بنویسید `انتقال 0.1`\n"
        "💳 شارژ و برداشت از طریق منوی زیر انجام می‌شود."
    )
    keyboard = [
        [InlineKeyboardButton("💰 موجودی", callback_data="balance")],
        [InlineKeyboardButton("💳 شارژ", callback_data="deposit")],
        [InlineKeyboardButton("🏧 برداشت", callback_data="withdraw")],
        [InlineKeyboardButton("🎮 بازی‌ها", callback_data="games")],
    ]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return
    bal = get_balance(user.id)
    await update.message.reply_text(f"💰 موجودی شما:\n{format_amount(bal)} TRX")

async def balance_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["موجودی", "موجودی من"]:
        await balance_command(update, context)

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("برای انتقال، روی پیام فرد مورد نظر Reply کنید.")
        return

    receiver = update.message.reply_to_message.from_user
    if not receiver:
        await update.message.reply_text("گیرنده نامشخص.")
        return

    if receiver.id == user.id:
        await update.message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return

    if receiver.is_bot:
        await update.message.reply_text("❌ انتقال به ربات مجاز نیست.")
        return

    text = update.message.text
    amount = parse_amount(text)
    if amount is None or amount <= 0:
        await update.message.reply_text("❌ لطفاً مبلغ معتبری وارد کنید. مثال: `انتقال 0.1`")
        return

    sender_balance = get_balance(user.id)
    if sender_balance < amount:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی شما: {format_amount(sender_balance)} TRX")
        return

    if not update_user_balance(user.id, -amount):
        await update.message.reply_text("❌ خطا در انتقال. موجودی کافی نیست.")
        return
    if not update_user_balance(receiver.id, amount):
        update_user_balance(user.id, amount)
        await update.message.reply_text("❌ خطا در انتقال به گیرنده.")
        return

    add_transaction(user.id, receiver.id, amount, "transfer", f"انتقال به {receiver.full_name}")
    add_transaction(receiver.id, user.id, amount, "transfer", f"دریافت از {user.full_name}")

    await update.message.reply_text(
        f"✅ انتقال موفق!\n"
        f"مبلغ: {format_amount(amount)} TRX\n"
        f"از: {user.full_name}\n"
        f"به: {receiver.full_name}"
    )

# === Game Start Handler ===
async def game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return

    text = update.message.text.strip()
    text = persian_to_english(text)
    pattern = r'^1\s+(تاس|بولینگ|دارت|بسکتبال)\s+(\d+(?:\.\d+)?)$'
    match = re.match(pattern, text)
    if not match:
        return

    game_type = match.group(1)
    amount = float(match.group(2))
    if amount <= 0:
        await update.message.reply_text("❌ مبلغ باید مثبت باشد.")
        return

    bal = get_balance(user.id)
    if bal < amount:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی شما: {format_amount(bal)} TRX")
        return

    # Deduct bet from creator now
    if not update_user_balance(user.id, -amount):
        await update.message.reply_text("❌ خطا در کسر موجودی.")
        return
    add_transaction(user.id, None, amount, "game", f"شرط بازی {game_type}")

    keyboard = [
        [InlineKeyboardButton("👥 بازی با دوستان", callback_data="game_friends")],
        [InlineKeyboardButton("🤖 بازی با ربات", callback_data="game_bot")],
        [InlineKeyboardButton("❌ لغو", callback_data="game_cancel")],
    ]
    msg = await update.message.reply_text(
        f"🎮 بازی {game_type} با مبلغ {format_amount(amount)} TRX\n"
        "حالت بازی را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    pending_games[(update.effective_chat.id, msg.message_id)] = {
        "creator": user.id,
        "game_type": game_type,
        "bet_amount": amount,
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
    }

async def game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_blocked(user.id):
        await query.edit_message_text("شما مسدود شده‌اید.")
        return

    data = query.data
    msg_id = query.message.message_id
    key = (chat_id, msg_id)
    if key not in pending_games:
        await query.edit_message_text("⏳ این بازی منقضی شده است. لطفاً دوباره درخواست دهید.")
        return

    game_data = pending_games.pop(key)
    if game_data["creator"] != user.id:
        await query.answer("❌ فقط سازنده بازی می‌تواند حالت را انتخاب کند.", show_alert=True)
        return

    if data == "game_cancel":
        # Refund bet
        update_user_balance(user.id, game_data["bet_amount"])
        add_transaction(None, user.id, game_data["bet_amount"], "game", "بازگشت شرط به دلیل لغو")
        await query.edit_message_text("❌ بازی لغو شد.")
        return

    if data == "game_bot":
        global bot_busy
        if bot_busy:
            await query.edit_message_text("🤖 ربات در حال بازی هست. لطفاً چند لحظه دیگر تلاش کنید.")
            return

        game_type = game_data["game_type"]
        bet_amount = game_data["bet_amount"]
        creator_id = user.id

        # Create session
        session = create_game_session(chat_id, creator_id, game_type, bet_amount, "bot")
        session["players"].append(None)  # placeholder for bot
        session["player_paid"][0] = True  # creator already paid

        bot_busy = True
        await query.edit_message_text(
            f"🤖 بازی با ربات شروع شد.\n"
            f"نوبت شماست، دکمه را بزنید تا پرتاب کنید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
            ])
        )

    elif data == "game_friends":
        game_type = game_data["game_type"]
        bet_amount = game_data["bet_amount"]
        creator_id = user.id

        session = create_game_session(chat_id, creator_id, game_type, bet_amount, "friends")
        session["player_paid"][creator_id] = True  # creator already paid

        await query.edit_message_text(
            f"👥 بازی با دوستان شروع شد.\n"
            f"نوبت شماست، دکمه را بزنید تا پرتاب کنید.\n"
            f"(بازیکن دوم بعد از شما خواهد آمد)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
            ])
        )

# === Roll Callback ===
async def roll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    chat_id = update.effective_chat.id

    if is_blocked(user.id):
        await query.edit_message_text("شما مسدود شده‌اید.")
        return

    session = active_games.get(chat_id)
    if not session:
        await query.edit_message_text("⏳ هیچ بازی فعالی یافت نشد.")
        return

    if session["finished"]:
        await query.edit_message_text("این بازی به پایان رسیده است.")
        return

    current_index = session["current_index"]
    players = session["players"]
    mode = session["mode"]

    # If friends mode and only one player, the second player is joining now
    if mode == "friends" and len(players) == 1 and user.id != players[0]:
        # Add second player
        players.append(user.id)
        session["player_paid"][user.id] = False  # not paid yet
        # Deduct bet from second player
        if not update_user_balance(user.id, -session["bet_amount"]):
            await query.edit_message_text("❌ موجودی کافی برای شرکت در بازی ندارید.")
            # Remove player
            players.pop()
            return
        session["player_paid"][user.id] = True
        add_transaction(user.id, None, session["bet_amount"], "game", f"شرط بازی {session['game_type']} با دوستان")
        # Now current_index should be 1 (second player's turn) if creator already rolled? Actually after creator rolls, we increment to 1, so it's fine.
        # We'll set current_index to 1 if not already
        if session["current_index"] == 0:
            session["current_index"] = 1
        # Notify
        await query.edit_message_text(
            f"👤 {get_user_name(user.id)} به بازی پیوست!\n"
            f"نوبت شماست، پرتاب کنید.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
            ])
        )
        return

    # Check if it's this user's turn
    if mode == "friends":
        if len(players) < 2:
            # If second hasn't joined, we already handled above
            await query.answer("⏳ منتظر پیوستن بازیکن دوم هستیم...", show_alert=True)
            return
        if players[current_index] != user.id:
            await query.answer("❌ نوبت شما نیست.", show_alert=True)
            return
        # Ensure user has paid (should be, but check)
        if not session["player_paid"].get(user.id, False):
            # Deduct now
            if not update_user_balance(user.id, -session["bet_amount"]):
                await query.edit_message_text("❌ موجودی کافی نیست.")
                return
            session["player_paid"][user.id] = True
            add_transaction(user.id, None, session["bet_amount"], "game", f"شرط بازی {session['game_type']} با دوستان")

    elif mode == "bot":
        if current_index == 0 and players[0] != user.id:
            await query.answer("❌ نوبت شما نیست.", show_alert=True)
            return
        # Bot turn is handled separately

    # Send dice
    game_type = session["game_type"]
    emoji = get_dice_emoji(game_type)
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    value = dice_msg.dice.value

    # Record score
    if mode == "bot":
        if current_index == 0:  # creator
            session["scores"].append(value)
            session["current_index"] = 1
            await query.edit_message_text(
                f"🎲 شما پرتاب کردید: {value}\n"
                f"نوبت ربات است...",
                reply_markup=None
            )
            # Bot rolls after delay
            context.job_queue.run_once(bot_roll, 1.5, context=chat_id)
        else:
            # Bot roll should not happen here
            pass
    else:  # friends
        session["scores"].append(value)
        # Check if all players have rolled (len(scores) == len(players))
        if len(session["scores"]) == len(players):
            # Both have rolled, compare
            await finish_game(chat_id, context)
        else:
            # Move to next player (should be second)
            session["current_index"] += 1
            next_player_id = players[session["current_index"]]
            next_player_name = get_user_name(next_player_id)
            await query.edit_message_text(
                f"🎲 {get_user_name(user.id)} پرتاب کرد: {value}\n"
                f"نوبت {next_player_name} است.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
                ])
            )

async def bot_roll(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    session = active_games.get(chat_id)
    if not session or session["finished"]:
        global bot_busy
        bot_busy = False
        return

    # Bot rolls
    game_type = session["game_type"]
    emoji = get_dice_emoji(game_type)
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    value = dice_msg.dice.value
    session["scores"].append(value)

    # Compare scores
    await finish_game(chat_id, context)

async def finish_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = active_games.get(chat_id)
    if not session or session["finished"]:
        return

    scores = session["scores"]
    players = session["players"]
    game_type = session["game_type"]
    bet_amount = session["bet_amount"]
    mode = session["mode"]

    if len(scores) != len(players):
        return

    player1_score = scores[0]
    player2_score = scores[1] if len(scores) > 1 else 0

    result_text = ""
    winner_id = None
    if player1_score > player2_score:
        winner_id = players[0]
        result_text = f"🎉 {get_user_name(players[0])} برنده شد!"
    elif player1_score < player2_score:
        if mode == "bot":
            result_text = "🤖 ربات برنده شد!"
        else:
            winner_id = players[1]
            result_text = f"🎉 {get_user_name(players[1])} برنده شد!"
    else:
        # Tie
        if mode == "bot":
            result_text = "🤝 مساوی شد! دوباره بازی کنید."
            # Refund bet to creator (already deducted)
            update_user_balance(players[0], bet_amount)
            add_transaction(None, players[0], bet_amount, "game", f"بازگشت شرط به دلیل مساوی در {game_type} با ربات")
            # Reset scores and play again
            session["scores"] = []
            session["current_index"] = 0
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{result_text}\nپرتاب مجدد... نوبت شماست.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
                ])
            )
            return
        else:  # friends
            result_text = "🤝 مساوی شد! دوباره بازی کنید."
            # Refund both players (already deducted)
            for pid in players:
                update_user_balance(pid, bet_amount)
                add_transaction(None, pid, bet_amount, "game", f"بازگشت شرط به دلیل مساوی در {game_type} با دوستان")
            # Reset and play again
            session["scores"] = []
            session["current_index"] = 0
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{result_text}\nپرتاب مجدد... نوبت {get_user_name(players[0])} است.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎲 پرتاب", callback_data="roll")]
                ])
            )
            return

    session["finished"] = True
    # Give prize to winner if any
    if winner_id:
        # Total prize = bet_amount * 2 (since both paid)
        prize = bet_amount * 2
        update_user_balance(winner_id, prize)
        add_transaction(None, winner_id, prize, "game", f"برد در بازی {game_type}")

    # Record game result
    if mode == "bot":
        record_game_result(players[0], 0, game_type, bet_amount, "bot_win" if winner_id else "bot_lose")
    else:
        if winner_id:
            record_game_result(players[0], players[1], game_type, bet_amount, f"winner_{winner_id}")
        else:
            record_game_result(players[0], players[1], game_type, bet_amount, "tie")

    await context.bot.send_message(chat_id=chat_id, text=result_text)
    end_game_session(chat_id)
    if mode == "bot":
        global bot_busy
        bot_busy = False

# === Admin Panel ===
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ شما دسترسی به این بخش ندارید.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 آمار", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 کاربران", callback_data="admin_users")],
        [InlineKeyboardButton("💰 موجودی کاربر", callback_data="admin_balance")],
        [InlineKeyboardButton("➕ افزایش موجودی", callback_data="admin_add")],
        [InlineKeyboardButton("➖ کاهش موجودی", callback_data="admin_sub")],
        [InlineKeyboardButton("📋 تاریخچه تراکنش‌ها", callback_data="admin_transactions")],
        [InlineKeyboardButton("🎮 آمار بازی‌ها", callback_data="admin_games")],
        [InlineKeyboardButton("🚫 مسدود کردن کاربر", callback_data="admin_block")],
        [InlineKeyboardButton("✅ رفع مسدودی", callback_data="admin_unblock")],
        [InlineKeyboardButton("❌ بستن", callback_data="admin_close")],
    ]
    await update.message.reply_text("👨‍💼 پنل مدیریت:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.edit_message_text("⛔ شما دسترسی ندارید.")
        return

    data = query.data
    if data == "admin_close":
        await query.edit_message_text("پنل مدیریت بسته شد.")
        return

    if data == "admin_stats":
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM games")
        total_games = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM transactions WHERE type='deposit'")
        total_deposits = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM transactions WHERE type='withdraw'")
        total_withdraws = c.fetchone()[0] or 0
        conn.close()
        text = (
            f"📊 آمار کلی:\n"
            f"👥 کاربران: {total_users}\n"
            f"🎮 بازی‌ها: {total_games}\n"
            f"💰 مجموع واریزها: {format_amount(total_deposits)} TRX\n"
            f"🏧 مجموع برداشت‌ها: {format_amount(total_withdraws)} TRX"
        )
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]))

    elif data == "admin_users":
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, balance, is_blocked FROM users LIMIT 10")
        rows = c.fetchall()
        conn.close()
        if not rows:
            text = "هیچ کاربری یافت نشد."
        else:
            text = "👥 کاربران (نمایش ۱۰ تای اول):\n"
            for row in rows:
                user_id, username, first_name, balance, blocked = row
                name = first_name or username or str(user_id)
                status = "🚫" if blocked else "✅"
                text += f"{status} {name} (ID: {user_id}) - {format_amount(balance)} TRX\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]))

    elif data == "admin_balance":
        context.user_data["admin_action"] = "balance"
        await query.edit_message_text("لطفاً USER_ID کاربر را وارد کنید:")
        return ADMIN_GET_USER_ID

    elif data == "admin_add":
        context.user_data["admin_action"] = "add"
        await query.edit_message_text("لطفاً USER_ID کاربر را وارد کنید:")
        return ADMIN_GET_USER_ID

    elif data == "admin_sub":
        context.user_data["admin_action"] = "sub"
        await query.edit_message_text("لطفاً USER_ID کاربر را وارد کنید:")
        return ADMIN_GET_USER_ID

    elif data == "admin_transactions":
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT id, sender_id, receiver_id, amount, type, description, timestamp FROM transactions ORDER BY id DESC LIMIT 10")
        rows = c.fetchall()
        conn.close()
        if not rows:
            text = "هیچ تراکنشی یافت نشد."
        else:
            text = "📋 آخرین تراکنش‌ها:\n"
            for row in rows:
                tid, sid, rid, amt, ttype, desc, ts = row
                text += f"#{tid} {ttype}: {format_amount(amt)} TRX - {desc} ({ts})\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]))

    elif data == "admin_games":
        conn = sqlite3.connect(DATABASE_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM games")
        total = c.fetchone()[0]
        c.execute("SELECT game_type, COUNT(*) FROM games GROUP BY game_type")
        by_type = c.fetchall()
        c.execute("SELECT result, COUNT(*) FROM games GROUP BY result")
        by_result = c.fetchall()
        conn.close()
        text = f"🎮 آمار بازی‌ها:\nکل بازی‌ها: {total}\n"
        for typ, cnt in by_type:
            text += f"{typ}: {cnt}\n"
        text += "\nنتایج:\n"
        for res, cnt in by_result:
            text += f"{res}: {cnt}\n"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]]))

    elif data == "admin_block":
        context.user_data["admin_action"] = "block"
        await query.edit_message_text("لطفاً USER_ID کاربر مورد نظر برای مسدودسازی را وارد کنید:")
        return ADMIN_GET_USER_ID_FOR_BLOCK

    elif data == "admin_unblock":
        context.user_data["admin_action"] = "unblock"
        await query.edit_message_text("لطفاً USER_ID کاربر مورد نظر برای رفع مسدودی را وارد کنید:")
        return ADMIN_GET_USER_ID_FOR_UNBLOCK

    elif data == "admin_back":
        keyboard = [
            [InlineKeyboardButton("📊 آمار", callback_data="admin_stats")],
            [InlineKeyboardButton("👥 کاربران", callback_data="admin_users")],
            [InlineKeyboardButton("💰 موجودی کاربر", callback_data="admin_balance")],
            [InlineKeyboardButton("➕ افزایش موجودی", callback_data="admin_add")],
            [InlineKeyboardButton("➖ کاهش موجودی", callback_data="admin_sub")],
            [InlineKeyboardButton("📋 تاریخچه تراکنش‌ها", callback_data="admin_transactions")],
            [InlineKeyboardButton("🎮 آمار بازی‌ها", callback_data="admin_games")],
            [InlineKeyboardButton("🚫 مسدود کردن کاربر", callback_data="admin_block")],
            [InlineKeyboardButton("✅ رفع مسدودی", callback_data="admin_unblock")],
            [InlineKeyboardButton("❌ بستن", callback_data="admin_close")],
        ]
        await query.edit_message_text("👨‍💼 پنل مدیریت:", reply_markup=InlineKeyboardMarkup(keyboard))

    return ConversationHandler.END

# Admin conversation handlers
async def admin_get_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END

    text = update.message.text.strip()
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ USER_ID باید عددی باشد. دوباره وارد کنید.")
        return ADMIN_GET_USER_ID

    action = context.user_data.get("admin_action")
    if action == "balance":
        bal = get_balance(target_id)
        if bal is None:
            await update.message.reply_text("❌ کاربری با این ID یافت نشد.")
        else:
            user_obj = get_user(target_id)
            name = user_obj["first_name"] or user_obj["username"] or str(target_id)
            await update.message.reply_text(f"💰 موجودی {name}: {format_amount(bal)} TRX")
        await admin_command(update, context)
        return ConversationHandler.END
    elif action in ["add", "sub"]:
        context.user_data["target_user_id"] = target_id
        await update.message.reply_text("لطفاً مبلغ مورد نظر را وارد کنید (به عدد):")
        return ADMIN_GET_AMOUNT
    else:
        await update.message.reply_text("خطا در تشخیص عملیات.")
        return ConversationHandler.END

async def admin_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END

    text = update.message.text.strip()
    amount = parse_amount(text)
    if amount is None or amount <= 0:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        return ADMIN_GET_AMOUNT

    target_id = context.user_data.get("target_user_id")
    if not target_id:
        await update.message.reply_text("❌ خطا: کاربر مشخص نشد.")
        return ConversationHandler.END

    action = context.user_data.get("admin_action")
    if action == "add":
        if update_user_balance(target_id, amount):
            add_transaction(None, target_id, amount, "admin_add", "افزایش موجودی توسط ادمین")
            await update.message.reply_text(f"✅ {format_amount(amount)} TRX به موجودی کاربر اضافه شد.")
        else:
            await update.message.reply_text("❌ خطا در افزایش موجودی.")
    elif action == "sub":
        bal = get_balance(target_id)
        if bal < amount:
            await update.message.reply_text(f"❌ موجودی کاربر کافی نیست. موجودی: {format_amount(bal)} TRX")
            return ConversationHandler.END
        if update_user_balance(target_id, -amount):
            add_transaction(target_id, None, amount, "admin_sub", "کاهش موجودی توسط ادمین")
            await update.message.reply_text(f"✅ {format_amount(amount)} TRX از موجودی کاربر کسر شد.")
        else:
            await update.message.reply_text("❌ خطا در کاهش موجودی.")
    else:
        await update.message.reply_text("خطا.")

    await admin_command(update, context)
    return ConversationHandler.END

async def admin_get_user_id_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END

    text = update.message.text.strip()
    try:
        target_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ USER_ID باید عددی باشد. دوباره وارد کنید.")
        return ADMIN_GET_USER_ID_FOR_BLOCK

    action = context.user_data.get("admin_action")
    if action == "block":
        if get_user(target_id) is None:
            await update.message.reply_text("❌ کاربری با این ID یافت نشد.")
        else:
            set_block(target_id, True)
            await update.message.reply_text(f"✅ کاربر {target_id} مسدود شد.")
    elif action == "unblock":
        if get_user(target_id) is None:
            await update.message.reply_text("❌ کاربری با این ID یافت نشد.")
        else:
            set_block(target_id, False)
            await update.message.reply_text(f"✅ کاربر {target_id} رفع مسدودی شد.")
    else:
        await update.message.reply_text("خطا.")

    await admin_command(update, context)
    return ConversationHandler.END

# === Deposit and Withdraw ===
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return

    keyboard = [
        [InlineKeyboardButton("0.5", callback_data="dep_0.5"), InlineKeyboardButton("1", callback_data="dep_1")],
        [InlineKeyboardButton("2", callback_data="dep_2"), InlineKeyboardButton("5", callback_data="dep_5")],
        [InlineKeyboardButton("10", callback_data="dep_10"), InlineKeyboardButton("مبلغ دلخواه", callback_data="dep_custom")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="dep_cancel")],
    ]
    await update.message.reply_text(
        f"💳 لطفاً مبلغ شارژ را انتخاب کنید (حداقل {DEPOSIT_MIN} TRX):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    if data == "dep_cancel":
        await query.edit_message_text("❌ شارژ لغو شد.")
        return

    if data.startswith("dep_"):
        amount_str = data[4:]
        if amount_str == "custom":
            await query.edit_message_text("لطفاً مبلغ مورد نظر را به عدد وارد کنید (حداقل 0.5):")
            return DEPOSIT_AMOUNT
        else:
            try:
                amount = float(amount_str)
            except ValueError:
                await query.edit_message_text("❌ مبلغ نامعتبر.")
                return
            if amount < DEPOSIT_MIN:
                await query.edit_message_text(f"❌ حداقل مبلغ شارژ {DEPOSIT_MIN} TRX است.")
                return
            return await create_deposit_request(update, context, user.id, amount)

async def deposit_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    amount = parse_amount(text)
    if amount is None or amount < DEPOSIT_MIN:
        await update.message.reply_text(f"❌ لطفاً یک عدد معتبر حداقل {DEPOSIT_MIN} وارد کنید.")
        return DEPOSIT_AMOUNT
    return await create_deposit_request(update, context, user.id, amount)

async def create_deposit_request(update, context, user_id, amount):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO deposit_requests (user_id, amount, status) VALUES (?, ?, 'pending')",
        (user_id, amount)
    )
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("✅ تأیید", callback_data=f"dep_confirm_{req_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"dep_reject_{req_id}")]
    ]
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"📩 درخواست شارژ جدید:\n"
             f"کاربر: {user_id}\n"
             f"مبلغ: {format_amount(amount)} TRX\n"
             f"شماره درخواست: {req_id}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await update.effective_message.reply_text("✅ درخواست شارژ شما به ادمین ارسال شد. پس از تأیید، موجودی شما افزایش می‌یابد.")
    return ConversationHandler.END

async def deposit_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.answer("⛔ شما دسترسی ندارید.", show_alert=True)
        return

    data = query.data
    parts = data.split('_')
    action = parts[1]
    req_id = int(parts[2])

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, amount, status FROM deposit_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("❌ درخواست یافت نشد.")
        conn.close()
        return
    user_id, amount, status = row
    if status != "pending":
        await query.edit_message_text(f"❌ این درخواست قبلاً {status} شده است.")
        conn.close()
        return

    if action == "confirm":
        context.user_data["deposit_req_id"] = req_id
        await query.edit_message_text(f"لطفاً مبلغ نهایی شارژ را وارد کنید (درخواست: {format_amount(amount)} TRX):")
        return OWNER_DEPOSIT_AMOUNT
    elif action == "reject":
        c.execute("UPDATE deposit_requests SET status = 'rejected', admin_id = ? WHERE id = ?", (OWNER_ID, req_id))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ درخواست شارژ {req_id} رد شد.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"❌ درخواست شارژ شما رد شد.")
        except:
            pass
        return ConversationHandler.END

async def deposit_owner_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END

    text = update.message.text.strip()
    amount = parse_amount(text)
    if amount is None or amount <= 0:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        return OWNER_DEPOSIT_AMOUNT

    req_id = context.user_data.get("deposit_req_id")
    if not req_id:
        await update.message.reply_text("❌ خطا: شناسه درخواست یافت نشد.")
        return ConversationHandler.END

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, status FROM deposit_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("❌ درخواست یافت نشد.")
        conn.close()
        return ConversationHandler.END
    user_id, status = row
    if status != "pending":
        await update.message.reply_text(f"❌ درخواست قبلاً {status} شده است.")
        conn.close()
        return ConversationHandler.END

    c.execute("UPDATE deposit_requests SET status = 'approved', admin_id = ?, comment = ? WHERE id = ?",
              (OWNER_ID, f"مبلغ {format_amount(amount)}", req_id))
    conn.commit()
    conn.close()

    if update_user_balance(user_id, amount):
        add_transaction(None, user_id, amount, "deposit", f"شارژ داخلی به مبلغ {format_amount(amount)}")
        await update.message.reply_text(f"✅ شارژ {format_amount(amount)} TRX برای کاربر {user_id} انجام شد.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ درخواست شارژ شما تأیید شد.\nمبلغ: {format_amount(amount)} TRX به موجودی شما اضافه شد."
            )
        except:
            pass
    else:
        await update.message.reply_text("❌ خطا در افزایش موجودی.")

    keyboard = [[InlineKeyboardButton("📩 پاسخ به کاربر", callback_data=f"dep_reply_{req_id}")]]
    await update.message.reply_text("برای ارسال پیام به کاربر، دکمه زیر را بزنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def deposit_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    data = query.data
    req_id = int(data.split('_')[2])
    context.user_data["reply_to"] = ("deposit", req_id)
    await query.edit_message_text("لطفاً پیام خود را برای کاربر ارسال کنید:")

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("شما مسدود شده‌اید.")
        return

    keyboard = [
        [InlineKeyboardButton("2.5", callback_data="with_2.5"), InlineKeyboardButton("5", callback_data="with_5")],
        [InlineKeyboardButton("10", callback_data="with_10"), InlineKeyboardButton("مبلغ دلخواه", callback_data="with_custom")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="with_cancel")],
    ]
    await update.message.reply_text(
        f"🏧 لطفاً مبلغ برداشت را انتخاب کنید (حداقل {WITHDRAW_MIN} TRX):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data

    if data == "with_cancel":
        await query.edit_message_text("❌ برداشت لغو شد.")
        return

    if data.startswith("with_"):
        amount_str = data[5:]
        if amount_str == "custom":
            await query.edit_message_text("لطفاً مبلغ مورد نظر را به عدد وارد کنید (حداقل 2.5):")
            return WITHDRAW_AMOUNT
        else:
            try:
                amount = float(amount_str)
            except ValueError:
                await query.edit_message_text("❌ مبلغ نامعتبر.")
                return
            if amount < WITHDRAW_MIN:
                await query.edit_message_text(f"❌ حداقل مبلغ برداشت {WITHDRAW_MIN} TRX است.")
                return
            if get_balance(user.id) < amount:
                await query.edit_message_text(f"❌ موجودی کافی نیست. موجودی شما: {format_amount(get_balance(user.id))} TRX")
                return
            return await create_withdraw_request(update, context, user.id, amount)

async def withdraw_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()
    amount = parse_amount(text)
    if amount is None or amount < WITHDRAW_MIN:
        await update.message.reply_text(f"❌ لطفاً یک عدد معتبر حداقل {WITHDRAW_MIN} وارد کنید.")
        return WITHDRAW_AMOUNT
    if get_balance(user.id) < amount:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی شما: {format_amount(get_balance(user.id))} TRX")
        return WITHDRAW_AMOUNT
    return await create_withdraw_request(update, context, user.id, amount)

async def create_withdraw_request(update, context, user_id, amount):
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO withdraw_requests (user_id, amount, status) VALUES (?, ?, 'pending')",
        (user_id, amount)
    )
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    keyboard = [
        [InlineKeyboardButton("✅ تأیید", callback_data=f"with_confirm_{req_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"with_reject_{req_id}")]
    ]
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=f"📩 درخواست برداشت جدید:\n"
             f"کاربر: {user_id}\n"
             f"مبلغ: {format_amount(amount)} TRX\n"
             f"شماره درخواست: {req_id}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await update.effective_message.reply_text("✅ درخواست برداشت شما به ادمین ارسال شد. پس از تأیید، مبلغ از موجودی شما کسر می‌شود.")
    return ConversationHandler.END

async def withdraw_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.answer("⛔ شما دسترسی ندارید.", show_alert=True)
        return

    data = query.data
    parts = data.split('_')
    action = parts[1]
    req_id = int(parts[2])

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, amount, status FROM withdraw_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("❌ درخواست یافت نشد.")
        conn.close()
        return
    user_id, amount, status = row
    if status != "pending":
        await query.edit_message_text(f"❌ این درخواست قبلاً {status} شده است.")
        conn.close()
        return

    if action == "confirm":
        context.user_data["withdraw_req_id"] = req_id
        await query.edit_message_text(f"لطفاً مبلغ نهایی برداشت را وارد کنید (درخواست: {format_amount(amount)} TRX):")
        return OWNER_WITHDRAW_AMOUNT
    elif action == "reject":
        c.execute("UPDATE withdraw_requests SET status = 'rejected', admin_id = ? WHERE id = ?", (OWNER_ID, req_id))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ درخواست برداشت {req_id} رد شد.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"❌ درخواست برداشت شما رد شد.")
        except:
            pass
        return ConversationHandler.END

async def withdraw_owner_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END

    text = update.message.text.strip()
    amount = parse_amount(text)
    if amount is None or amount <= 0:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        return OWNER_WITHDRAW_AMOUNT

    req_id = context.user_data.get("withdraw_req_id")
    if not req_id:
        await update.message.reply_text("❌ خطا: شناسه درخواست یافت نشد.")
        return ConversationHandler.END

    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, status FROM withdraw_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("❌ درخواست یافت نشد.")
        conn.close()
        return
    user_id, status = row
    if status != "pending":
        await update.message.reply_text(f"❌ درخواست قبلاً {status} شده است.")
        conn.close()
        return

    if get_balance(user_id) < amount:
        await update.message.reply_text(f"❌ موجودی کاربر کافی نیست. موجودی: {format_amount(get_balance(user_id))} TRX")
        return

    c.execute("UPDATE withdraw_requests SET status = 'approved', admin_id = ?, comment = ? WHERE id = ?",
              (OWNER_ID, f"مبلغ {format_amount(amount)}", req_id))
    conn.commit()
    conn.close()

    if update_user_balance(user_id, -amount):
        add_transaction(user_id, None, amount, "withdraw", f"برداشت داخلی به مبلغ {format_amount(amount)}")
        await update.message.reply_text(f"✅ برداشت {format_amount(amount)} TRX از کاربر {user_id} انجام شد.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ درخواست برداشت شما تأیید شد.\nمبلغ: {format_amount(amount)} TRX از موجودی شما کسر شد."
            )
        except:
            pass
    else:
        await update.message.reply_text("❌ خطا در کاهش موجودی.")

    keyboard = [[InlineKeyboardButton("📩 پاسخ به کاربر", callback_data=f"with_reply_{req_id}")]]
    await update.message.reply_text("برای ارسال پیام به کاربر، دکمه زیر را بزنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def withdraw_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if user.id != OWNER_ID:
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    data = query.data
    req_id = int(data.split('_')[2])
    context.user_data["reply_to"] = ("withdraw", req_id)
    await query.edit_message_text("لطفاً پیام خود را برای کاربر ارسال کنید:")

async def owner_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        return

    reply_info = context.user_data.get("reply_to")
    if not reply_info:
        return

    req_type, req_id = reply_info
    conn = sqlite3.connect(DATABASE_FILE)
    c = conn.cursor()
    if req_type == "deposit":
        c.execute("SELECT user_id FROM deposit_requests WHERE id = ?", (req_id,))
    else:
        c.execute("SELECT user_id FROM withdraw_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("❌ کاربر یافت نشد.")
        return

    target_user_id = row[0]
    text = update.message.text
    try:
        await context.bot.send_message(chat_id=target_user_id, text=f"📩 پیام از ادمین:\n{text}")
        await update.message.reply_text("✅ پیام ارسال شد.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ارسال پیام: {e}")
    context.user_data.pop("reply_to", None)

# === Main Menu Callbacks ===
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "balance":
        bal = get_balance(update.effective_user.id)
        await query.edit_message_text(f"💰 موجودی شما:\n{format_amount(bal)} TRX")
    elif data == "deposit":
        await deposit_start(update, context)
    elif data == "withdraw":
        await withdraw_start(update, context)
    elif data == "games":
        await query.edit_message_text("برای شروع بازی، در گروه پیام `1 تاس 0.1` ارسال کنید.")
    else:
        await query.edit_message_text("گزینه نامعتبر.")

# === Error Handler ===
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")
    # If error happens, release bot_busy if needed
    if context.error and "chat_id" in context.error.__dict__:
        chat_id = context.error.chat_id
        if chat_id in active_games:
            session = active_games.get(chat_id)
            if session and session["mode"] == "bot":
                global bot_busy
                bot_busy = False
                end_game_session(chat_id)

# === Main ===
def main():
    init_db()
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("deposit", deposit_start))
    app.add_handler(CommandHandler("withdraw", withdraw_start))

    # Message handlers
    app.add_handler(MessageHandler(filters.Regex(r'^(موجودی|موجودی من)$'), balance_text))
    app.add_handler(MessageHandler(filters.Regex(r'^انتقال\s+'), transfer))
    app.add_handler(MessageHandler(filters.Regex(r'^1\s+(تاس|بولینگ|دارت|بسکتبال)\s+\d+(?:\.\d+)?$'), game_start))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(game_callback, pattern="^game_"))
    app.add_handler(CallbackQueryHandler(roll_callback, pattern="^roll$"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^(balance|deposit|withdraw|games)$"))

    # Admin conversation
    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_callback, pattern="^admin_"),
        ],
        states={
            ADMIN_GET_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_user_id)],
            ADMIN_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_amount)],
            ADMIN_GET_USER_ID_FOR_BLOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_user_id_block)],
            ADMIN_GET_USER_ID_FOR_UNBLOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_get_user_id_block)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(admin_conv)

    # Deposit conversation
    deposit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(deposit_callback, pattern="^dep_"),
        ],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_input)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
    )
    app.add_handler(deposit_conv)

    # Withdraw conversation
    withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(withdraw_callback, pattern="^with_"),
        ],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_input)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
    )
    app.add_handler(withdraw_conv)

    # Owner confirm conversations
    owner_deposit_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(deposit_confirm_callback, pattern="^dep_(confirm|reject)_"),
        ],
        states={
            OWNER_DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_owner_amount)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
    )
    app.add_handler(owner_deposit_conv)

    owner_withdraw_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(withdraw_confirm_callback, pattern="^with_(confirm|reject)_"),
        ],
        states={
            OWNER_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_owner_amount)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
    )
    app.add_handler(owner_withdraw_conv)

    # Reply to user from owner
    app.add_handler(CallbackQueryHandler(deposit_reply, pattern="^dep_reply_"))
    app.add_handler(CallbackQueryHandler(withdraw_reply, pattern="^with_reply_"))
    app.add_handler(MessageHandler(filters.TEXT & filters.Chat(OWNER_ID), owner_reply_message))

    # Error handler
    app.add_error_handler(error_handler)

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
