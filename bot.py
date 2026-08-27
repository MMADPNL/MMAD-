import os
import re
import logging
import sqlite3
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# ===== CONFIG =====
OWNER_ID = 8552447077
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set.")

DATABASE = "bot.db"
DEPOSIT_MIN = 0.5
WITHDRAW_MIN = 2.5
REFERRAL_BONUS = 0.05
GAME_BET = 0.1
GAME_WIN = 0.18
GAME_FEE = 0.02

TON_ADDRESS = "UQCfIahBY06klJFYNyeAcwOxpNKq78yQOSMMHHe4QDZbziwC"
BANK_CARD = "6219861856357990"
BANK_OWNER = "محمد امین"

GAME_EMOJIS = {"تاس": "🎲", "بولینگ": "🎳", "دارت": "🎯", "بسکتبال": "🏀"}

# Conversation states
DEPOSIT_AMOUNT, DEPOSIT_METHOD, DEPOSIT_PROOF = range(1, 4)
WITHDRAW_AMOUNT, WITHDRAW_METHOD, WITHDRAW_PROOF = range(4, 7)
OWNER_DEPOSIT_AMOUNT, OWNER_WITHDRAW_AMOUNT = range(7, 9)
ADMIN_GET_USER_ID, ADMIN_GET_AMOUNT = range(9, 11)
ADMIN_GET_USER_ID_FOR_BLOCK, ADMIN_GET_USER_ID_FOR_UNBLOCK = range(11, 13)

# In-memory
pending_games = {}
active_games = {}
bot_busy = False
bot_game_chat_id = None

# ===== DATABASE =====
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL DEFAULT 0,
        referrer_id INTEGER,
        is_blocked INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        bonus REAL,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER,
        amount REAL,
        type TEXT,
        description TEXT,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player1_id INTEGER,
        player2_id INTEGER,
        game_type TEXT,
        bet_amount REAL,
        win_amount REAL,
        fee_amount REAL,
        result TEXT,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS deposit_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        method TEXT,
        proof TEXT,
        status TEXT DEFAULT 'pending',
        admin_id INTEGER,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraw_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        method TEXT,
        account_info TEXT,
        status TEXT DEFAULT 'pending',
        admin_id INTEGER,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

# ===== HELPERS =====
def get_user(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "user_id": row[0], "username": row[1], "first_name": row[2],
            "balance": row[3], "referrer_id": row[4], "is_blocked": row[5],
            "created_at": row[6]
        }
    return None

def create_user(user_id: int, username=None, first_name=None, referrer_id=None):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if c.fetchone():
        conn.close()
        return
    c.execute(
        "INSERT INTO users (user_id, username, first_name, referrer_id) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, referrer_id)
    )
    if referrer_id and referrer_id != user_id:
        if update_balance(referrer_id, REFERRAL_BONUS):
            add_transaction(None, referrer_id, REFERRAL_BONUS, "referral", f"پاداش معرفی {user_id}")
            c.execute("INSERT INTO referrals (referrer_id, referred_id, bonus) VALUES (?, ?, ?)",
                      (referrer_id, user_id, REFERRAL_BONUS))
    conn.commit()
    conn.close()

def update_balance(user_id: int, delta: float) -> bool:
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        conn.close()
        return False
    new_bal = row[0] + delta
    if new_bal < 0:
        conn.close()
        return False
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_bal, user_id))
    conn.commit()
    conn.close()
    return True

def get_balance(user_id: int) -> float:
    user = get_user(user_id)
    return user["balance"] if user else 0.0

def add_transaction(sender, receiver, amount, ttype, desc=""):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO transactions (sender_id, receiver_id, amount, type, description) VALUES (?, ?, ?, ?, ?)",
        (sender, receiver, amount, ttype, desc)
    )
    conn.commit()
    conn.close()

def is_blocked(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user["is_blocked"]) if user else False

def set_block(user_id: int, block: bool):
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (1 if block else 0, user_id))
    conn.commit()
    conn.close()

def persian_to_english(s: str) -> str:
    mapping = {'۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4',
               '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9'}
    for p, e in mapping.items():
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

