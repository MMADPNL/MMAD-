# ==============================
# BOT.PY PART 1/4
# ==============================

import os
import sqlite3
import logging
import secrets
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ==============================
# CONFIG
# ==============================

BOT_TOKEN = os.getenv("BOT_TOKEN")

OWNER_ID = 8552447077

ADMIN_IDS = [
    OWNER_ID
]

CHANNEL = "@zobxt"

DB = "bot.db"


logging.basicConfig(
    level=logging.INFO
)


# ==============================
# DATABASE
# ==============================

def db():

    con = sqlite3.connect(
        DB,
        check_same_thread=False
    )

    con.row_factory = sqlite3.Row

    return con



def init_db():

    con = db()

    cur = con.cursor()


    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(

        id INTEGER PRIMARY KEY,

        name TEXT,

        balance REAL DEFAULT 0,

        verified INTEGER DEFAULT 0

    )
    """)



    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        tx TEXT UNIQUE,

        user INTEGER,

        amount REAL,

        type TEXT,

        time TEXT

    )
    """)



    cur.execute("""
    CREATE TABLE IF NOT EXISTS games(

        id TEXT PRIMARY KEY,

        game TEXT,

        player1 INTEGER,

        player2 INTEGER,

        bet REAL,

        status TEXT

    )
    """)



    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdrawals(

        id INTEGER PRIMARY KEY AUTOINCREMENT,

        user INTEGER,

        amount REAL,

        address TEXT,

        status TEXT

    )
    """)


    con.commit()

    con.close()



# ==============================
# USERS
# ==============================


def add_user(user):

    con=db()

    con.execute(
        """
        INSERT OR IGNORE INTO users
        (id,name)
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

    con=db()

    r=con.execute(
        "SELECT balance FROM users WHERE id=?",
        (uid,)
    ).fetchone()

    con.close()


    if r:

        return float(r["balance"])

    return 0



# ==============================
# SAFE MONEY CHANGE
# ==============================


def change_money(
        uid,
        amount,
        typ
):

    con=db()

    try:

        con.execute(
            "BEGIN"
        )


        tx=secrets.token_hex(16)


        old=con.execute(
            "SELECT balance FROM users WHERE id=?",
            (uid,)
        ).fetchone()


        if not old:

            con.execute(
                """
                INSERT INTO users(id,balance)
                VALUES(?,0)
                """,
                (uid,)
            )

            old_balance=0

        else:

            old_balance=float(
                old["balance"]
            )


        new=old_balance+amount


        if new < 0:

            con.rollback()

            return False



        con.execute(
            """
            UPDATE users
            SET balance=?
            WHERE id=?
            """,
            (
                new,
                uid
            )
        )



        con.execute(
            """
            INSERT INTO transactions
            (tx,user,amount,type,time)

            VALUES(?,?,?,?,?)
            """,
            (
                tx,
                uid,
                amount,
                typ,
                str(datetime.now())
            )
        )


        con.commit()

        return True


    except Exception:


        con.rollback()

        return False


    finally:

        con.close()



# ==============================
# START
# ==============================


async def start(
        update:Update,
        context:ContextTypes.DEFAULT_TYPE
):

    add_user(
        update.effective_user
    )


    await update.message.reply_text(
        "✅ ربات روشن شد\n"
        "💰 موجودی شما ذخیره می‌شود."
    )



def main():

    if not BOT_TOKEN:

        raise Exception(
            "BOT_TOKEN نیست"
        )


    init_db()


    app=Application.builder().token(
        BOT_TOKEN
    ).build()



    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )



    app.run_polling()



if __name__=="__main__":

    main()
    # ==============================
# BOT.PY PART 2/4
# GAME SYSTEM
# ==============================

import re
import random


active_games = {}



# تبدیل اعداد فارسی به انگلیسی

def fa_to_en(text):

    nums = "۰۱۲۳۴۵۶۷۸۹"

    for i,n in enumerate(nums):

        text = text.replace(
            n,
            str(i)
        )

    return text



# تشخیص بازی

def get_game(text):

    text = fa_to_en(
        text.lower()
    )


    games = {

        "تاس":"dice",
        "بولینگ":"bowling",
        "دارت":"dart",
        "بسکتبال":"basketball"

    }


    for name,value in games.items():

        if name in text:

            return value


    return None



# گرفتن تعداد بازی و مبلغ

