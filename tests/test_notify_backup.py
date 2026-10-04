"""Email alerts (core/notifier.py) and nightly database backups (core/backup.py)."""
import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import scheduler as scheduler_module
from core import backup, notifier
from database.models import AppSetting

MAIL_SETTINGS = {
    "notify_email_enabled": "true",
    "notify_email_to": "",
    "smtp_host": "smtp.test.local",
    "smtp_port": "587",
    "smtp_user": "sender@test.local",
    "smtp_password": "abcd efgh ijkl mnop",
}


def _set(db, **values):
    for key, value in values.items():
        row = db.query(AppSetting).filter(AppSetting.key == key).first() or AppSetting(key=key)
        row.value = value
        db.add(row)
    db.commit()


@pytest.fixture
def outbox(db, monkeypatch):
    """Mail settings filled in, SMTP replaced by a recorder."""
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, **kw):
            self.host, self.port = host, port

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            self.login_as = (user, password)

        def send_message(self, msg):
            sent.append({"to": msg["To"], "subject": msg["Subject"], "body": msg.get_content(),
                         "login": self.login_as})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    _set(db, **MAIL_SETTINGS)
    yield sent
    _set(db, notify_email_enabled="false", smtp_user="", smtp_password="", notify_state="{}")


def test_alerts_go_to_the_login_email_once_per_cooldown(client, db, outbox):
    assert notifier.notify(db, "publish:1", "Gagal posting", "detail") is True
    assert notifier.notify(db, "publish:1", "Gagal posting", "detail") is False   # same problem, muted
    assert notifier.notify(db, "publish:2", "Gagal posting", "detail") is True    # a different one
    assert len(outbox) == 2
    assert outbox[0]["to"] == "admin@test.local"            # fallback: the dashboard login
    assert outbox[0]["login"] == ("sender@test.local", "abcdefghijklmnop")   # App Password spaces dropped
    assert "Gagal posting" in outbox[0]["subject"]


def test_alerts_are_silent_when_switched_off(client, db, outbox):
    _set(db, notify_email_enabled="false")
    assert notifier.notify(db, "x", "s", "b") is False
    assert outbox == []


def test_incomplete_mail_setup_is_reported_not_raised(client, db, outbox):
    _set(db, smtp_password="")
    result = notifier.send_email(db, "s", "b")
    assert result["success"] is False and "App Password" in result["message"]


def test_smtp_password_is_never_sent_back_to_the_browser(client, db, outbox):
    assert client.get("/api/settings").json()["smtp_password"] != MAIL_SETTINGS["smtp_password"]


def test_test_email_endpoint(client, outbox):
    res = client.post("/api/notifications/test").json()
    assert res["success"] is True
    assert outbox[-1]["to"] == "admin@test.local"


def test_failed_autopost_mails_an_alert(client, db, make_page, with_gemini_key, monkeypatch, outbox):
    page = make_page("100", autopilot_enabled=True)

    def broken(*args, **kwargs):
        raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(scheduler_module, "generate_post_content", broken)
    scheduler_module.auto_generate_and_post_job(page["id"])
    assert len(outbox) == 1
    assert "insufficient_quota" in outbox[0]["body"]


def test_broken_token_is_reported_by_the_nightly_check(client, db, make_page, fake_facebook, fake_metrics, outbox):
    make_page("100")
    fake_facebook["fail_verify"].add("100")
    scheduler_module.metrics_sync_job()
    assert any("Token Facebook" in m["subject"] for m in outbox)
    assert fake_metrics["calls"] == []          # skipped: the token would fail anyway


# ---------------------------------------------------------------- backups


def test_backup_is_a_valid_compressed_copy_of_the_database(client, tmp_dir):
    made = backup.create_backup()
    path = backup.backup_path(made["name"])
    restored = tmp_dir / "restored.db"
    with gzip.open(path, "rb") as packed:
        restored.write_bytes(packed.read())
    con = sqlite3.connect(restored)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    finally:
        con.close()
    assert {"posts", "facebook_pages", "app_settings"} <= tables


