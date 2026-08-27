# =========================
# Telegram Bot - Part 1/4
# =========================

import os
import sqlite3
import random
import logging
import time
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)


logging.basicConfig(
    level=logging.INFO
)


# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است")


OWNER_ID = 8552447077

ADMINS = [
    OWNER_ID
]


FORCE_CHANNEL = "@zobxt"


# دیتابیس دائمی
DB_PATH = "/data/bot.db"

if not os.path.exists("/data"):
    DB_PATH = "bot.db"


bot_enabled = True


# =========================
# DATABASE
# =========================

def db():
    return sqlite3.connect(DB_PATH)


def init_db():

    con = db()
    cur = con.cursor()


    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        balance REAL DEFAULT 0,
        captcha INTEGER DEFAULT 0
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        kind TEXT,
        unique_id TEXT UNIQUE,
        created TEXT
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user1 INTEGER,
        user2 INTEGER,
        game TEXT,
        bet REAL,
        status TEXT,
        created TEXT
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        address TEXT,
        status TEXT,
        created TEXT
    )
    """)


    con.commit()
    con.close()



def add_user(user):

    con = db()
    cur = con.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO users
        (user_id,name)
        VALUES(?,?)
        """,
        (
            user.id,
            user.full_name
        )
    )

    con.commit()
    con.close()



def balance(uid):

    con = db()
    cur = con.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (uid,)
    )

    row = cur.fetchone()

    con.close()

    return row[0] if row else 0



def change_balance(uid, amount, kind):

    con = db()
    cur = con.cursor()

    code = f"{uid}_{time.time()}"

    cur.execute(
        """
        UPDATE users
        SET balance = balance + ?
        WHERE user_id=?
        """,
        (
            amount,
            uid
        )
    )

    cur.execute(
        """
        INSERT INTO transactions
        (user_id,amount,kind,unique_id,created)
        VALUES(?,?,?,?,?)
        """,
        (
            uid,
            amount,
            kind,
            code,
            str(datetime.now())
        )
    )

    con.commit()
    con.close()

# =========================
# PART 2/4
# =========================


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    try:
        member = await context.bot.get_chat_member(
            FORCE_CHANNEL,
            user.id
        )

        if member.status in [
            "member",
            "administrator",
            "creator"
        ]:
            return True

    except:
        pass


    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 عضویت در کانال",
                    url="https://t.me/zobxt"
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


    await update.message.reply_text(
        "❌ ابتدا عضو کانال شوید.",
        reply_markup=keyboard
    )

    return False



def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                "💰 موجودی",
                "🎮 بازی"
            ],
            [
                "🔄 انتقال",
                "💸 برداشت"
            ],
            [
                "👑 پنل مدیریت"
            ]
        ],
        resize_keyboard=True
    )



def game_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                "🎲 تاس",
                "🎳 بولینگ"
            ],
            [
                "🎯 دارت",
                "🏀 بسکتبال"
            ],
            [
                "👥 بازی با دوستان",
                "🤖 بازی با ربات"
            ],
            [
                "❌ لغو"
            ]
        ],
        resize_keyboard=True
    )



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not await check_join(update, context):
        return


    add_user(
        update.effective_user
    )


    captcha = random.randint(1000,9999)

    con=db()
    cur=con.cursor()

    cur.execute(
        """
        UPDATE users
        SET captcha=?
        WHERE user_id=?
        """,
        (
            captcha,
            update.effective_user.id
        )
    )

    con.commit()
    con.close()


    await update.message.reply_text(
        f"🔐 کپچا را وارد کنید:\n\n{captcha}",
        reply_markup=ReplyKeyboardRemove()
    )



async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user=update.effective_user
    text=update.message.text


    add_user(user)


    if text.isdigit():

        con=db()
        cur=con.cursor()

        cur.execute(
            """
            SELECT captcha FROM users
            WHERE user_id=?
            """,
            (user.id,)
        )

        row=cur.fetchone()

        con.close()


        if row and int(text)==row[0]:

            await update.message.reply_text(
                "✅ تایید شد",
                reply_markup=main_keyboard()
            )

            return



    if text=="💰 موجودی":

        await update.message.reply_text(
            f"💰 موجودی شما:\n{balance(user.id)} TRX"
        )



    elif text=="🎮 بازی":

        await update.message.reply_text(
            "🎮 نوع بازی را انتخاب کنید:",
            reply_markup=game_keyboard()
        )



    elif text=="👑 پنل مدیریت":

        if user.id in ADMINS:

            await update.message.reply_text(
                "👑 پنل مدیریت:\n\n"
                "🟢 روشن\n"
                "🔴 خاموش\n"
                "💰 موجودی کاربران\n"
                "➕ افزایش موجودی\n"
                "➖ کسر موجودی"
            )



    elif text=="🔄 انتقال":

        await update.message.reply_text(
            "روی پیام کاربر Reply کنید و بنویسید:\n\n"
            "انتقال 0.1"
    )

# =========================
# PART 3/4
# =========================


active_games = {}


def roll_game(game):

    if game == "🎲 تاس":
        return random.randint(1,6)

    elif game == "🎳 بولینگ":
        return random.randint(0,10)

    elif game == "🎯 دارت":
        return random.randint(0,60)

    elif game == "🏀 بسکتبال":
        return random.randint(0,3)



def win_reward(user_id, bet):

    # 80 درصد به بازیکن
    reward = round(bet * 1.8, 2)

    # 20 درصد سهم مالک
    owner_fee = round(bet * 0.2, 2)


    change_balance(
        user_id,
        reward,
        "game_win"
    )


    change_balance(
        OWNER_ID,
        owner_fee,
        "owner_fee"
    )


    return reward



