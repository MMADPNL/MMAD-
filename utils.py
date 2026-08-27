# =========================================================
# BET BOT - UTILS
# ابزارهای عمومی
# =========================================================

import re
import random


# ---------------------------------------------------------
# تبدیل اعداد فارسی و عربی به انگلیسی
# ---------------------------------------------------------

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
ENGLISH_DIGITS = "0123456789"


def normalize_digits(text: str) -> str:
    if not text:
        return ""

    table = str.maketrans(
        PERSIAN_DIGITS + ARABIC_DIGITS,
        ENGLISH_DIGITS + ENGLISH_DIGITS
    )

    return text.translate(table)


# ---------------------------------------------------------
# تبدیل اعداد و حذف فاصله‌های اضافی
# ---------------------------------------------------------

def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = normalize_digits(text)
    text = text.strip()
    text = re.sub(r"\s+", " ", text)

    return text


# ---------------------------------------------------------
# تبدیل مبلغ
# مثال:
# 0.1
# ۰.۱
# 1
# ۱
# ---------------------------------------------------------

def parse_amount(value: str):
    if not value:
        return None

    value = normalize_digits(value)
    value = value.strip()
    value = value.replace(",", ".")
    value = value.replace("٫", ".")

    # فقط عدد مثبت
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None

    try:
        amount = float(value)
    except ValueError:
        return None

    if amount <= 0:
        return None

    return round(amount, 8)


# ---------------------------------------------------------
# فرمت موجودی
# ---------------------------------------------------------

def format_amount(amount: float) -> str:
    amount = round(float(amount), 8)

    if amount.is_integer():
        return f"{int(amount)}"

    text = f"{amount:.8f}".rstrip("0").rstrip(".")

    return text


def format_trx(amount: float) -> str:
    return f"{format_amount(amount)} TRX"


# ---------------------------------------------------------
# ساخت شناسه امن تصادفی
# ---------------------------------------------------------

def random_code(length: int = 6) -> str:
    digits = "0123456789"

    return "".join(
        random.choice(digits)
        for _ in range(length)
    )


# ---------------------------------------------------------
# کپچا
# ---------------------------------------------------------

def create_captcha():
    """
    یک کپچای ساده جمع ایجاد می‌کند.

    خروجی:
    question, answer, options

    مثال:
    3 + 4 = ?
    answer = 7
    options = [7, 5, 9, 2]
    """

    a = random.randint(1, 9)
    b = random.randint(1, 9)

    answer = a + b

    options = {answer}

    while len(options) < 4:
        options.add(
            random.randint(2, 18)
        )

    options = list(options)

    random.shuffle(options)

    question = f"{a} + {b} = ?"

    return question, answer, options


# ---------------------------------------------------------
# تشخیص دستور بازی
#
# نمونه‌های قابل قبول:
# 1 تاس 0.1
# ۱ تاس ۰.۱
# 2 بولینگ 1
# ۱ دارت ۰.۵
# 3 بسکتبال 2
# ---------------------------------------------------------

GAME_ALIASES = {
    "تاس": "dice",
    "dice": "dice",

    "بولینگ": "bowling",
    "بولينگ": "bowling",
    "bowling": "bowling",

    "بسکتبال": "basketball",
    "basketball": "basketball",

    "دارت": "darts",
    "دارتس": "darts",
    "darts": "darts",
    "dart": "darts",
}


def parse_game_command(text: str):
    """
    دستور بازی را بررسی می‌کند.

    فرمت:
    1 تاس 0.1
    ۱ تاس ۰.۱

    خروجی:
    {
        "count": 1,
        "game": "dice",
        "amount": 0.1
    }

    در صورت نامعتبر بودن:
    None
    """

    text = normalize_text(text)

    parts = text.split()

    if len(parts) != 3:
        return None

    count_text, game_text, amount_text = parts

    # تعداد بازی/پرتاب
    if not count_text.isdigit():
        return None

    count = int(count_text)

    if count <= 0:
        return None

    # نوع بازی
    game_text = game_text.lower()

    game = GAME_ALIASES.get(game_text)

    if not game:
        return None

    # مبلغ
    amount = parse_amount(amount_text)

    if amount is None:
        return None

    return {
        "count": count,
        "game": game,
        "amount": amount
    }


# ---------------------------------------------------------
# تبدیل نام بازی برای نمایش
# ---------------------------------------------------------

GAME_NAMES = {
    "dice": "🎲 تاس",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت",
}


def game_name(game: str) -> str:
    return GAME_NAMES.get(
        game,
        "🎮 بازی"
    )


# ---------------------------------------------------------
# بررسی مقدار معتبر برای بازی
# ---------------------------------------------------------

def valid_game_amount(amount: float) -> bool:
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False

    return amount > 0


# ---------------------------------------------------------
# امن کردن متن برای نمایش
# ---------------------------------------------------------

def safe_text(text: str, max_length: int = 4000) -> str:
    if not text:
        return ""

    text = str(text)

    if len(text) > max_length:
        text = text[:max_length] + "..."

    return text


# ---------------------------------------------------------
# تشخیص اینکه پیام ریپلای است یا نه
# ---------------------------------------------------------

def get_replied_user_id(message):
    if not message:
        return None

    reply = message.reply_to_message

    if not reply:
        return None

    if not reply.from_user:
        return None

    return reply.from_user.id


# ---------------------------------------------------------
# فرمت نام کاربر
# ---------------------------------------------------------

def user_display_name(user) -> str:
    if not user:
        return "کاربر"

    if getattr(user, "username", None):
        return f"@{user.username}"

    first_name = getattr(user, "first_name", None)

    if first_name:
        return first_name

    return str(user.id)


# ---------------------------------------------------------
# تشخیص لینک یا هش تراکنش
# ---------------------------------------------------------

def looks_like_transaction_proof(text: str) -> bool:
    if not text:
        return False

    text = text.strip()

    # لینک
    if text.startswith("http://") or text.startswith("https://"):
        return True

    # هش‌های طولانی
    if re.fullmatch(r"[A-Za-z0-9_-]{20,200}", text):
        return True

    return False
