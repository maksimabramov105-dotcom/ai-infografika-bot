"""SQLite database for user credits, transactions and analytics."""

import aiosqlite
import os
import logging
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/tmp/users.db")


async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                free_infographics_left INTEGER DEFAULT 3,
                paid_credits INTEGER DEFAULT 0,
                analytics_credits INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount_rub INTEGER DEFAULT 0,
                credits_bought INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                type TEXT NOT NULL,
                result TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()
    logging.info("Database initialized.")


async def get_or_create_user(user_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
        # New user
        import secrets
        ref_code = secrets.token_hex(4).upper()
        await db.execute(
            "INSERT INTO users (user_id, referral_code) VALUES (?, ?)",
            (user_id, ref_code),
        )
        await db.commit()
        return {
            "user_id": user_id,
            "free_infographics_left": 3,
            "paid_credits": 0,
            "analytics_credits": 0,
            "referral_code": ref_code,
            "referred_by": None,
        }


async def log_transaction(user_id: int, tx_type: str, amount_rub: int, credits: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, type, amount_rub, credits_bought) VALUES (?, ?, ?, ?)",
            (user_id, tx_type, amount_rub, credits),
        )
        await db.commit()


async def log_analysis(user_id: int, query: str, analysis_type: str, result: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO analyses (user_id, query, type, result) VALUES (?, ?, ?, ?)",
            (user_id, query, analysis_type, result),
        )
        await db.commit()


async def add_analytics_credits(user_id: int, credits: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET analytics_credits = analytics_credits + ? WHERE user_id = ?",
            (credits, user_id),
        )
        await db.commit()


async def use_analytics_credit(user_id: int) -> bool:
    """Deduct 1 analytics credit. Returns True if successful."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT analytics_credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[0] <= 0:
            return False
        await db.execute(
            "UPDATE users SET analytics_credits = analytics_credits - 1 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
        return True


async def get_analytics_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT analytics_credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0