async def play_bot(update, context, game, bet):

    user = update.effective_user


    if balance(user.id) < bet:

        await update.message.reply_text(
            "❌ موجودی کافی نیست"
        )
        return



    change_balance(
        user.id,
        -bet,
        "game_bet"
    )


    await update.message.reply_text(
        "🎮 نوبت شماست، ایموجی بازی را ارسال کنید."
    )


    context.user_data["bot_game"] = {
        "game":game,
        "bet":bet
    }



async def handle_game_throw(update, context):

    user=update.effective_user

    data=context.user_data.get(
        "bot_game"
    )


    if not data:
        return


    game=data["game"]
    bet=data["bet"]


    user_score=roll_game(game)


    await update.message.reply_text(
        f"🎮 امتیاز شما: {user_score}\n"
        "🤖 نوبت ربات..."
    )


    bot_score=roll_game(game)


    if user_score > bot_score:

        reward=win_reward(
            user.id,
            bet
        )


        result=(
            f"🏆 شما بردید\n"
            f"🎁 جایزه: {reward} TRX"
        )


    elif user_score < bot_score:

        result=(
            f"❌ شما باختید\n"
            f"🤖 ربات: {bot_score}"
        )


    else:

        change_balance(
            user.id,
            bet,
            "draw_return"
        )

        result=(
            "🤝 مساوی شد\n"
            "🔄 دوباره بازی کنید"
        )


    await update.message.reply_text(
        result
    )


    context.user_data.pop(
        "bot_game",
        None
    )



async def select_game(update, context):

    text=update.message.text


    games=[
        "🎲 تاس",
        "🎳 بولینگ",
        "🎯 دارت",
        "🏀 بسکتبال"
    ]


    if text in games:

        context.user_data["selected_game"]=text

        await update.message.reply_text(
            "مبلغ شرط را ارسال کنید.\nمثال:\n0.1"
        )

        return



    if text=="🤖 بازی با ربات":

        await update.message.reply_text(
            "بازی را انتخاب کنید."
        )

        return
# =========================
# PART 4/4
# =========================


async def transfer(update, context):

    msg = update.message

    if not msg.reply_to_message:
        await msg.reply_text(
            "❌ روی پیام کاربر Reply کنید."
        )
        return


    try:
        amount = float(
            msg.text.split()[1]
        )

    except:
        await msg.reply_text(
            "❌ مقدار اشتباه است."
        )
        return


    sender = update.effective_user.id
    receiver = msg.reply_to_message.from_user.id


    if balance(sender) < amount:
        await msg.reply_text(
            "❌ موجودی کافی نیست."
        )
        return


    change_balance(
        sender,
        -amount,
        "transfer_out"
    )

    change_balance(
        receiver,
        amount,
        "transfer_in"
    )


    await msg.reply_text(
        "✅ انتقال انجام شد."
    )



async def withdrawal(update, context):

    user = update.effective_user


    await update.message.reply_text(
        "💸 مقدار برداشت را بفرستید.\nحداقل: 2 TRX"
    )

    context.user_data["withdraw_step"]="amount"



async def withdraw_process(update, context):

    user=update.effective_user
    text=update.message.text


    if context.user_data.get("withdraw_step")=="amount":

        try:
            amount=float(text)
        except:
            await update.message.reply_text(
                "❌ مقدار نامعتبر"
            )
            return


        if amount < 2:
            await update.message.reply_text(
                "❌ حداقل برداشت 2 TRX است"
            )
            return


        if balance(user.id)<amount:
            await update.message.reply_text(
                "❌ موجودی کافی نیست"
            )
            return


        context.user_data["withdraw_amount"]=amount
        context.user_data["withdraw_step"]="address"


        await update.message.reply_text(
            "آدرس TRC20 را ارسال کنید."
        )

        return


    if context.user_data.get("withdraw_step")=="address":

        address=text
        amount=context.user_data["withdraw_amount"]


        change_balance(
            user.id,
            -amount,
            "withdraw_pending"
        )


        con=db()
        cur=con.cursor()

        cur.execute(
            """
            INSERT INTO withdrawals
            (user_id,amount,address,status,created)
            VALUES(?,?,?,?,?)
            """,
            (
                user.id,
                amount,
                address,
                "pending",
                str(datetime.now())
            )
        )

        con.commit()
        con.close()


        for admin in ADMINS:

            await context.bot.send_message(
                admin,
                f"💸 درخواست برداشت\n\n"
                f"کاربر: {user.id}\n"
                f"مقدار: {amount} TRX\n"
                f"آدرس:\n{address}"
            )


        await update.message.reply_text(
            "✅ درخواست ارسال شد."
        )


        context.user_data.clear()



async def admin(update, context):

    if update.effective_user.id not in ADMINS:
        return


    await update.message.reply_text(
        "👑 پنل مدیریت\n\n"
        "🟢 روشن\n"
        "🔴 خاموش\n"
        "💰 موجودی کاربران\n"
        "➕ افزایش موجودی\n"
        "➖ کسر موجودی"
    )



async def main():

    init_db()


    app = Application.builder().token(
        BOT_TOKEN
    ).build()


    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT,
            text_handler
        )
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT,
            handle_game_throw
        )
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT,
            withdraw_process
        )
    )


    app.run_polling()



if __name__=="__main__":

    import asyncio

    asyncio.run(main())
    