# ===== START & MENU =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer_id = None
    if args and args[0].isdigit():
        referrer_id = int(args[0])
        if referrer_id == user.id:
            referrer_id = None
    create_user(user.id, user.username, user.first_name, referrer_id)
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [
        [InlineKeyboardButton("💰 موجودی", callback_data="balance")],
        [InlineKeyboardButton("💳 شارژ", callback_data="deposit")],
        [InlineKeyboardButton("🏧 برداشت", callback_data="withdraw")],
        [InlineKeyboardButton("🎮 بازی‌ها", callback_data="games")],
        [InlineKeyboardButton("👥 زیرمجموعه", callback_data="referrals")],
    ]
    await update.message.reply_text(
        "🎮 به ربات بازی‌های گروهی خوش آمدید!\n\n"
        "برای شروع بازی در گروه:\n`1 تاس 0.1` یا `1 بولینگ 0.1` و ...\n\n"
        "💰 موجودی: `موجودی` یا `موجودی من`\n"
        "💸 انتقال: روی پیام فرد Reply کنید و بنویسید `انتقال 0.1`\n"
        "👥 لینک معرفی: /referral",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    link = f"https://t.me/{context.bot.username}?start={user.id}"
    await update.message.reply_text(
        f"👥 لینک معرفی شما:\n`{link}`\n\n"
        "به ازای هر کاربر جدید که از این لینک وارد شود، 0.05 TRX به موجودی شما اضافه می‌شود."
    )

async def referrals_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
    count = c.fetchone()[0]
    c.execute("SELECT SUM(bonus) FROM referrals WHERE referrer_id = ?", (user_id,))
    total_bonus = c.fetchone()[0] or 0
    conn.close()
    await query.edit_message_text(
        f"👥 زیرمجموعه‌های شما:\n"
        f"تعداد: {count}\n"
        f"جمع پاداش: {format_amount(total_bonus)} TRX\n\n"
        f"لینک معرفی: `https://t.me/{context.bot.username}?start={user_id}`"
    )

# ===== BALANCE & TRANSFER =====
async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    bal = get_balance(user.id)
    await update.message.reply_text(f"💰 موجودی شما:\n{format_amount(bal)} TRX")

async def balance_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.strip() in ["موجودی", "موجودی من"]:
        await balance_command(update, context)

async def transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ روی پیام گیرنده Reply کنید.")
        return
    receiver = update.message.reply_to_message.from_user
    if not receiver or receiver.is_bot or receiver.id == user.id:
        await update.message.reply_text("❌ انتقال نامعتبر.")
        return
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ مبلغ معتبر وارد کنید. مثال: `انتقال 0.1`")
        return
    if get_balance(user.id) < amount:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی: {format_amount(get_balance(user.id))} TRX")
        return
    if not update_balance(user.id, -amount):
        await update.message.reply_text("❌ خطا در کسر موجودی.")
        return
    if not update_balance(receiver.id, amount):
        update_balance(user.id, amount)
        await update.message.reply_text("❌ خطا در افزایش موجودی گیرنده.")
        return
    add_transaction(user.id, receiver.id, amount, "transfer", f"به {receiver.full_name}")
    add_transaction(receiver.id, user.id, amount, "transfer", f"از {user.full_name}")
    await update.message.reply_text(
        f"✅ انتقال {format_amount(amount)} TRX از {user.full_name} به {receiver.full_name} انجام شد."
    )

# ===== DEPOSIT =====
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return ConversationHandler.END
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ لطفاً برای شارژ به پیوی ربات بروید.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("0.5", callback_data="dep_quick_0.5"),
         InlineKeyboardButton("1", callback_data="dep_quick_1")],
        [InlineKeyboardButton("2", callback_data="dep_quick_2"),
         InlineKeyboardButton("5", callback_data="dep_quick_5")],
        [InlineKeyboardButton("10", callback_data="dep_quick_10"),
         InlineKeyboardButton("مبلغ دلخواه", callback_data="dep_custom")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="dep_cancel")],
    ]
    await update.message.reply_text(
        f"💳 لطفاً مبلغ شارژ را انتخاب کنید (حداقل {DEPOSIT_MIN} TRX):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DEPOSIT_AMOUNT

async def deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    if data == "dep_cancel":
        await query.edit_message_text("❌ شارژ لغو شد.")
        return ConversationHandler.END
    if data.startswith("dep_quick_"):
        amount = float(data.split("_")[2])
        if amount < DEPOSIT_MIN:
            await query.edit_message_text(f"❌ حداقل مبلغ {DEPOSIT_MIN} TRX است.")
            return DEPOSIT_AMOUNT
        context.user_data["deposit_amount"] = amount
        keyboard = [
            [InlineKeyboardButton("💎 TON (شبکه TON)", callback_data="dep_method_ton")],
            [InlineKeyboardButton("💳 کارت بانکی", callback_data="dep_method_card")],
        ]
        await query.edit_message_text(
            f"💰 مبلغ: {format_amount(amount)} TRX\n\nلطفاً روش پرداخت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return DEPOSIT_METHOD
    elif data == "dep_custom":
        await query.edit_message_text("لطفاً مبلغ مورد نظر را به عدد وارد کنید (حداقل 0.5):")
        return DEPOSIT_AMOUNT
    return ConversationHandler.END

async def deposit_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    amount = parse_amount(update.message.text)
    if not amount or amount < DEPOSIT_MIN:
        await update.message.reply_text(f"❌ عدد معتبر حداقل {DEPOSIT_MIN} وارد کنید.")
        return DEPOSIT_AMOUNT
    context.user_data["deposit_amount"] = amount
    keyboard = [
        [InlineKeyboardButton("💎 TON (شبکه TON)", callback_data="dep_method_ton")],
        [InlineKeyboardButton("💳 کارت بانکی", callback_data="dep_method_card")],
    ]
    await update.message.reply_text(
        f"💰 مبلغ: {format_amount(amount)} TRX\n\nلطفاً روش پرداخت را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DEPOSIT_METHOD

async def deposit_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    method = query.data.split("_")[2]
    amount = context.user_data.get("deposit_amount")
    if not amount:
        await query.edit_message_text("❌ خطا: مبلغ مشخص نشد.")
        return ConversationHandler.END

    if method == "ton":
        info = f"🌐 آدرس TON برای واریز:\n`{TON_ADDRESS}`\n\nلطفاً پس از ارسال، تصویر یا هش تراکنش را ارسال کنید."
    else:
        info = f"💳 شماره کارت:\n`{BANK_CARD}`\nبه نام: {BANK_OWNER}\n\nلطفاً پس از واریز، تصویر رسید را ارسال کنید."

    context.user_data["deposit_method"] = "TON" if method == "ton" else "کارت بانکی"
    await query.edit_message_text(
        f"💰 مبلغ: {format_amount(amount)} TRX\n\n{info}\n\n"
        "پس از پرداخت، **تصویر یا هش** را در همین گفت‌وگو ارسال کنید.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="dep_cancel")]])
    )
    return DEPOSIT_PROOF

async def deposit_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    proof_text = None
    proof_photo = None
    if update.message.photo:
        proof_photo = update.message.photo[-1].file_id
        proof_text = update.message.caption or "تصویر رسید"
    elif update.message.text:
        proof_text = update.message.text
    else:
        await update.message.reply_text("❌ لطفاً یک تصویر یا متن حاوی هش تراکنش ارسال کنید.")
        return DEPOSIT_PROOF

    amount = context.user_data.get("deposit_amount")
    method = context.user_data.get("deposit_method")
    if not amount or not method:
        await update.message.reply_text("❌ خطا: اطلاعات ناقص. لطفاً دوباره از ابتدا شروع کنید.")
        return ConversationHandler.END

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO deposit_requests (user_id, amount, method, proof, status) VALUES (?, ?, ?, ?, 'pending')",
        (user.id, amount, method, proof_text)
    )
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    owner_text = (
        f"📩 درخواست شارژ جدید:\n"
        f"شماره: {req_id}\n"
        f"کاربر: {user.id} ({user.full_name})\n"
        f"مبلغ: {format_amount(amount)} TRX\n"
        f"روش: {method}\n"
        f"مدرک: {proof_text}"
    )
    keyboard = [
        [InlineKeyboardButton("✅ تأیید", callback_data=f"dep_confirm_{req_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"dep_reject_{req_id}")]
    ]
    if proof_photo:
        await context.bot.send_photo(
            chat_id=OWNER_ID,
            photo=proof_photo,
            caption=owner_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=owner_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    await update.message.reply_text(
        f"✅ درخواست شارژ {format_amount(amount)} TRX ارسال شد.\n"
        "پس از تأیید ادمین، موجودی شما افزایش می‌یابد."
    )
    context.user_data.pop("deposit_amount", None)
    context.user_data.pop("deposit_method", None)
    return ConversationHandler.END

# ===== DEPOSIT OWNER CONFIRM =====
async def deposit_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    parts = query.data.split('_')
    action, req_id = parts[1], int(parts[2])
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id, amount, status FROM deposit_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("❌ درخواست یافت نشد.")
        conn.close()
        return ConversationHandler.END
    user_id, amount, status = row
    if status != "pending":
        await query.edit_message_text(f"❌ درخواست قبلاً {status} شده.")
        conn.close()
        return ConversationHandler.END
    if action == "confirm":
        context.user_data["deposit_req_id"] = req_id
        await query.edit_message_text(
            f"مبلغ نهایی را برای شارژ کاربر {user_id} وارد کنید (درخواست: {format_amount(amount)} TRX):"
        )
        return OWNER_DEPOSIT_AMOUNT
    else:
        c.execute("UPDATE deposit_requests SET status='rejected', admin_id=? WHERE id=?", (OWNER_ID, req_id))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ درخواست {req_id} رد شد.")
        try:
            await context.bot.send_message(user_id, "❌ درخواست شارژ شما رد شد.")
        except:
            pass
        return ConversationHandler.END

async def deposit_owner_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ عدد معتبر وارد کنید.")
        return OWNER_DEPOSIT_AMOUNT
    req_id = context.user_data.get("deposit_req_id")
    if not req_id:
        await update.message.reply_text("❌ خطا.")
        return ConversationHandler.END
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id, status FROM deposit_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row or row[1] != "pending":
        await update.message.reply_text("❌ درخواست نامعتبر.")
        conn.close()
        return ConversationHandler.END
    user_id = row[0]
    c.execute("UPDATE deposit_requests SET status='approved', admin_id=? WHERE id=?", (OWNER_ID, req_id))
    conn.commit()
    conn.close()
    if update_balance(user_id, amount):
        add_transaction(None, user_id, amount, "deposit", f"شارژ {format_amount(amount)}")
        await update.message.reply_text(f"✅ شارژ {format_amount(amount)} TRX به کاربر {user_id} انجام شد.")
        try:
            await context.bot.send_message(
                user_id,
                f"✅ درخواست شارژ شما تأیید شد.\nمبلغ {format_amount(amount)} TRX به موجودی اضافه شد."
            )
        except:
            pass
    else:
        await update.message.reply_text("❌ خطا در افزایش موجودی.")
    return ConversationHandler.END

# ===== WITHDRAW =====
async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return ConversationHandler.END
    if update.effective_chat.type != "private":
        await update.message.reply_text("❌ لطفاً برای برداشت به پیوی ربات بروید.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("2.5", callback_data="with_quick_2.5"),
         InlineKeyboardButton("5", callback_data="with_quick_5")],
        [InlineKeyboardButton("10", callback_data="with_quick_10"),
         InlineKeyboardButton("مبلغ دلخواه", callback_data="with_custom")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="with_cancel")],
    ]
    await update.message.reply_text(
        f"🏧 لطفاً مبلغ برداشت را انتخاب کنید (حداقل {WITHDRAW_MIN} TRX):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WITHDRAW_AMOUNT

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    data = query.data
    if data == "with_cancel":
        await query.edit_message_text("❌ برداشت لغو شد.")
        return ConversationHandler.END
    if data.startswith("with_quick_"):
        amount = float(data.split("_")[2])
        if amount < WITHDRAW_MIN:
            await query.edit_message_text(f"❌ حداقل مبلغ {WITHDRAW_MIN} TRX است.")
            return WITHDRAW_AMOUNT
        if get_balance(user.id) < amount:
            await query.edit_message_text(f"❌ موجودی کافی نیست. موجودی: {format_amount(get_balance(user.id))} TRX")
            return WITHDRAW_AMOUNT
        context.user_data["withdraw_amount"] = amount
        keyboard = [
            [InlineKeyboardButton("💎 TON (شبکه TON)", callback_data="with_method_ton")],
            [InlineKeyboardButton("💳 کارت بانکی", callback_data="with_method_card")],
        ]
        await query.edit_message_text(
            f"💰 مبلغ: {format_amount(amount)} TRX\n\nلطفاً روش برداشت را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return WITHDRAW_METHOD
    elif data == "with_custom":
        await query.edit_message_text("لطفاً مبلغ مورد نظر را به عدد وارد کنید (حداقل 2.5):")
        return WITHDRAW_AMOUNT
    return ConversationHandler.END

async def withdraw_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    amount = parse_amount(update.message.text)
    if not amount or amount < WITHDRAW_MIN:
        await update.message.reply_text(f"❌ عدد معتبر حداقل {WITHDRAW_MIN} وارد کنید.")
        return WITHDRAW_AMOUNT
    if get_balance(user.id) < amount:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی: {format_amount(get_balance(user.id))} TRX")
        return WITHDRAW_AMOUNT
    context.user_data["withdraw_amount"] = amount
    keyboard = [
        [InlineKeyboardButton("💎 TON (شبکه TON)", callback_data="with_method_ton")],
        [InlineKeyboardButton("💳 کارت بانکی", callback_data="with_method_card")],
    ]
    await update.message.reply_text(
        f"💰 مبلغ: {format_amount(amount)} TRX\n\nلطفاً روش برداشت را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WITHDRAW_METHOD

async def withdraw_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    method = query.data.split("_")[2]
    amount = context.user_data.get("withdraw_amount")
    if not amount:
        await query.edit_message_text("❌ خطا: مبلغ مشخص نشد.")
        return ConversationHandler.END

    if method == "ton":
        info = "🌐 لطفاً آدرس TON خود را وارد کنید:"
    else:
        info = "💳 لطفاً شماره کارت بانکی خود را وارد کنید:"

    context.user_data["withdraw_method"] = "TON" if method == "ton" else "کارت بانکی"
    await query.edit_message_text(
        f"💰 مبلغ: {format_amount(amount)} TRX\n\n{info}\n\n"
        "لطفاً اطلاعات حساب خود را ارسال کنید.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="with_cancel")]])
    )
    return WITHDRAW_PROOF

async def withdraw_proof(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    account_info = update.message.text
    if not account_info:
        await update.message.reply_text("❌ لطفاً اطلاعات حساب خود را به صورت متن ارسال کنید.")
        return WITHDRAW_PROOF

    amount = context.user_data.get("withdraw_amount")
    method = context.user_data.get("withdraw_method")
    if not amount or not method:
        await update.message.reply_text("❌ خطا: اطلاعات ناقص. لطفاً دوباره از ابتدا شروع کنید.")
        return ConversationHandler.END

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO withdraw_requests (user_id, amount, method, account_info, status) VALUES (?, ?, ?, ?, 'pending')",
        (user.id, amount, method, account_info)
    )
    req_id = c.lastrowid
    conn.commit()
    conn.close()

    owner_text = (
        f"📩 درخواست برداشت جدید:\n"
        f"شماره: {req_id}\n"
        f"کاربر: {user.id} ({user.full_name})\n"
        f"مبلغ: {format_amount(amount)} TRX\n"
        f"روش: {method}\n"
        f"اطلاعات حساب: {account_info}"
    )
    keyboard = [
        [InlineKeyboardButton("✅ تأیید", callback_data=f"with_confirm_{req_id}"),
         InlineKeyboardButton("❌ رد", callback_data=f"with_reject_{req_id}")]
    ]
    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=owner_text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await update.message.reply_text(
        f"✅ درخواست برداشت {format_amount(amount)} TRX ارسال شد.\n"
        "پس از تأیید ادمین، مبلغ از موجودی شما کسر می‌شود."
    )
    context.user_data.pop("withdraw_amount", None)
    context.user_data.pop("withdraw_method", None)
    return ConversationHandler.END

# ===== WITHDRAW OWNER CONFIRM =====
async def withdraw_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return ConversationHandler.END
    parts = query.data.split('_')
    action, req_id = parts[1], int(parts[2])
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id, amount, status FROM withdraw_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row:
        await query.edit_message_text("❌ درخواست یافت نشد.")
        conn.close()
        return ConversationHandler.END
    user_id, amount, status = row
    if status != "pending":
        await query.edit_message_text(f"❌ درخواست قبلاً {status} شده.")
        conn.close()
        return ConversationHandler.END
    if action == "confirm":
        context.user_data["withdraw_req_id"] = req_id
        await query.edit_message_text(
            f"مبلغ نهایی را برای برداشت از کاربر {user_id} وارد کنید (درخواست: {format_amount(amount)} TRX):"
        )
        return OWNER_WITHDRAW_AMOUNT
    else:
        c.execute("UPDATE withdraw_requests SET status='rejected', admin_id=? WHERE id=?", (OWNER_ID, req_id))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"❌ درخواست {req_id} رد شد.")
        try:
            await context.bot.send_message(user_id, "❌ درخواست برداشت شما رد شد.")
        except:
            pass
        return ConversationHandler.END

async def withdraw_owner_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ عدد معتبر وارد کنید.")
        return OWNER_WITHDRAW_AMOUNT
    req_id = context.user_data.get("withdraw_req_id")
    if not req_id:
        await update.message.reply_text("❌ خطا.")
        return ConversationHandler.END
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT user_id, status FROM withdraw_requests WHERE id = ?", (req_id,))
    row = c.fetchone()
    if not row or row[1] != "pending":
        await update.message.reply_text("❌ درخواست نامعتبر.")
        conn.close()
        return ConversationHandler.END
    user_id = row[0]
    if get_balance(user_id) < amount:
        await update.message.reply_text(f"❌ موجودی کاربر کافی نیست. موجودی: {format_amount(get_balance(user_id))} TRX")
        return ConversationHandler.END
    c.execute("UPDATE withdraw_requests SET status='approved', admin_id=? WHERE id=?", (OWNER_ID, req_id))
    conn.commit()
    conn.close()
    if update_balance(user_id, -amount):
        add_transaction(user_id, None, amount, "withdraw", f"برداشت {format_amount(amount)}")
        await update.message.reply_text(f"✅ برداشت {format_amount(amount)} TRX از کاربر {user_id} انجام شد.")
        try:
            await context.bot.send_message(
                user_id,
                f"✅ درخواست برداشت شما تأیید شد.\nمبلغ {format_amount(amount)} TRX از موجودی کسر شد."
            )
        except:
            pass
    else:
        await update.message.reply_text("❌ خطا در کاهش موجودی.")
    return ConversationHandler.END

# ===== GAMES =====
async def game_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    if update.effective_chat.type == "private":
        await update.message.reply_text("❌ لطفاً برای بازی به گروه بروید.")
        return
    text = persian_to_english(update.message.text.strip())
    match = re.match(r'^1\s+(تاس|بولینگ|دارت|بسکتبال)\s+(\d+(?:\.\d+)?)$', text)
    if not match:
        return
    game_type, bet_str = match.groups()
    bet = float(bet_str)
    if bet != GAME_BET:
        await update.message.reply_text(f"❌ مبلغ شرط باید {GAME_BET} TRX باشد.")
        return
    if get_balance(user.id) < bet:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی: {format_amount(get_balance(user.id))} TRX")
        return
    if not update_balance(user.id, -bet):
        await update.message.reply_text("❌ خطا در کسر موجودی.")
        return
    add_transaction(user.id, None, bet, "game", f"شرط {game_type}")
    keyboard = [
        [InlineKeyboardButton("👥 بازی با دوستان", callback_data="game_friends")],
        [InlineKeyboardButton("🤖 بازی با ربات", callback_data="game_bot")],
        [InlineKeyboardButton("❌ لغو", callback_data="game_cancel")],
    ]
    msg = await update.message.reply_text(
        f"🎮 {game_type} با مبلغ {format_amount(bet)} TRX\nحالت بازی را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    pending_games[(update.effective_chat.id, msg.message_id)] = {
        "creator": user.id, "game_type": game_type, "bet": bet,
        "chat_id": update.effective_chat.id, "message_id": msg.message_id,
    }

async def game_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    chat_id = update.effective_chat.id
    if is_blocked(user.id):
        await query.edit_message_text("⛔ شما مسدود شده‌اید.")
        return
    key = (chat_id, query.message.message_id)
    if key not in pending_games:
        await query.edit_message_text("⏳ این بازی منقضی شده.")
        return
    data = pending_games.pop(key)
    if data["creator"] != user.id:
        await query.answer("❌ فقط سازنده می‌تواند انتخاب کند.", show_alert=True)
        return
    if query.data == "game_cancel":
        update_balance(user.id, data["bet"])
        add_transaction(None, user.id, data["bet"], "game", "بازگشت شرط به دلیل لغو")
        await query.edit_message_text("❌ بازی لغو شد و موجودی شما برگشت داده شد.")
        return

    if query.data == "game_bot":
        global bot_busy, bot_game_chat_id
        if bot_busy:
            update_balance(user.id, data["bet"])
            add_transaction(None, user.id, data["bet"], "game", "بازگشت شرط به دلیل مشغول بودن ربات")
            await query.edit_message_text(
                "❌ ربات در حال بازی با کاربر دیگری است.\nموجودی شما برگشت داده شد."
            )
            return
        bot_busy = True
        bot_game_chat_id = chat_id

    session = {
        "creator": user.id, "game_type": data["game_type"], "bet": data["bet"],
        "mode": "bot" if query.data == "game_bot" else "friends",
        "players": [user.id], "scores": [], "current": 0, "finished": False,
        "paid": {user.id: True}, "user_rolled": False, "bot_rolled": False,
    }
    active_games[chat_id] = session

    if query.data == "game_bot":
        session["players"].append(None)
        await query.edit_message_text(
            f"🤖 بازی با ربات شروع شد.\n"
            f"نوع بازی: {data['game_type']}\n"
            f"مبلغ شرط: {format_amount(data['bet'])} TRX\n"
            f"جایزه برنده: {format_amount(GAME_WIN)} TRX\n\n"
            f"👤 {get_user_name(user.id)} نوبت شماست!\n"
            f"لطفاً یک پیام (هر چیزی) در این گفت‌وگو ارسال کنید تا {GAME_EMOJIS[data['game_type']]} بیندازید."
        )
    else:
        await query.edit_message_text(
            f"👥 بازی با دوستان شروع شد.\n"
            f"نوع بازی: {data['game_type']}\n"
            f"مبلغ شرط: {format_amount(data['bet'])} TRX\n"
            f"جایزه برنده: {format_amount(GAME_WIN)} TRX\n\n"
            f"👤 {get_user_name(user.id)} نوبت شماست!\n"
            f"لطفاً یک پیام (هر چیزی) در این گفت‌وگو ارسال کنید تا {GAME_EMOJIS[data['game_type']]} بیندازید.\n"
            f"(بازیکن دوم بعداً می‌تواند با ارسال پیام بپیوندد)"
        )

async def game_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if update.effective_chat.type == "private":
        return
    session = active_games.get(chat_id)
    if not session or session["finished"]:
        return
    if session["mode"] == "bot":
        if session["current"] == 0:
            if session["players"][0] != user.id:
                await update.message.reply_text("⏳ الان نوبت شما نیست، منتظر نوبت خود باشید.")
                return
            if session.get("user_rolled", False):
                await update.message.reply_text("⏳ شما قبلاً پرتاب کردید، منتظر ربات باشید.")
                return
        else:
            await update.message.reply_text("🤖 نوبت ربات است، لطفاً صبر کنید...")
            return
    else:
        if len(session["players"]) < 2:
            if user.id != session["players"][0]:
                if get_balance(user.id) < session["bet"]:
                    await update.message.reply_text("❌ موجودی کافی برای شرکت در بازی ندارید.")
                    return
                session["players"].append(user.id)
                session["paid"][user.id] = False
                if not update_balance(user.id, -session["bet"]):
                    session["players"].pop()
                    await update.message.reply_text("❌ خطا در کسر موجودی.")
                    return
                session["paid"][user.id] = True
                add_transaction(user.id, None, session["bet"], "game", f"شرط {session['game_type']} با دوستان")
                await update.message.reply_text(
                    f"👤 {get_user_name(user.id)} به بازی پیوست!\n"
                    f"نوبت شماست، یک پیام ارسال کنید تا {GAME_EMOJIS[session['game_type']]} بیندازید."
                )
                return
        if session["current"] >= len(session["players"]):
            return
        if session["players"][session["current"]] != user.id:
            await update.message.reply_text("⏳ الان نوبت شما نیست، منتظر نوبت خود باشید.")
            return
        if not session["paid"].get(user.id, False):
            if not update_balance(user.id, -session["bet"]):
                await update.message.reply_text("❌ موجودی کافی نیست.")
                return
            session["paid"][user.id] = True
            add_transaction(user.id, None, session["bet"], "game", f"شرط {session['game_type']} با دوستان")
    # کاربر پرتاب میکنه
    emoji = GAME_EMOJIS[session["game_type"]]
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    value = dice_msg.dice.value
    session["scores"].append(value)
    if session["mode"] == "bot" and session["current"] == 0:
        session["user_rolled"] = True
        session["current"] = 1
        await update.message.reply_text(
            f"🎲 {get_user_name(user.id)}: {value}\n\n🤖 نوبت ربات..."
        )
        context.job_queue.run_once(bot_roll, 2.0, context=chat_id)
    else:
        session["current"] += 1
        if len(session["scores"]) == len(session["players"]):
            await finish_game(chat_id, context)
        else:
            next_player = session["players"][session["current"]]
            await update.message.reply_text(
                f"🎲 {get_user_name(user.id)}: {value}\n"
                f"👤 نوبت {get_user_name(next_player)}\n"
                f"لطفاً یک پیام ارسال کنید تا {GAME_EMOJIS[session['game_type']]} بیندازید."
            )

async def bot_roll(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    session = active_games.get(chat_id)
    if not session or session["finished"]:
        global bot_busy, bot_game_chat_id
        bot_busy = False
        bot_game_chat_id = None
        return
    if session.get("bot_rolled", False):
        return
    session["bot_rolled"] = True
    emoji = GAME_EMOJIS[session["game_type"]]
    dice_msg = await context.bot.send_dice(chat_id=chat_id, emoji=emoji)
    value = dice_msg.dice.value
    session["scores"].append(value)
    await finish_game(chat_id, context)

async def finish_game(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = active_games.get(chat_id)
    if not session or session["finished"]:
        return
    session["finished"] = True
    players = session["players"]
    scores = session["scores"]
    bet = session["bet"]
    game_type = session["game_type"]
    mode = session["mode"]
    if len(scores) < 2:
        return
    p1_score, p2_score = scores[0], scores[1]
    result_text = ""
    winner = None
    if p1_score > p2_score:
        winner = players[0]
        result_text = f"🎉 {get_user_name(players[0])} برنده شد!"
    elif p1_score < p2_score:
        if mode == "bot":
            result_text = "🤖 ربات برنده شد!"
        else:
            winner = players[1]
            result_text = f"🎉 {get_user_name(players[1])} برنده شد!"
    else:
        if mode == "bot":
            result_text = "🤝 مساوی! دوباره بازی کنید."
            update_balance(players[0], bet)
            add_transaction(None, players[0], bet, "game", "بازگشت شرط به دلیل مساوی")
            session["finished"] = False
            session["scores"] = []
            session["current"] = 0
            session["user_rolled"] = False
            session["bot_rolled"] = False
            await context.bot.send_message(
                chat_id=chat_id,
                text=result_text + f"\nپرتاب مجدد... نوبت {get_user_name(players[0])} است.\nلطفاً یک پیام ارسال کنید تا تاس بیندازید."
            )
            return
        else:
            result_text = "🤝 مساوی! دوباره بازی کنید."
            for pid in players:
                update_balance(pid, bet)
                add_transaction(None, pid, bet, "game", "بازگشت شرط به دلیل مساوی")
            session["finished"] = False
            session["scores"] = []
            session["current"] = 0
            await context.bot.send_message(
                chat_id=chat_id,
                text=result_text + f"\nپرتاب مجدد... نوبت {get_user_name(players[0])} است.\nلطفاً یک پیام ارسال کنید تا تاس بیندازید."
            )
            return
    if winner:
        prize = GAME_WIN
        update_balance(winner, prize)
        add_transaction(None, winner, prize, "game", f"برد در {game_type} - جایزه {format_amount(prize)}")
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO games (player1_id, player2_id, game_type, bet_amount, win_amount, fee_amount, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (players[0], players[1] if len(players) > 1 else 0, game_type, bet, GAME_WIN, GAME_FEE, "win" if winner else "tie")
    )
    conn.commit()
    conn.close()
    await context.bot.send_message(chat_id=chat_id, text=result_text)
    del active_games[chat_id]
    if mode == "bot":
        global bot_busy, bot_game_chat_id
        bot_busy = False
        bot_game_chat_id = None

# ===== ADMIN PANEL =====
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
        await query.edit_message_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    data = query.data
    if data == "admin_close":
        await query.edit_message_text("❌ پنل مدیریت بسته شد.")
        return ConversationHandler.END
    elif data == "admin_stats":
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM games")
        total_games = c.fetchone()[0]
        c.execute("SELECT SUM(amount) FROM transactions WHERE type='deposit'")
        total_dep = c.fetchone()[0] or 0
        c.execute("SELECT SUM(amount) FROM transactions WHERE type='withdraw'")
        total_with = c.fetchone()[0] or 0
        conn.close()
        await query.edit_message_text(
            f"📊 آمار کلی:\n👥 کاربران: {total_users}\n🎮 بازی‌ها: {total_games}\n💰 واریزها: {format_amount(total_dep)} TRX\n🏧 برداشت‌ها: {format_amount(total_with)} TRX",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]])
        )
        return ConversationHandler.END
    elif data == "admin_users":
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, balance, is_blocked FROM users LIMIT 10")
        rows = c.fetchall()
        conn.close()
        if not rows:
            text = "هیچ کاربری یافت نشد."
        else:
            text = "👥 کاربران (۱۰ تای اول):\n"
            for row in rows:
                uid, uname, fname, bal, blocked = row
                name = fname or uname or str(uid)
                status = "🚫" if blocked else "✅"
                text += f"{status} {name} (ID: {uid}) - {format_amount(bal)} TRX\n"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]])
        )
        return ConversationHandler.END
    elif data in ["admin_balance", "admin_add", "admin_sub"]:
        context.user_data["admin_action"] = data.replace("admin_", "")
        await query.edit_message_text("لطفاً USER_ID کاربر را وارد کنید:")
        return ADMIN_GET_USER_ID
    elif data == "admin_transactions":
        conn = sqlite3.connect(DATABASE)
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
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]])
        )
        return ConversationHandler.END
    elif data == "admin_games":
        conn = sqlite3.connect(DATABASE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM games")
        total = c.fetchone()[0]
        c.execute("SELECT game_type, COUNT(*) FROM games GROUP BY game_type")
        by_type = c.fetchall()
        c.execute("SELECT result, COUNT(*) FROM games GROUP BY result")
        by_result = c.fetchall()
        conn.close()
        text = f"🎮 آمار بازی‌ها:\nکل: {total}\n"
        for typ, cnt in by_type:
            text += f"{typ}: {cnt}\n"
        text += "\nنتایج:\n"
        for res, cnt in by_result:
            text += f"{res}: {cnt}\n"
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")]])
        )
        return ConversationHandler.END
    elif data in ["admin_block", "admin_unblock"]:
        context.user_data["admin_action"] = data.replace("admin_", "")
        await query.edit_message_text("لطفاً USER_ID کاربر را وارد کنید:")
        return ADMIN_GET_USER_ID_FOR_BLOCK
    elif data == "admin_back":
        await admin_command(update, context)
        return ConversationHandler.END
    return ConversationHandler.END

