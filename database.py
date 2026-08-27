# =========================================================
# BET BOT - DATABASE
# Persistent SQLite Database
# =========================================================

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DATABASE, OWNER_ID


# =========================================================
# اتصال به دیتابیس
# =========================================================

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


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


# =========================================================
# ساخت جداول
# =========================================================

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

        # ادمین‌ها
        db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
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

        # پشتیبانی
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

        # لاگ موجودی
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

        # تنظیمات
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # بازی‌های در انتظار
        db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER DEFAULT NULL,
                game TEXT NOT NULL,
                amount REAL NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                chat_id INTEGER NOT NULL,
                message_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # مالک همیشه ادمین است
        db.execute("""
            INSERT OR IGNORE INTO admins (
                user_id,
                added_by,
                created_at
            )
            VALUES (?, ?, ?)
        """, (
            OWNER_ID,
            OWNER_ID,
            now()
        ))

        # وضعیت اولیه ربات
        db.execute("""
            INSERT OR IGNORE INTO settings (
                key,
                value
            )
            VALUES ('bot_enabled', '1')
        """)


# =========================================================
# کاربران
# =========================================================

def create_user(
    user_id: int,
    username: str = "",
    first_name: str = "",
    referred_by=None
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

        if referred_by == user_id:
            referred_by = None

        if referred_by is not None:

            exists = db.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (referred_by,)
            ).fetchone()

            if not exists:
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


def get_users_count() -> int:

    with get_db() as db:

        row = db.execute(
            "SELECT COUNT(*) AS count FROM users"
        ).fetchone()

        return int(row["count"])


def get_total_balance() -> float:

    with get_db() as db:

        row = db.execute(
            "SELECT COALESCE(SUM(balance), 0) AS total FROM users"
        ).fetchone()

        return float(row["total"])


# =========================================================
# کپچا
# =========================================================

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


# =========================================================
# موجودی
# =========================================================

def change_balance(
    user_id: int,
    amount: float,
    action: str,
    reference_id=None
):
    amount = round(float(amount), 8)

    with get_db() as db:

        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if not row:
            raise ValueError("User does not exist")

        before = round(
            float(row["balance"]),
            8
        )

        after = round(
            before + amount,
            8
        )

        if after < 0:
            raise ValueError(
                "Insufficient balance"
            )

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


# =========================================================
# ادمین‌ها
# =========================================================

def is_admin(user_id: int) -> bool:

    if user_id == OWNER_ID:
        return True

    with get_db() as db:

        row = db.execute(
            "SELECT user_id FROM admins WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        return row is not None


def add_admin(
    user_id: int,
    added_by: int
):

    if user_id == OWNER_ID:
        return False

    with get_db() as db:

        exists = db.execute(
            "SELECT user_id FROM admins WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if exists:
            return False

        db.execute("""
            INSERT INTO admins (
                user_id,
                added_by,
                created_at
            )
            VALUES (?, ?, ?)
        """, (
            user_id,
            added_by,
            now()
        ))

        return True


def remove_admin(user_id: int):

    if user_id == OWNER_ID:
        return False

    with get_db() as db:

        cursor = db.execute(
            "DELETE FROM admins WHERE user_id = ?",
            (user_id,)
        )

        return cursor.rowcount > 0


def get_admins():

    with get_db() as db:

        return db.execute("""
            SELECT *
            FROM admins
            ORDER BY created_at ASC
        """).fetchall()


# =========================================================
# روشن / خاموش کردن ربات
# =========================================================

def is_bot_enabled() -> bool:

    value = get_setting(
        "bot_enabled",
        "1"
    )

    return value == "1"


def set_bot_enabled(enabled: bool):

    set_setting(
        "bot_enabled",
        "1" if enabled else "0"
    )


# =========================================================
# تنظیمات
# =========================================================

def get_setting(
    key: str,
    default=None
):

    with get_db() as db:

        row = db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()

        if not row:
            return default

        return row["value"]


def set_setting(
    key: str,
    value: str
):

    with get_db() as db:

        db.execute("""
            INSERT INTO settings (
                key,
                value
            )
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (
            key,
            str(value)
        ))


# =========================================================
# واریز
# =========================================================

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


# =========================================================
# برداشت
# =========================================================

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


# =========================================================
# پشتیبانی
# =========================================================

def create_support_message(
    user_id: int,
    message_type: str,
    content: str = "",
    telegram_message_id=None
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


# =========================================================
# بازی‌ها
# =========================================================

def create_game(
    creator_id: int,
    game: str,
    amount: float,
    mode: str,
    chat_id: int,
    message_id=None
):

    with get_db() as db:

        cursor = db.execute("""
            INSERT INTO games (
                creator_id,
                game,
                amount,
                mode,
                status,
                chat_id,
                message_id,
                created_at
            )
            VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?)
        """, (
            creator_id,
            game,
            round(float(amount), 8),
            mode,
            chat_id,
            message_id,
            now()
        ))

        return cursor.lastrowid


def get_game(game_id: int):

    with get_db() as db:

        return db.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,)
        ).fetchone()


def join_game(
    game_id: int,
    opponent_id: int
):

    with get_db() as db:

        game = db.execute(
            "SELECT * FROM games WHERE id = ?",
            (game_id,)
        ).fetchone()

        if not game:
            return False

        if game["status"] != "waiting":
            return False

        if game["creator_id"] == opponent_id:
            return False

        db.execute("""
            UPDATE games
            SET opponent_id = ?,
                status = 'playing'
            WHERE id = ?
        """, (
            opponent_id,
            game_id
        ))

        return True


def update_game_status(
    game_id: int,
    status: str
):

    with get_db() as db:

        db.execute("""
            UPDATE games
            SET status = ?
            WHERE id = ?
        """, (
            status,
            game_id
        ))
