# =========================================================
# BET BOT - KEYBOARDS
# =========================================================

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram import ReplyKeyboardMarkup


# =========================================================
# منوی اصلی
# =========================================================

def main_menu():
    keyboard = [
        ["💰 موجودی", "➕ واریز"],
        ["➖ برداشت", "🎮 مثال بازی"],
        ["👥 زیرمجموعه", "🆘 پشتیبانی"],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )


# =========================================================
# پنل مدیریت
# =========================================================

def admin_menu():
    keyboard = [
        ["👥 کاربران", "💰 آمار موجودی"],
        ["👤 اضافه کردن ادمین", "❌ حذف ادمین"],
        ["🟢 روشن کردن ربات", "🔴 خاموش کردن ربات"],
        ["📥 درخواست‌های واریز"],
        ["📤 درخواست‌های برداشت"],
        ["🆘 پیام‌های پشتیبانی"],
        ["⚙️ تنظیمات"],
        ["🔙 بازگشت"],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )


# =========================================================
# انتخاب روش واریز
# =========================================================

def deposit_methods():
    keyboard = [
        [
            InlineKeyboardButton(
                "TON",
                callback_data="deposit_ton"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 کارتی",
                callback_data="deposit_card"
            )
        ],
        [
            InlineKeyboardButton(
                "TRON",
                callback_data="deposit_tron"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="deposit_cancel"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# بازی
# =========================================================

def game_start_buttons(game: str, amount: float):
    keyboard = [
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"game_friend:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"game_bot:{game}:{amount}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"game_cancel:{game}"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# پیوستن به بازی دوستان
# =========================================================

def friend_game_buttons(game_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "🎮 پیوستن به بازی",
                callback_data=f"join_game:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel_game:{game_id}"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# تأیید / رد درخواست
# =========================================================

def request_review_buttons(kind: str, request_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"approve:{kind}:{request_id}"
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"reject:{kind}:{request_id}"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# پاسخ به کاربر
# =========================================================

def reply_to_user_button(user_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "↩️ پاسخ به کاربر",
                callback_data=f"support_reply:{user_id}"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# کپچا
# =========================================================

def captcha_buttons(options):
    keyboard = []

    for option in options:
        keyboard.append([
            InlineKeyboardButton(
                str(option),
                callback_data=f"captcha:{option}"
            )
        ])

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# درخواست برداشت
# =========================================================

def withdrawal_review_buttons(withdrawal_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید برداشت",
                callback_data=f"approve:withdrawal:{withdrawal_id}"
            ),
            InlineKeyboardButton(
                "❌ رد برداشت",
                callback_data=f"reject:withdrawal:{withdrawal_id}"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# درخواست واریز
# =========================================================

def deposit_review_buttons(deposit_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید واریز",
                callback_data=f"approve:deposit:{deposit_id}"
            ),
            InlineKeyboardButton(
                "❌ رد واریز",
                callback_data=f"reject:deposit:{deposit_id}"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# مدیریت کاربر
# =========================================================

def user_management_buttons(user_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data=f"admin_add:{user_id}"
            ),
            InlineKeyboardButton(
                "➖ کاهش موجودی",
                callback_data=f"admin_remove:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 موجودی کاربر",
                callback_data=f"admin_balance:{user_id}"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# پشتیبانی
# =========================================================

def support_admin_buttons(user_id: int, support_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "↩️ پاسخ به کاربر",
                callback_data=f"support_reply:{user_id}:{support_id}"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# تأیید عملیات
# =========================================================

def confirm_buttons(action: str):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"confirm:{action}"
            ),
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{action}"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# کنترل روشن / خاموش ربات
# =========================================================

def bot_status_buttons():
    keyboard = [
        [
            InlineKeyboardButton(
                "🟢 روشن کردن",
                callback_data="bot_enable"
            ),
            InlineKeyboardButton(
                "🔴 خاموش کردن",
                callback_data="bot_disable"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# تأیید حذف ادمین
# =========================================================

def remove_admin_confirm(admin_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ بله، حذف شود",
                callback_data=f"remove_admin_confirm:{admin_id}"
            ),
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="remove_admin_cancel"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# تأیید اضافه کردن ادمین
# =========================================================

def add_admin_confirm(admin_id: int):
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ اضافه شود",
                callback_data=f"add_admin_confirm:{admin_id}"
            ),
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="add_admin_cancel"
            ),
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# لیست مدیریت ادمین
# =========================================================

def admin_action_buttons():
    keyboard = [
        [
            InlineKeyboardButton(
                "👤 اضافه کردن ادمین",
                callback_data="admin_add_start"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ حذف ادمین",
                callback_data="admin_remove_start"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 بازگشت",
                callback_data="admin_back"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)
