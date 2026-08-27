# =========================================================
# BET BOT - CONFIG
# =========================================================

import os

# توکن ربات را در GitHub Secrets با نام BOT_TOKEN قرار می‌دهیم
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# آیدی عددی مالک ربات
OWNER_ID = 8552447077

# گروه اجباری
FORCE_GROUP = "@zobxt"

# حداقل واریز داخلی
MIN_DEPOSIT = 0.5

# حداقل برداشت داخلی
MIN_WITHDRAW = 2.5

# پاداش زیرمجموعه
REFERRAL_REWARD = 0.05

# سهم سیستم در هر بازی
OWNER_GAME_FEE = 0.02

# نام واحد موجودی
CURRENCY = "TRX"

# فایل دیتابیس
DATABASE = "bot.db"


def get_admin_ids():
    """
    آیدی ادمین‌ها را از متغیر محیطی می‌خواند.
    مثال:
    ADMIN_IDS=123456789,987654321
    """
    raw = os.getenv("ADMIN_IDS", "")

    result = []

    for item in raw.split(","):
        item = item.strip()

        if item.isdigit():
            result.append(int(item))

    # مالک همیشه ادمین محسوب می‌شود
    if OWNER_ID not in result:
        result.append(OWNER_ID)

    return result