async def admin_get_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    try:
        target = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ USER_ID باید عددی باشد. دوباره وارد کنید:")
        return ADMIN_GET_USER_ID
    action = context.user_data.get("admin_action")
    if action == "balance":
        bal = get_balance(target)
        if bal is None:
            await update.message.reply_text("❌ کاربر یافت نشد.")
        else:
            name = get_user_name(target)
            await update.message.reply_text(f"💰 موجودی {name}: {format_amount(bal)} TRX")
        await admin_command(update, context)
        return ConversationHandler.END
    elif action in ["add", "sub"]:
        context.user_data["target_user"] = target
        await update.message.reply_text("لطفاً مبلغ مورد نظر را وارد کنید (عدد):")
        return ADMIN_GET_AMOUNT
    else:
        await update.message.reply_text("خطا در تشخیص عملیات.")
        return ConversationHandler.END

async def admin_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    amount = parse_amount(update.message.text)
    if not amount or amount <= 0:
        await update.message.reply_text("❌ عدد معتبر وارد کنید.")
        return ADMIN_GET_AMOUNT
    target = context.user_data.get("target_user")
    if not target:
        await update.message.reply_text("❌ خطا: کاربر مشخص نشد.")
        return ConversationHandler.END
    action = context.user_data.get("admin_action")
    if action == "add":
        if update_balance(target, amount):
            add_transaction(None, target, amount, "admin_add", "افزایش توسط ادمین")
            await update.message.reply_text(f"✅ {format_amount(amount)} TRX به موجودی {target} اضافه شد.")
        else:
            await update.message.reply_text("❌ خطا در افزایش موجودی.")
    elif action == "sub":
        if get_balance(target) < amount:
            await update.message.reply_text(f"❌ موجودی کاربر کافی نیست. موجودی: {format_amount(get_balance(target))} TRX")
            return ConversationHandler.END
        if update_balance(target, -amount):
            add_transaction(target, None, amount, "admin_sub", "کاهش توسط ادمین")
            await update.message.reply_text(f"✅ {format_amount(amount)} TRX از موجودی {target} کسر شد.")
        else:
            await update.message.reply_text("❌ خطا در کاهش موجودی.")
    else:
        await update.message.reply_text("خطا.")
    await admin_command(update, context)
    return ConversationHandler.END

