# =========================================================
# BET BOT - WITHDRAWALS
# =========================================================

from config import MIN_WITHDRAW
from database import (
    create_withdrawal,
    get_withdrawal,
    update_withdrawal_status,
    get_balance,
)


def validate_withdrawal_amount(value):
    try:
        amount = float(str(value).replace("۰", "0"))
    except (TypeError, ValueError):
        return False, None, "مبلغ واردشده معتبر نیست."

    amount = round(amount, 8)

    if amount < MIN_WITHDRAW:
        return (
            False,
            None,
            f"حداقل برداشت {MIN_WITHDRAW} TRX است."
        )

    if amount <= 0:
        return False, None, "مبلغ باید بیشتر از صفر باشد."

    return True, amount, ""


def can_withdraw(user_id: int, amount: float):
    balance = get_balance(user_id)

    if balance < amount:
        return False, "موجودی کافی نیست."

    return True, ""


def create_withdrawal_request(
    user_id: int,
    amount,
    wallet: str
):
    valid, parsed_amount, error = validate_withdrawal_amount(
        amount
    )

    if not valid:
        raise ValueError(error)

    if not wallet or not wallet.strip():
        raise ValueError("ولت معتبر نیست.")

    valid, error = can_withdraw(
        user_id,
        parsed_amount
    )

    if not valid:
        raise ValueError(error)

    return create_withdrawal(
        user_id=user_id,
        amount=parsed_amount,
        wallet=wallet.strip()
    )


def get_withdrawal_request(withdrawal_id: int):
    return get_withdrawal(withdrawal_id)


def approve_withdrawal(
    withdrawal_id: int,
    admin_id: int
):
    withdrawal = get_withdrawal(
        withdrawal_id
    )

    if not withdrawal:
        return False, "درخواست پیدا نشد."

    if withdrawal["status"] != "pending":
        return False, "این درخواست قبلاً بررسی شده است."

    update_withdrawal_status(
        withdrawal_id,
        "approved",
        admin_id
    )

    return True, "برداشت تأیید شد."


def reject_withdrawal(
    withdrawal_id: int,
    admin_id: int
):
    withdrawal = get_withdrawal(
        withdrawal_id
    )

    if not withdrawal:
        return False, "درخواست پیدا نشد."

    if withdrawal["status"] != "pending":
        return False, "این درخواست قبلاً بررسی شده است."

    update_withdrawal_status(
        withdrawal_id,
        "rejected",
        admin_id
    )

    return True, "برداشت رد شد."
