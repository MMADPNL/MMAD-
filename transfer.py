# =========================================================
# BET BOT - TRANSFER
# انتقال موجودی داخل گروه
# =========================================================

from database import get_user, get_balance, change_balance
from utils import parse_amount, normalize_text


def parse_transfer_command(text: str):
    """
    فرمت‌های قابل قبول:

    انتقال 0.1
    انتقال ۰.۱
    """

    if not text:
        return None

    text = normalize_text(text)

    parts = text.split()

    if len(parts) != 2:
        return None

    command = parts[0]
    amount_text = parts[1]

    if command != "انتقال":
        return None

    amount = parse_amount(amount_text)

    if amount is None:
        return None

    return amount


def can_transfer(sender_id: int, receiver_id: int, amount: float):
    """
    بررسی شرایط انتقال
    """

    if sender_id == receiver_id:
        return False, "نمی‌توانید به خودتان انتقال دهید."

    if amount <= 0:
        return False, "مبلغ انتقال باید بیشتر از صفر باشد."

    sender = get_user(sender_id)

    if not sender:
        return False, "حساب شما پیدا نشد."

    balance = get_balance(sender_id)

    if balance < amount:
        return False, "موجودی کافی نیست."

    return True, ""


def transfer_balance(
    sender_id: int,
    receiver_id: int,
    amount: float
):
    """
    انتقال موجودی.

    ابتدا از فرستنده کم می‌شود،
    سپس به گیرنده اضافه می‌شود.

    اگر مرحله دوم شکست بخورد،
    مبلغ فرستنده برگردانده می‌شود.
    """

    amount = round(float(amount), 8)

    valid, error = can_transfer(
        sender_id,
        receiver_id,
        amount
    )

    if not valid:
        raise ValueError(error)

    # کم کردن از فرستنده
    change_balance(
        sender_id,
        -amount,
        "transfer_sent",
        receiver_id
    )

    try:
        # اضافه کردن به گیرنده
        change_balance(
            receiver_id,
            amount,
            "transfer_received",
            sender_id
        )

    except Exception:
        # برگشت مبلغ در صورت خطا
        change_balance(
            sender_id,
            amount,
            "transfer_rollback",
            receiver_id
        )

        raise

    return True