async def admin_get_user_id_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ دسترسی ندارید.")
        return ConversationHandler.END
    try:
        target = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ USER_ID باید عددی باشد.")
        return ADMIN_GET_USER_ID_FOR_BLOCK
    if get_user(target) is None:
        await update.message.reply_text("❌ کاربر یافت نشد.")
    else:
        action = context.user_data.get("admin_action")
        if action == "block":
            set_block(target, True)
            await update.message.reply_text(f"✅ کاربر {target} مسدود شد.")
        elif action == "unblock":
            set_block(target, False)
            await update.message.reply_text(f"✅ کاربر {target} رفع مسدودی شد.")
        else:
            await update.message.reply_text("خطا.")
    await admin_command(update, context)
    return ConversationHandler.END

# ===== MAIN MENU =====
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
        await query.edit_message_text(
            "🎮 برای شروع بازی، در گروه پیام زیر را ارسال کنید:\n"
            "`1 تاس 0.1` یا `1 بولینگ 0.1` یا `1 دارت 0.1` یا `1 بسکتبال 0.1`\n\n"
            "بازی‌های موجود: تاس، بولینگ، دارت، بسکتبال"
        )
    elif data == "referrals":
        await referrals_menu(update, context)
    else:
        await query.edit_message_text("گزینه نامعتبر.")

