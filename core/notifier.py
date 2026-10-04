"""
Email alerts for problems the autopilot cannot fix by itself: a failed post, a
Facebook token that stopped working, an AI key out of credit, a failed backup.

Mail goes out over SMTP (Gmail: smtp.gmail.com:587 with an App Password). Alerts
never raise: a broken mail setup must not break the job that tried to report.
The same problem is mailed at most once per cooldown so a page failing every
10 minutes does not flood the inbox.
"""
import hashlib
import json
import logging
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from config import SCHEDULER_TIMEZONE
from core.utils import redact_secrets
from database.models import AppSetting, User

logger = logging.getLogger(__name__)

STATE_KEY = "notify_state"   # {alert key hash: last sent ISO time}
DEFAULT_COOLDOWN_HOURS = 6
STATE_MAX_AGE_DAYS = 7
SMTP_TIMEOUT = 20
APP_NAME = "Gold AI AutoPoster"


def _get(db, key, default=""):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value else default


def _set(db, key, value: str):
    row = db.query(AppSetting).filter(AppSetting.key == key).first() or AppSetting(key=key)
    row.value = value
    db.add(row)
    db.commit()


def recipient(db) -> str:
    """The address set in Pengaturan, else the dashboard login's email."""
    explicit = _get(db, "notify_email_to").strip()
    if explicit:
        return explicit
    user = db.query(User).order_by(User.id).first()
    return user.email if user else ""


def mail_config(db) -> dict:
    try:
        port = int(_get(db, "smtp_port", "587"))
    except ValueError:
        port = 587
    user = _get(db, "smtp_user").strip()
    return {
        "enabled": _get(db, "notify_email_enabled", "false") == "true",
        "host": _get(db, "smtp_host", "smtp.gmail.com").strip(),
        "port": port,
        "user": user,
        "password": _get(db, "smtp_password").replace(" ", ""),   # Gmail shows App Passwords in groups of 4
        "to": recipient(db),
    }


def send_email(db, subject: str, body: str) -> dict:
    """Sends one message now. Returns {success, message} instead of raising."""
    cfg = mail_config(db)
    missing = [label for label, value in (("server SMTP", cfg["host"]), ("email pengirim", cfg["user"]),
                                          ("App Password", cfg["password"]), ("email tujuan", cfg["to"]))
               if not value]
    if missing:
        return {"success": False, "message": f"Notifikasi email belum lengkap: {', '.join(missing)} belum diisi."}

    # Error texts can quote a request URL with its access token; never mail it.
    subject, body = redact_secrets(subject), redact_secrets(body)
    msg = EmailMessage()
    msg["Subject"] = f"[{APP_NAME}] {subject}"
    msg["From"] = f"{APP_NAME} <{cfg['user']}>"
    msg["To"] = cfg["to"]
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT)
            server.starttls(context=context)
        with server:
            server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return {"success": False, "message": "Login SMTP ditolak. Untuk Gmail, pakai App Password (bukan password biasa)."}
    except Exception as e:
        return {"success": False, "message": f"Gagal mengirim email: {e}"}
    logger.info(f"[Notify] Email sent to {cfg['to']}: {subject}")
    return {"success": True, "message": f"Email terkirim ke {cfg['to']}."}


def _load_state(db) -> dict:
    try:
        state = json.loads(_get(db, STATE_KEY, "{}"))
        return state if isinstance(state, dict) else {}
    except ValueError:
        return {}


def notify(db, key: str, subject: str, body: str, cooldown_hours: float = DEFAULT_COOLDOWN_HOURS) -> bool:
    """
    Mails an alert unless the same `key` was mailed within `cooldown_hours`, or
    alerts are switched off. Returns True when an email went out.
    """
    try:
        if not mail_config(db)["enabled"]:
            return False
        now = datetime.now(timezone.utc)
        state = _load_state(db)
        slot = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        last = state.get(slot)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=cooldown_hours):
            return False

        try:
            stamp = now.astimezone(ZoneInfo(SCHEDULER_TIMEZONE)).strftime("%d %b %Y %H:%M %Z")
        except Exception:
            stamp = now.strftime("%d %b %Y %H:%M UTC")
        result = send_email(db, subject, f"{body}\n\n— {APP_NAME}, {stamp}\n"
                                          "Email otomatis. Atur di tab Pengaturan > Notifikasi Email.")
        if not result["success"]:
            logger.warning(f"[Notify] {result['message']}")
            return False

        horizon = now - timedelta(days=STATE_MAX_AGE_DAYS)
        state = {k: v for k, v in state.items() if datetime.fromisoformat(v) > horizon}
        state[slot] = now.isoformat()
        _set(db, STATE_KEY, json.dumps(state))
        return True
    except Exception as e:
        logger.warning(f"[Notify] Alert '{subject}' not sent: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return False