def parse_bet(text):

    text = fa_to_en(
        text.lower()
    )


    game=get_game(text)


    if not game:

        return None



    nums=re.findall(
        r"\d+(?:\.\d+)?",
        text
    )


    if len(nums)<2:

        return None



    count=int(
        float(nums[0])
    )


    bet=float(
        nums[1]
    )


    return game,count,bet



# شروع بازی

async def start_game(
        update,
        context
):

    user=update.effective_user

    text=update.message.text


    data=parse_bet(text)


    if not data:

        return


    game,count,bet=data



    total=bet


    if balance(user.id)<total:

        await update.message.reply_text(
            "❌ موجودی کافی نیست"
        )

        return



    change_money(
        user.id,
        -total,
        "game_bet"
    )



    game_id=secrets.token_hex(8)



    active_games[game_id]={

        "game":game,

        "owner":user.id,

        "bet":bet,

        "count":count,

        "turn":user.id

    }



    await update.message.reply_text(

        f"🎮 بازی ساخته شد\n\n"
        f"بازی: {game}\n"
        f"تعداد: {count}\n"
        f"شرط: {bet} TRX\n\n"
        f"اول بازیکن ایموجی بازی را بفرستد"

    )



# اجرای پرتاب

async def play_roll(
        update,
        context
):


    if not update.message.dice:

        return



    user=update.effective_user



    for gid,g in active_games.items():


        if g["turn"]!=user.id:

            continue



        score=update.message.dice.value



        g["score1"]=score



        await update.message.reply_text(

            f"🎯 امتیاز شما: {score}\n"
            "⏳ نوبت بعدی"

        )


        return



# انتخاب بازی

async def choose_game(
        update,
        context
):


    txt=update.message.text


    if txt in [

        "🎲 تاس",

        "🎳 بولینگ",

        "🎯 دارت",

        "🏀 بسکتبال"

    ]:


        await update.message.reply_text(

            "مثال:\n"
            "1 تاس 0.1\n"
            "۱ تاس ۰.۱"

        )



# اضافه کردن هندلرها

# این قسمت را داخل main قسمت قبل از run_polling اضافه می‌کنیم:
#
# app.add_handler(
# MessageHandler(
# filters.TEXT,
# start_game
# ))
#
# app.add_handler(
# MessageHandler(
# filters.Dice.ALL,
# play_roll
# ))

# ==============================
# BOT.PY PART 3/4
# FRIEND + BOT GAME
# ==============================


async def bot_game_result(
        update,
        context,
        game_id
):

    game = active_games.get(game_id)

    if not game:
        return


    user = update.effective_user


    player_score = random.randint(
        1,
        6
    )

    bot_score = random.randint(
        1,
        6
    )


    bet = game["bet"]



    if player_score > bot_score:


        reward = round(
            bet * 1.8,
            2
        )


        change_money(
            user.id,
            reward,
            "game_win"
        )


        text = (
            "🤖 نتیجه بازی\n\n"
            f"👤 شما: {player_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            f"🏆 برنده شدید\n"
            f"💰 جایزه: {reward} TRX"
        )


    elif player_score < bot_score:


        text = (
            "🤖 نتیجه بازی\n\n"
            f"👤 شما: {player_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            "❌ باختید"
        )


    else:


        change_money(
            user.id,
            bet,
            "game_draw"
        )


        text = (
            "🤝 مساوی شد\n\n"
            f"👤 شما: {player_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            "🔄 مبلغ برگشت داده شد"
        )


    del active_games[game_id]


    await update.message.reply_text(
        text
    )





# ==============================
# FRIEND GAME
# ==============================


async def join_game(
        update,
        context
):

    query = update.callback_query

    await query.answer()


    gid=query.data.split(":")[1]


    game=active_games.get(gid)



    if not game:

        await query.message.reply_text(
            "❌ بازی پیدا نشد"
        )

        return



    user=query.from_user



    if game["owner"]==user.id:

        await query.message.reply_text(
            "❌ خودت نمی‌توانی وارد شوی"
        )

        return



    if balance(user.id)<game["bet"]:


        await query.message.reply_text(
            "❌ موجودی کافی نیست"
        )

        return



    change_money(
        user.id,
        -game["bet"],
        "friend_bet"
    )



    game["player2"]=user.id

    game["turn"]="player1"



    await query.message.reply_text(

        "✅ بازیکن وارد شد\n"
        "🎮 نوبت سازنده بازی است"

    )





