# =========================================================
# BET BOT - DEPOSITS
# مدیریت درخواست‌های واریز
# =========================================================

from config import MIN_DEPOSIT
from database import (
    create_deposit,
    get_deposit,
    update_deposit_status,
)
from utils import parse_amount


# =========================================================
# بررسی مبلغ واریز
# =========================================================

def validate_deposit_amount(value):
    amount = parse_amount(str(value))

    if amount is None:
        return False, None, "مبلغ واردشده معتبر نیست."

    if amount < MIN_DEPOSIT:
        return (
            False,
            None,
            f"حداقل واریز {MIN_DEPOSIT} TRX است."
        )

    return True, amount, ""


# =========================================================
# ساخت درخواست واریز
# =========================================================

def create_deposit_request(
    user_id: int,
    amount,
    method: str,
    proof: str = ""
):
    valid, parsed_amount, error = validate_deposit_amount(
        amount
    )

    if not valid:
        raise ValueError(error)

    allowed_methods = {
        "ton",
        "card",
        "tron",
    }

    if method not in allowed_methods:
        raise ValueError(
            "روش واریز معتبر نیست."
        )

    return create_deposit(
        user_id=user_id,
        amount=parsed_amount,
        method=method,
        proof=proof
    )


# =========================================================
# دریافت درخواست
# =========================================================

def get_deposit_request(deposit_id: int):
    return get_deposit(deposit_id)


# =========================================================
# تأیید درخواست
# =========================================================

def approve_deposit(
    deposit_id: int,
    admin_id: int
):
    deposit = get_deposit(
        deposit_id
    )

    if not deposit:
        return False, "درخواست پیدا نشد."

    if deposit["status"] != "pending":
        return False, "این درخواست قبلاً بررسی شده است."

    update_deposit_status(
        deposit_id,
        "approved",
        admin_id
    )

    return True, "واریز تأیید شد."


# =========================================================
# رد درخواست
# =========================================================

def reject_deposit(
    deposit_id: int,
    admin_id: int
):
    deposit = get_deposit(
        deposit_id
    )

    if not deposit:
        return False, "درخواست پیدا نشد."

    if deposit["status"] != "pending":
        return False, "این درخواست قبلاً بررسی شده است."

    update_deposit_status(
        deposit_id,
        "rejected",
        admin_id
    )

    return True, "واریز رد شد."


# =========================================================
# نام روش واریز
# =========================================================

def method_name(method: str):

    names = {
        "ton": "TON",
        "card": "💳 کارتی",
        "tron": "TRON",
    }

    return names.get(
        method,
        method
  )
