import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from typing import Any

from config import SQLITE_DB_PATH


SESSION_COOKIE_NAME = "ww_session"
SESSION_TTL_SECONDS = 14 * 24 * 60 * 60
PBKDF2_ITERATIONS = 220_000


class AuthStore:
    def __init__(self, db_path: str = SQLITE_DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self._init_db()

    def _init_db(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                disabled INTEGER DEFAULT 0,
                created_at REAL,
                last_login_at REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                created_by INTEGER,
                max_uses INTEGER DEFAULT 1,
                uses INTEGER DEFAULT 0,
                expires_at REAL,
                created_at REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_agent TEXT,
                created_at REAL,
                expires_at REAL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        self.cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id, expires_at)")
        self.conn.commit()

    def ensure_bootstrap_invite(self) -> str | None:
        self.cursor.execute("SELECT COUNT(*) FROM users")
        user_count = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM invite_codes WHERE uses < max_uses")
        invite_count = self.cursor.fetchone()[0]
        if user_count or invite_count:
            return None

        code = os.getenv("INITIAL_INVITE_CODE") or secrets.token_urlsafe(12)
        self.cursor.execute(
            "INSERT OR IGNORE INTO invite_codes (code, max_uses, uses, created_at) VALUES (?, ?, 0, ?)",
            (code, 5, time.time()),
        )
        self.conn.commit()
        os.makedirs(".runlogs", exist_ok=True)
        with open(os.path.join(".runlogs", "bootstrap_invite.txt"), "w", encoding="utf-8") as f:
            f.write(code)
        return code

    def _hash_password(self, password: str, salt: str | None = None) -> tuple[str, str]:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            PBKDF2_ITERATIONS,
        ).hex()
        return digest, salt

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        self.cursor.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        self.cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def create_user(self, username: str, password: str, invite_code: str) -> dict[str, Any]:
        username = username.strip().lower()
        invite_code = invite_code.strip()
        if len(username) < 3:
            raise ValueError("用户名至少 3 个字符")
        if len(password) < 8:
            raise ValueError("密码至少 8 个字符")

        now = time.time()
        self.cursor.execute(
            """
            SELECT code, max_uses, uses, expires_at
            FROM invite_codes
            WHERE code=?
            """,
            (invite_code,),
        )
        invite = self.cursor.fetchone()
        if not invite:
            raise ValueError("邀请码无效")
        if invite["uses"] >= invite["max_uses"]:
            raise ValueError("邀请码已用完")
        if invite["expires_at"] and invite["expires_at"] < now:
            raise ValueError("邀请码已过期")
        if self.get_user_by_username(username):
            raise ValueError("用户名已存在")

        password_hash, salt = self._hash_password(password)
        self.cursor.execute("SELECT COUNT(*) FROM users")
        is_admin = 1 if self.cursor.fetchone()[0] == 0 else 0
        self.cursor.execute(
            """
            INSERT INTO users (username, password_hash, password_salt, is_admin, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (username, password_hash, salt, is_admin, now),
        )
        user_id = self.cursor.lastrowid
        self.cursor.execute(
            "UPDATE invite_codes SET uses=uses+1 WHERE code=?",
            (invite_code,),
        )
        self.conn.commit()
        return self.safe_user(self.get_user_by_id(user_id))

    def verify_password(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.get_user_by_username(username)
        if not user or user.get("disabled"):
            return None
        expected, _ = self._hash_password(password, user["password_salt"])
        if not hmac.compare_digest(expected, user["password_hash"]):
            return None
        self.cursor.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), user["id"]))
        self.conn.commit()
        return self.safe_user(user)

    def create_session(self, user_id: int, user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.cursor.execute(
            """
            INSERT INTO user_sessions (token_hash, user_id, user_agent, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (self._hash_token(token), user_id, user_agent[:300], now, now + SESSION_TTL_SECONDS),
        )
        self.conn.commit()
        return token

    def get_user_by_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.time()
        self.cursor.execute(
            """
            SELECT u.*
            FROM user_sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.disabled=0
            """,
            (self._hash_token(token), now),
        )
        row = self.cursor.fetchone()
        return self.safe_user(dict(row)) if row else None

    def delete_session(self, token: str | None):
        if not token:
            return
        self.cursor.execute("DELETE FROM user_sessions WHERE token_hash=?", (self._hash_token(token),))
        self.conn.commit()

    def safe_user(self, user: dict[str, Any] | None) -> dict[str, Any] | None:
        if not user:
            return None
        return {
            "id": user["id"],
            "username": user["username"],
            "is_admin": bool(user.get("is_admin")),
            "created_at": user.get("created_at"),
            "last_login_at": user.get("last_login_at"),
        }
