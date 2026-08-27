# =========================================================
# BET BOT - REFERRALS
# =========================================================

from config import REFERRAL_REWARD
from database import get_db, get_user, change_balance, now


def make_referral_link(bot_username: str, user_id: int) -> str:
    """
    ساخت لینک اختصاصی زیرمجموعه
    """
    bot_username = bot_username.lstrip("@")

    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def get_referrer(user_id: int):
    """
    دریافت معرف کاربر
    """
    user = get_user(user_id)

    if not user:
        return None

    return user["referred_by"]


def get_referral_count(user_id: int) -> int:
    """
    تعداد زیرمجموعه‌های مستقیم
    """

    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM users
            WHERE referred_by = ?
            """,
            (user_id,)
        ).fetchone()

        return int(row["count"])


def get_referrals(user_id: int):
    """
    دریافت لیست زیرمجموعه‌ها
    """

    with get_db() as db:
        return db.execute(
            """
            SELECT user_id, username, first_name, created_at
            FROM users
            WHERE referred_by = ?
            ORDER BY created_at DESC
            """,
            (user_id,)
        ).fetchall()


def referral_reward_paid(user_id: int) -> bool:
    """
    بررسی اینکه پاداش زیرمجموعه قبلاً پرداخت شده یا نه
    """

    user = get_user(user_id)

    if not user:
        return False

    return bool(user["referral_paid"])


def pay_referral_reward(user_id: int):
    """
    پرداخت یک‌باره پاداش زیرمجموعه به معرف.
    """

    with get_db() as db:

        user = db.execute(
            """
            SELECT referred_by, referral_paid
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            return False

        referred_by = user["referred_by"]
        already_paid = bool(user["referral_paid"])

        # معرف ندارد
        if not referred_by:
            return False

        # قبلاً پرداخت شده
        if already_paid:
            return False

        # علامت پرداخت را داخل همان تراکنش ثبت می‌کنیم
        db.execute(
            """
            UPDATE users
            SET referral_paid = 1,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                now(),
                user_id
            )
        )

    # پاداش به اعتبار داخلی معرف
    try:
        change_balance(
            referred_by,
            REFERRAL_REWARD,
            "referral_reward",
            user_id
        )
    except Exception:
        # اگر پرداخت انجام نشد، علامت را برمی‌گردانیم
        with get_db() as db:
            db.execute(
                """
                UPDATE users
                SET referral_paid = 0,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    now(),
                    user_id
                )
            )

        return False

    return True
