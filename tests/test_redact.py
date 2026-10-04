"""Tokens never leak into stored errors, emails or logs."""
import logging

import requests

import core.fb_client as fbc
from core.utils import RedactSecretsFilter, redact_secrets

TOKEN = "EAAUwEXda8goBRkDummyTokenForTests1234567890abcdef"
DNS_ERROR = (
    "HTTPSConnectionPool(host='graph.facebook.com', port=443): Max retries exceeded with url: "
    f"/v25.0/366143080610045/posts?fields=id%2Cmessage&since=1789914965&limit=25&access_token={TOKEN} "
    "(Caused by NameResolutionError(\"Failed to resolve 'graph.facebook.com'\"))"
)


def test_token_di_url_disamarkan():
    """Regresi: email 'Balas komentar otomatis terhenti' memuat Page Access Token utuh."""
    out = redact_secrets(f"Koneksi ke Facebook gagal: {DNS_ERROR}")

    assert TOKEN not in out and "EAA" + "UwEX" not in out
    assert "access_token=***" in out and "Failed to resolve" in out


def test_token_tanpa_nama_parameter_juga_disamarkan():
    assert TOKEN not in redact_secrets(f"token: {TOKEN}")


def test_error_baca_komentar_tidak_memuat_token(monkeypatch):
    def putus(*a, **k):
        raise requests.exceptions.ConnectionError(DNS_ERROR)

    monkeypatch.setattr(fbc.requests, "get", putus)
    res = fbc.fetch_recent_comments("366143080610045", TOKEN)

    assert not res["success"] and TOKEN not in res["message"]


def test_email_tidak_memuat_token(outbox_for_redact):
    from core.notifier import send_email
    db, sent = outbox_for_redact
    send_email(db, "Balas komentar terhenti", f"Pesan: Koneksi ke Facebook gagal: {DNS_ERROR}")

    assert sent and TOKEN not in sent[0]


def test_log_tidak_memuat_token():
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "Failed: %s", (DNS_ERROR,), None)
    RedactSecretsFilter().filter(record)

    assert TOKEN not in record.getMessage()


import pytest  # noqa: E402


@pytest.fixture
def outbox_for_redact(db, monkeypatch):
    from core import notifier
    from database.models import AppSetting
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self, context=None): pass
        def login(self, u, p): pass
        def send_message(self, msg): sent.append(msg["Subject"] + "\n" + msg.get_content())
        def __enter__(self): return self
        def __exit__(self, *e): return False

    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    values = {"smtp_host": "smtp.test", "smtp_user": "a@test", "smtp_password": "pw", "notify_email_to": "b@test"}
    for k, v in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == k).first() or AppSetting(key=k)
        row.value = v
        db.add(row)
    db.commit()
    yield db, sent
    for k in values:
        row = db.query(AppSetting).filter(AppSetting.key == k).first()
        row.value = ""
    db.commit()