async def friend_roll(
        update,
        context
):

    user=update.effective_user



    for gid,g in active_games.items():


        if g.get("player2")==user.id or g.get("owner")==user.id:


            score=update.message.dice.value



            if g.get("player1_score") is None:


                g["player1_score"]=score


                await update.message.reply_text(
                    "⏳ نوبت نفر دوم"
                )


                return



            else:


                g["player2_score"]=score



                a=g["player1_score"]

                b=g["player2_score"]



                if a==b:


                    g["player1_score"]=None

                    g["player2_score"]=None


                    await update.message.reply_text(
                        "🤝 مساوی شد\n🔄 دوباره بندازید"
                    )

                    return




                if a>b:

                    winner=g["owner"]


                else:

                    winner=g["player2"]




                prize=round(
                    g["bet"]*1.8,
                    2
                )



                change_money(
                    winner,
                    prize,
                    "friend_win"
                )



                await update.message.reply_text(

                    "🏆 بازی تمام شد\n\n"
                    f"نتیجه: {a} - {b}\n"
                    f"💰 جایزه: {prize} TRX"

                )



                del active_games[gid]


                return
                # ==============================
# BOT.PY PART 4/4
# TRANSFER + WITHDRAW + ADMIN + MAIN
# ==============================


from telegram import InlineKeyboardButton, InlineKeyboardMarkup



# انتقال با Reply

async def transfer(update, context):

    msg = update.message
    user = update.effective_user


    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ روی پیام کاربر Reply کنید"
        )
        return



    try:

        amount=float(
            msg.text.split()[1]
        )

    except:

        await msg.reply_text(
            "❌ مثال:\nانتقال 0.1"
        )
        return



    target=msg.reply_to_message.from_user


    if balance(user.id)<amount:

        await msg.reply_text(
            "❌ موجودی کافی نیست"
        )
        return



    change_money(
        user.id,
        -amount,
        "transfer_out"
    )


    change_money(
        target.id,
        amount,
        "transfer_in"
    )



    await msg.reply_text(
        "✅ انتقال انجام شد"
    )





# برداشت

async def withdraw(update,context):


    user=update.effective_user


    await update.message.reply_text(

        "💸 مقدار برداشت را بفرستید\n"
        "حداقل: 2 TRX"

    )


    context.user_data["withdraw"]=True





async def withdraw_amount(update,context):


    if not context.user_data.get("withdraw"):

        return


    user=update.effective_user


    try:

        amount=float(
            update.message.text
        )

    except:

        return



    if amount<2:

        await update.message.reply_text(
            "❌ حداقل 2 TRX"
        )
        return



    if balance(user.id)<amount:

        await update.message.reply_text(
            "❌ موجودی کافی نیست"
        )

        return



    context.user_data["amount"]=amount
    context.user_data["withdraw_address"]=True
    context.user_data["withdraw"]=False



    await update.message.reply_text(
        "📥 آدرس TRC20 را بفرست"
    )





# پنل مدیریت


async def admin(update,context):


    if update.effective_user.id not in ADMIN_IDS:

        return



    kb=InlineKeyboardMarkup([

        [
        InlineKeyboardButton(
            "💰 موجودی کاربران",
            callback_data="users"
        )
        ],

        [
        InlineKeyboardButton(
            "🟢 روشن",
            callback_data="on"
        ),

        InlineKeyboardButton(
            "🔴 خاموش",
            callback_data="off"
        )
        ],

        [
        InlineKeyboardButton(
            "➕ افزایش موجودی",
            callback_data="add"
        ),

        InlineKeyboardButton(
            "➖ کسر موجودی",
            callback_data="sub"
        )
        ]

    ])



    await update.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=kb
    )





# اتصال همه چیز


def main():


    init_db()


    app=Application.builder().token(
        BOT_TOKEN
    ).build()



    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )



    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )



    app.add_handler(
        MessageHandler(
            filters.TEXT,
            start_game
        )
    )



    app.add_handler(
        MessageHandler(
            filters.TEXT,
            transfer
        )
    )



    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            friend_roll
        )
    )



    app.add_handler(
        MessageHandler(
            filters.TEXT,
            withdraw_amount
        )
    )



    print(
        "BOT STARTED"
    )


    app.run_polling()




if __name__=="__main__":

    main()
