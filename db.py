"""SQLite database — users, analytics, Stars payments, referrals."""

import aiosqlite
import os
import logging
import secrets
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "/tmp/users.db")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                analytics_credits INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                referral_reward_stars INTEGER DEFAULT 0,
                referral_reward_withdrawn INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id TEXT,
                stars_paid INTEGER DEFAULT 0,
                amount_rub INTEGER DEFAULT 0,
                telegram_payment_id TEXT,
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
            CREATE TABLE IF NOT EXISTS referral_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL,
                purchase_stars INTEGER DEFAULT 0,
                reward_stars INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                stars_amount INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.commit()
    logging.info("Database initialized.")


async def register_user(user_id: int, referrer_id: int | None = None, username: str | None = None) -> bool:
    """Register user if new. Returns True if newly registered."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if row:
            # Update last_active and username
            await db.execute(
                "UPDATE users SET last_active = ?, username = COALESCE(?, username) WHERE user_id = ?",
                (datetime.now(timezone.utc).isoformat(), username, user_id),
            )
            await db.commit()
            return False
        ref_code = secrets.token_hex(4).upper()
        await db.execute(
            "INSERT INTO users (user_id, username, referral_code, referred_by, last_active) VALUES (?,?,?,?,?)",
            (user_id, username, ref_code, referrer_id, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return True


async def get_referrer(user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None


async def add_referral_reward(referrer_id: int, referred_user_id: int, purchase_stars: int, reward_stars: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO referral_rewards (referrer_id, referred_user_id, purchase_stars, reward_stars) VALUES (?,?,?,?)",
            (referrer_id, referred_user_id, purchase_stars, reward_stars),
        )
        await db.execute(
            "UPDATE users SET referral_reward_stars = referral_reward_stars + ? WHERE user_id = ?",
            (reward_stars, referrer_id),
        )
        await db.commit()


async def get_total_referral_reward(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT referral_reward_stars FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


async def get_withdrawable_stars(user_id: int) -> int:
    """Stars earned but not yet withdrawn."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT referral_reward_stars, referral_reward_withdrawn FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return 0
        return max(0, row[0] - row[1])


async def request_withdrawal(user_id: int, stars: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO withdrawal_requests (user_id, stars_amount) VALUES (?,?)",
            (user_id, stars),
        )
        await db.execute(
            "UPDATE users SET referral_reward_withdrawn = referral_reward_withdrawn + ? WHERE user_id = ?",
            (stars, user_id),
        )
        await db.commit()


async def save_stars_transaction(user_id: int, product_id: str, stars: int, payment_id: str | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO transactions (user_id, product_id, stars_paid, telegram_payment_id) VALUES (?,?,?,?)",
            (user_id, product_id, stars, payment_id),
        )
        await db.commit()


# ── ANALYTICS CREDITS ─────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int) -> dict:
    """Legacy helper — ensure user row exists."""
    await register_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else {}


async def add_analytics_credits(user_id: int, credits: int):
    await register_user(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET analytics_credits = analytics_credits + ? WHERE user_id = ?",
            (credits, user_id),
        )
        await db.commit()


async def use_analytics_credit(user_id: int, count: int = 1) -> bool:
    """Deduct analytics credits. Returns True if successful."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT analytics_credits FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row or row[0] < count:
            return False
        await db.execute(
            "UPDATE users SET analytics_credits = analytics_credits - ? WHERE user_id = ?",
            (count, user_id),
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


async def log_analysis(user_id: int, query: str, analysis_type: str, result: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO analyses (user_id, query, type, result) VALUES (?, ?, ?, ?)",
            (user_id, query, analysis_type, result),
        )
        await db.commit()


# ── ADMIN STATS ───────────────────────────────────────────────────────────────────

async def get_admin_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            total_users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM transactions") as cur:
            total_purchases = (await cur.fetchone())[0]
        async with db.execute("SELECT COALESCE(SUM(stars_paid),0) FROM transactions") as cur:
            total_stars = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE timestamp >= date('now','-1 day')"
        ) as cur:
            today_purchases = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE timestamp >= date('now','-7 day')"
        ) as cur:
            week_purchases = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT referrer_id, COUNT(*) as cnt FROM referral_rewards GROUP BY referrer_id ORDER BY cnt DESC LIMIT 5"
        ) as cur:
            top_refs = await cur.fetchall()
    return {
        "total_users": total_users,
        "total_purchases": total_purchases,
        "total_stars": total_stars,
        "today": today_purchases,
        "week": week_purchases,
        "top_refs": top_refs,
    }
