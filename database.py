# =========================================================
# BET BOT - DATABASE
# SQLite persistent database
# =========================================================

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DATABASE


@contextmanager
def get_db():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as db:

        # کاربران
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                referral_paid INTEGER NOT NULL DEFAULT 0,
                captcha_ok INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # درخواست‌های واریز
        db.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                proof TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT DEFAULT NULL,
                reviewed_by INTEGER DEFAULT NULL
            )
        """)

        # درخواست‌های برداشت
        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT DEFAULT NULL,
                reviewed_by INTEGER DEFAULT NULL
            )
        """)

        # تیکت‌های پشتیبانی
        db.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT DEFAULT '',
                telegram_message_id INTEGER DEFAULT NULL,
                admin_message_id INTEGER DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            )
        """)

        # تراکنش‌های موجودی
        db.execute("""
            CREATE TABLE IF NOT EXISTS balance_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                balance_before REAL NOT NULL,
                balance_after REAL NOT NULL,
                action TEXT NOT NULL,
                reference_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # تنظیمات ربات
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def create_user(
    user_id: int,
    username: str = "",
    first_name: str = "",
    referred_by: int | None = None
):
    current = now()

    with get_db() as db:
        user = db.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if user:
            db.execute("""
                UPDATE users
                SET username = ?,
                    first_name = ?,
                    updated_at = ?
                WHERE user_id = ?
            """, (
                username or "",
                first_name or "",
                current,
                user_id
            ))
            return

        # جلوگیری از ثبت خود فرد به عنوان معرف
        if referred_by == user_id:
            referred_by = None

        # اگر معرف وجود نداشته باشد، ذخیره نمی‌کنیم
        if referred_by is not None:
            ref_exists = db.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (referred_by,)
            ).fetchone()

            if not ref_exists:
                referred_by = None

        db.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                balance,
                referred_by,
                referral_paid,
                captcha_ok,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 0, ?, 0, 0, ?, ?)
        """, (
            user_id,
            username or "",
            first_name or "",
            referred_by,
            current,
            current
        ))


def get_user(user_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()


def get_balance(user_id: int) -> float:
    user = get_user(user_id)

    if not user:
        return 0.0

    return float(user["balance"])


def set_captcha_ok(user_id: int, value: bool = True):
    with get_db() as db:
        db.execute("""
            UPDATE users
            SET captcha_ok = ?,
                updated_at = ?
            WHERE user_id = ?
        """, (
            1 if value else 0,
            now(),
            user_id
        ))


def captcha_is_ok(user_id: int) -> bool:
    user = get_user(user_id)

    if not user:
        return False

    return bool(user["captcha_ok"])


def change_balance(
    user_id: int,
    amount: float,
    action: str,
    reference_id: int | None = None
):
    """
    تغییر موجودی با ثبت کامل لاگ.
    مقدار منفی در صورت کم شدن موجودی استفاده می‌شود.
    """

    amount = round(float(amount), 8)

    with get_db() as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            raise ValueError("User does not exist")

        before = round(float(row["balance"]), 8)
        after = round(before + amount, 8)

        # موجودی هیچ‌وقت منفی نمی‌شود
        if after < 0:
            raise ValueError("Insufficient balance")

        db.execute("""
            UPDATE users
            SET balance = ?,
                updated_at = ?
            WHERE user_id = ?
        """, (
            after,
            now(),
            user_id
        ))

        db.execute("""
            INSERT INTO balance_logs (
                user_id,
                amount,
                balance_before,
                balance_after,
                action,
                reference_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            amount,
            before,
            after,
            action,
            reference_id,
            now()
        ))

        return before, after


def create_deposit(
    user_id: int,
    amount: float,
    method: str,
    proof: str = ""
):
    with get_db() as db:
        cursor = db.execute("""
            INSERT INTO deposits (
                user_id,
                amount,
                method,
                proof,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (
            user_id,
            round(float(amount), 8),
            method,
            proof,
            now()
        ))

        return cursor.lastrowid


def get_deposit(deposit_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM deposits WHERE id = ?",
            (deposit_id,)
        ).fetchone()


def update_deposit_status(
    deposit_id: int,
    status: str,
    reviewed_by: int
):
    with get_db() as db:
        db.execute("""
            UPDATE deposits
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?
            WHERE id = ?
        """, (
            status,
            now(),
            reviewed_by,
            deposit_id
        ))


def create_withdrawal(
    user_id: int,
    amount: float,
    wallet: str
):
    with get_db() as db:
        cursor = db.execute("""
            INSERT INTO withdrawals (
                user_id,
                amount,
                wallet,
                status,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?)
        """, (
            user_id,
            round(float(amount), 8),
            wallet,
            now()
        ))

        return cursor.lastrowid


def get_withdrawal(withdrawal_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM withdrawals WHERE id = ?",
            (withdrawal_id,)
        ).fetchone()


def update_withdrawal_status(
    withdrawal_id: int,
    status: str,
    reviewed_by: int
):
    with get_db() as db:
        db.execute("""
            UPDATE withdrawals
            SET status = ?,
                reviewed_at = ?,
                reviewed_by = ?
            WHERE id = ?
        """, (
            status,
            now(),
            reviewed_by,
            withdrawal_id
        ))


def create_support_message(
    user_id: int,
    message_type: str,
    content: str = "",
    telegram_message_id: int | None = None
):
    with get_db() as db:
        cursor = db.execute("""
            INSERT INTO support_messages (
                user_id,
                message_type,
                content,
                telegram_message_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'open', ?)
        """, (
            user_id,
            message_type,
            content,
            telegram_message_id,
            now()
        ))

        return cursor.lastrowid


def set_support_admin_message(
    support_id: int,
    admin_message_id: int
):
    with get_db() as db:
        db.execute("""
            UPDATE support_messages
            SET admin_message_id = ?
            WHERE id = ?
        """, (
            admin_message_id,
            support_id
        ))


def get_support_message(support_id: int):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM support_messages WHERE id = ?",
            (support_id,)
        ).fetchone()


def get_setting(key: str, default=None):
    with get_db() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()

        if not row:
            return default

        return row["value"]


def set_setting(key: str, value: str):
    with get_db() as db:
        db.execute("""
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (
            key,
            str(value)
        ))
