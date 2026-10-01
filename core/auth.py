"""
Dashboard login: password hashing, browser sessions and a brake on guessing.

Standard library only. Passwords are stored as PBKDF2-SHA256 hashes; a session
is a random token in an HttpOnly cookie whose SHA-256 is kept in the database,
so logging out or changing the password really ends the session.
"""
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from database.models import User, UserSession

SESSION_COOKIE = "autoposter_session"
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8

_PBKDF2_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"

# Failed logins allowed inside the window before it is locked: per (IP, email),
# and per email from any IP — behind a proxy the client IP comes from
# X-Forwarded-For, which an attacker can vary at will.
MAX_FAILED_ATTEMPTS = 5
MAX_FAILED_PER_EMAIL = 20
LOCKOUT_SECONDS = 15 * 60


def _utc_now() -> datetime:
    # SQLite hands back naive datetimes; keep everything naive UTC to compare safely.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), _PBKDF2_ITERATIONS)
    return f"{_ALGORITHM}${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), expected)


# A real hash to check against when the email is unknown, so the response time
# does not reveal which emails have an account.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def set_user_password(db: Session, email: str, password: str) -> User:
    """Creates the account or replaces its password, ending all its sessions."""
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError("Email tidak valid.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password minimal {MIN_PASSWORD_LENGTH} karakter.")
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email, password_hash=hash_password(password))
        db.add(user)
    else:
        user.password_hash = hash_password(password)
        user.password_changed_at = _utc_now()
        db.query(UserSession).filter(UserSession.user_id == user.id).delete()
    db.commit()
    return user


def authenticate(db: Session, email: str, password: str):
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    return user if verify_password(password, user.password_hash) else None


def has_any_user(db: Session) -> bool:
    return db.query(User.id).first() is not None


# ------------------------------------------------------------------ sessions


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    """Returns the raw token for the cookie; only its hash is stored."""
    now = _utc_now()
    db.query(UserSession).filter(UserSession.expires_at < now).delete()
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=_token_hash(token), user_id=user.id,
                       expires_at=now + timedelta(days=SESSION_DAYS)))
    db.commit()
    return token


def user_for_token(db: Session, token: str):
    if not token:
        return None
    row = db.query(UserSession).filter(UserSession.token_hash == _token_hash(token)).first()
    if row is None or row.expires_at < _utc_now():
        return None
    return db.query(User).filter(User.id == row.user_id).first()


def end_session(db: Session, token: str) -> None:
    if token:
        db.query(UserSession).filter(UserSession.token_hash == _token_hash(token)).delete()
        db.commit()


# ------------------------------------------------------------------ guessing brake


class LoginThrottle:
    """In-memory count of failed logins per (IP, email) and per email; resets on restart."""

    def __init__(self, max_attempts: int = MAX_FAILED_ATTEMPTS,
                 max_per_email: int = MAX_FAILED_PER_EMAIL, window: int = LOCKOUT_SECONDS):
        self.max_attempts = max_attempts
        self.max_per_email = max_per_email
        self.window = window
        self._failures = {}
        self._lock = threading.Lock()

    def _keys(self, ip: str, email: str):
        email = normalize_email(email)
        return (((ip, email), self.max_attempts), ((None, email), self.max_per_email))

    def _recent(self, key, now):
        recent = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = recent
        return recent

    def seconds_locked(self, ip: str, email: str) -> int:
        now, locked = time.monotonic(), 0
        with self._lock:
            for key, limit in self._keys(ip, email):
                recent = self._recent(key, now)
                if len(recent) >= limit:
                    locked = max(locked, max(1, int(self.window - (now - recent[-limit]))))
        return locked

    def record_failure(self, ip: str, email: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key, _ in self._keys(ip, email):
                self._failures[key] = self._recent(key, now) + [now]

    def reset(self, ip: str, email: str) -> None:
        # Only this IP's count: a correct login must not wipe the evidence of
        # guesses that came from elsewhere.
        with self._lock:
            self._failures.pop((ip, normalize_email(email)), None)


login_throttle = LoginThrottle()