def test_old_backups_are_pruned_but_the_newest_is_kept():
    for old in backup.BACKUP_DIR.glob("autoposter-*.db.gz"):
        old.unlink()
    now = datetime.now(timezone.utc)
    for days in (1, 13, 15, 40):
        stamp = (now - timedelta(days=days)).strftime("%Y%m%d-%H%M%S")
        (backup.BACKUP_DIR / f"autoposter-{stamp}.db.gz").write_bytes(b"x")
    assert backup.prune_backups(now) == 2
    assert len(backup.list_backups()) == 2

    for old in backup.BACKUP_DIR.glob("autoposter-*.db.gz"):
        old.unlink()
    lone = (now - timedelta(days=60)).strftime("%Y%m%d-%H%M%S")
    (backup.BACKUP_DIR / f"autoposter-{lone}.db.gz").write_bytes(b"x")
    assert backup.prune_backups(now) == 0     # never delete the only backup left


def test_backup_api_lists_downloads_and_rejects_other_paths(client):
    made = client.post("/api/backups").json()
    assert made["success"] is True
    listed = client.get("/api/backups").json()
    assert listed["items"][0]["name"] == made["backup"]["name"]
    assert listed["keep_days"] == 14

    res = client.get(f"/api/backups/{made['backup']['name']}")
    assert res.status_code == 200 and res.content[:2] == b"\x1f\x8b"   # gzip
    assert client.get("/api/backups/..%2Fautoposter.db").status_code == 404
    assert client.get("/api/backups/autoposter.db").status_code == 404


def test_backups_need_a_login(client):
    client.post("/api/auth/logout")
    assert client.get("/api/backups").status_code == 401


def test_catch_up_makes_a_backup_when_none_is_recent(client, monkeypatch):
    for old in backup.BACKUP_DIR.glob("autoposter-*.db.gz"):
        old.unlink()
    monkeypatch.setattr(scheduler_module, "metrics_sync_overdue", lambda db: False)
    monkeypatch.setattr(scheduler_module, "missed_post_pages", lambda db, started: [])
    scheduler_module.catch_up_job()
    assert len(backup.list_backups()) == 1


def test_satu_putaran_balas_komentar_gagal_tidak_mengirim_email(client, make_page, outbox, db, monkeypatch):
    """Regresi: satu gangguan sesaat (Facebook/AI) dulu langsung mengirim email
    'tidak berjalan', padahal putaran 10 menit berikutnya berhasil."""
    page = make_page("111")
    client.patch(f"/api/pages/{page['id']}", json={"auto_reply_enabled": True})
    hasil = {"success": False, "message": "(#2) Service temporarily unavailable"}
    monkeypatch.setattr(scheduler_module, "process_page_comments", lambda db, p, gap=False: dict(hasil))
    jam = {"t": datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc)}
    asli = scheduler_module._reply_failure_minutes
    monkeypatch.setattr(scheduler_module, "_reply_failure_minutes", lambda key: asli(key, jam["t"]))
    scheduler_module._reply_failing_since.clear()

    scheduler_module.comment_reply_job()                 # gagal sekali
    jam["t"] += timedelta(minutes=10)
    hasil.update(success=True, message="ok")
    scheduler_module.comment_reply_job()                 # pulih
    assert outbox == []

    hasil.update(success=False, message="(#2) Service temporarily unavailable")
    for _ in range(3):                                   # gagal terus 30 menit
        jam["t"] += timedelta(minutes=10)
        scheduler_module.comment_reply_job()
    assert len(outbox) == 0
    jam["t"] += timedelta(minutes=10)
    scheduler_module.comment_reply_job()

    assert len(outbox) == 1 and "terhenti" in outbox[0]["subject"]
    assert "30 menit" in outbox[0]["body"]
    scheduler_module._reply_failing_since.clear()