# ===== ERROR =====
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")
    global bot_busy, bot_game_chat_id
    bot_busy = False
    bot_game_chat_id = None

# ===== MAIN =====
def main():
    init_db()
    logging.basicConfig(level=logging.INFO)

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("referral", referral_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("deposit", deposit_start))
    app.add_handler(CommandHandler("withdraw", withdraw_start))

    # Message handlers
    app.add_handler(MessageHandler(filters.Regex(r'^(موجودی|موجودی من)$'), balance_text))
    app.add_handler(MessageHandler(filters.Regex(r'^انتقال\s+'), transfer))
    app.add_handler(MessageHandler(filters.Regex(r'^1\s+(تاس|بولینگ|دارت|بسکتبال)\s+\d+(?:\.\d+)?$'), game_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, game_message_handler))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(game_callback, pattern="^game_"))
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^(balance|deposit|withdraw|games|referrals)$"))
    app.add_handler(CallbackQueryHandler(deposit_callback, pattern="^dep_"))
    app.add_handler(CallbackQueryHandler(withdraw_callback, pattern="^with_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))

    # Deposit conversation
    deposit_conv = ConversationHandler(
        entry_points=[],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_input)],
            DEPOSIT_METHOD: [CallbackQueryHandler(deposit_method_callback, pattern="^dep_method_")],
            DEPOSIT_PROOF: [MessageHandler(filters.PHOTO | filters.TEXT, deposit_proof)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(deposit_conv)

    # Withdraw conversation
    withdraw_conv = ConversationHandler(
        entry_points=[],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_input)],
            WITHDRAW_METHOD: [CallbackQueryHandler(withdraw_method_callback, pattern="^with_method_")],
            WITHDRAW_PROOF: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_proof)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(withdraw_conv)

    # Owner confirm conversations
    owner_deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(deposit_confirm_callback, pattern="^dep_(confirm|reject)_")],
        states={
            OWNER_DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_owner_amount)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(owner_deposit_conv)

    owner_withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(withdraw_confirm_callback, pattern="^with_(confirm|reject)_")],
        states={
            OWNER_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_owner_amount)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: c.bot.send_message(u.effective_chat.id, "لغو شد."))],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(owner_withdraw_conv)

    # Admin conversation
    admin_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^admin_")],
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

    app.add_error_handler(error_handler)

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
