"""Small shared helpers."""
import logging
import re
from datetime import timezone
from typing import Optional


def iso_utc(dt) -> Optional[str]:
    """
    Timestamps are stored as naive UTC by SQLite. Tag them explicitly so the
    browser does not parse them as local time (which shifted every date by the
    local UTC offset).
    """
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# Credentials that can end up inside an error text: requests puts the full URL,
# query string included, in its exception messages, and Graph API tokens travel
# in the query string (access_token=EAA...).
_SECRET_PARAM = re.compile(r"\b(access_token|input_token|client_secret|appsecret_proof|key)=[^&\s'\"<>)]+",
                           re.I)
_FB_TOKEN = re.compile(r"\bEAA[A-Za-z0-9]{20,}")


def redact_secrets(text) -> str:
    """Masks tokens and keys in a message before it is stored, logged or mailed."""
    text = _SECRET_PARAM.sub(lambda m: f"{m.group(1)}=***", str(text))
    return _FB_TOKEN.sub("EAA***", text)


class RedactSecretsFilter(logging.Filter):
    """Logging filter applying redact_secrets to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact_secrets(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return True
