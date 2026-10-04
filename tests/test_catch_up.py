"""
After a VPS reboot or redeploy the app must catch up what fell due while it was
down: the latest missed posting slot per page (once, no burst) and an overdue
nightly metric sync, without ever double-posting a slot that already ran.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import scheduler as scheduler_module
from scheduler import missed_post_pages, metrics_sync_overdue, LAST_METRICS_SYNC_KEY
from database.models import AppSetting, Post

WIB = ZoneInfo("Asia/Jakarta")


@pytest.fixture
def autopilot_page(make_page):
    return make_page("111", autopilot_enabled=True, auto_post_times="09:00,21:00")


def _started(hour, minute):
    return datetime(2026, 10, 1, hour, minute, tzinfo=WIB)


def test_slot_yang_terlewat_saat_mati_disusul(client, autopilot_page, db):
    started = _started(9, 5)          # VPS hidup lagi 09:05, slot 09:00 terlewat
    assert missed_post_pages(db, started, now=started + timedelta(minutes=1)) == [autopilot_page["id"]]


def test_slot_yang_sudah_menghasilkan_postingan_tidak_diulang(client, autopilot_page, make_post, topics, db):
    started = _started(9, 5)
    slot_utc = _started(9, 0).astimezone(timezone.utc).replace(tzinfo=None)
    post = make_post(autopilot_page["id"], topics[0], status="ready")
    post.created_at = slot_utc + timedelta(minutes=2)       # job 09:00 sempat jalan sebelum restart
    db.commit()

    assert missed_post_pages(db, started, now=started + timedelta(minutes=1)) == []


def test_slot_setelah_aplikasi_menyala_milik_penjadwal_biasa(client, autopilot_page, db):
    started = _started(8, 59)         # menyala 08:59 -> slot 09:00 dijalankan penjadwal biasa
    assert missed_post_pages(db, started, now=_started(9, 0) + timedelta(seconds=30)) == []


def test_mati_terlalu_lama_tidak_menyusul(client, autopilot_page, db):
    started = _started(13, 30)        # slot terakhir 09:00, sudah 4,5 jam
    assert missed_post_pages(db, started, now=started + timedelta(minutes=1)) == []


def test_autopilot_mati_tidak_disusul(client, make_page, db):
    make_page("111", autopilot_enabled=False, auto_post_times="09:00")
    started = _started(9, 5)
    assert missed_post_pages(db, started, now=started + timedelta(minutes=1)) == []


def test_slot_kemarin_malam_ikut_dihitung(client, make_page, db):
    make_page("111", autopilot_enabled=True, auto_post_times="23:30")
    started = datetime(2026, 10, 2, 0, 20, tzinfo=WIB)   # 23:30 kemarin terlewat
    assert len(missed_post_pages(db, started, now=started + timedelta(minutes=1))) == 1


def _set_last_sync(db, value):
    row = db.query(AppSetting).filter(AppSetting.key == LAST_METRICS_SYNC_KEY).first() \
        or AppSetting(key=LAST_METRICS_SYNC_KEY)
    row.value = value
    db.add(row)
    db.commit()


def test_sinkron_metrik_terlambat_terdeteksi(client, db):
    now = datetime.now(timezone.utc)
    _set_last_sync(db, "")
    assert metrics_sync_overdue(db, now) is True
    _set_last_sync(db, (now - timedelta(hours=5)).isoformat())
    assert metrics_sync_overdue(db, now) is False
    _set_last_sync(db, (now - timedelta(hours=30)).isoformat())
    assert metrics_sync_overdue(db, now) is True
    _set_last_sync(db, "")


def test_catch_up_menjalankan_yang_terlewat(client, autopilot_page, db, monkeypatch):
    calls = []
    monkeypatch.setattr(scheduler_module, "metrics_sync_job", lambda: calls.append("metrics"))
    monkeypatch.setattr(scheduler_module, "auto_generate_and_post_job", lambda pid: calls.append(pid))
    monkeypatch.setattr(scheduler_module, "STARTED_AT", datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(scheduler_module, "missed_post_pages", lambda db, started: [autopilot_page["id"]])
    _set_last_sync(db, "")

    scheduler_module.catch_up_job()

    assert calls == ["metrics", autopilot_page["id"]]


def test_waktu_sinkron_tidak_bisa_ditimpa_dari_form_pengaturan(client, db):
    _set_last_sync(db, "2026-10-01T03:00:00+00:00")
    client.post("/api/settings", json={LAST_METRICS_SYNC_KEY: "1999-01-01T00:00:00+00:00"})
    db.expire_all()
    row = db.query(AppSetting).filter(AppSetting.key == LAST_METRICS_SYNC_KEY).first()
    assert row.value == "2026-10-01T03:00:00+00:00"
    assert LAST_METRICS_SYNC_KEY not in client.get("/api/settings").json()
    _set_last_sync(db, "")
