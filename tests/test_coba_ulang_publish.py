"""Coba ulang otomatis bila upload autopilot tidak pernah sampai ke Facebook (gangguan DNS/koneksi)."""
import socket
from datetime import datetime, timedelta, timezone

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError

import scheduler as sched
from core import fb_client
from database.models import FacebookPage, Post

DNS_GAGAL = {"success": False, "message": "Max retries exceeded (Caused by NameResolutionError)", "retryable": True}
DITOLAK = {"success": False, "message": "Facebook Error: permission denied"}
TAYANG = {"success": True, "post_id": "fb_1", "post_url": "https://fb/1", "message": "ok"}


def _galat_dns():
    alasan = NameResolutionError("graph.facebook.com", None, socket.gaierror(-3, "Temporary failure in name resolution"))
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/v25.0/1/photos", alasan))


@pytest.mark.parametrize("galat, aman_diulang", [
    (_galat_dns(), True),
    (requests.exceptions.ConnectionError(MaxRetryError(None, "/x", NewConnectionError(None, "Connection refused"))), True),
    (requests.exceptions.ConnectTimeout(), True),
    # Permintaan mungkin sudah diterima Facebook: mengulang bisa membuat postingan ganda.
    (requests.exceptions.ReadTimeout(), False),
    (requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError())), False),
    (ValueError("lain"), False),
])
def test_hanya_galat_sebelum_sampai_facebook_yang_aman_diulang(galat, aman_diulang):
    assert fb_client.never_reached_facebook(galat) is aman_diulang


def test_publish_menandai_gangguan_dns_bisa_diulang(monkeypatch, tmp_dir):
    gambar = tmp_dir / "poster.jpg"
    gambar.write_bytes(b"jpeg")

    def post(*a, **k):
        raise _galat_dns()

    monkeypatch.setattr(fb_client.requests, "post", post)

    res = fb_client.publish_photo_to_page("1", "token", str(gambar), "caption")

    assert res["success"] is False
    assert res["retryable"] is True


@pytest.fixture
def email(monkeypatch):
    terkirim = []
    monkeypatch.setattr(sched, "notify", lambda db, key, subject, body, **k: terkirim.append(subject) or True)
    return terkirim


@pytest.fixture
def upload(monkeypatch):
    """Upload palsu yang menjawab sesuai daftar `hasil`, berurutan (yang terakhir diulang)."""
    monkeypatch.setattr(sched, "generate_post_content", lambda *a, **k: {
        "visual_title": "Judul", "subtitle": "s", "imagen_prompt": "prompt", "caption": "caption otomatis",
    })
    monkeypatch.setattr(sched, "generate_poster_image", lambda **k: ("auto.jpg", "auto.jpg"))
    state = {"hasil": [], "panggilan": 0}

    def publish(**k):
        state["panggilan"] += 1
        hasil = state["hasil"].pop(0) if len(state["hasil"]) > 1 else state["hasil"][0]
        return dict(hasil)

    monkeypatch.setattr(sched, "publish_photo_to_page", publish)
    return state


def _sekarang():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_job_coba_ulang_terpasang(client):
    assert sched.scheduler.get_job("publish_retry_job") is not None


def test_gangguan_dns_tidak_langsung_email_tetapi_dicoba_ulang(client, make_page, topics, with_gemini_key,
                                                               upload, email, db):
    upload["hasil"] = [DNS_GAGAL]
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    post = db.query(Post).one()
    assert post.status == "failed"
    assert "tidak bisa dihubungi" in post.error_message
    sisa = post.publish_retry_until - _sekarang()
    assert timedelta(minutes=55) < sisa <= timedelta(minutes=60)
    assert email == []
    assert client.get(f"/api/posts/{post.id}").json()["publish_retry_until"]


def test_coba_ulang_menayangkan_saat_jaringan_pulih(client, make_page, topics, with_gemini_key,
                                                    upload, email, db):
    upload["hasil"] = [DNS_GAGAL, TAYANG]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])

    sched.publish_retry_job()

    db.expire_all()
    post = db.query(Post).one()
    assert post.status == "published"
    assert post.fb_post_id == "fb_1"
    assert post.publish_retry_until is None
    assert post.error_message is None
    assert upload["panggilan"] == 2
    assert email == []

    sched.publish_retry_job()   # tidak ada lagi yang perlu diulang
    assert upload["panggilan"] == 2


def test_menyerah_dan_email_sekali_setelah_jendela_habis(client, make_page, topics, with_gemini_key,
                                                         upload, email, db):
    upload["hasil"] = [DNS_GAGAL]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    sched.publish_retry_job()
    assert upload["panggilan"] == 2
    assert email == []

    db.expire_all()
    post = db.query(Post).one()
    post.publish_retry_until = _sekarang() - timedelta(minutes=1)
    db.commit()
    sched.publish_retry_job()
    sched.publish_retry_job()

    db.expire_all()
    post = db.query(Post).one()
    assert post.status == "failed"
    assert post.publish_retry_until is None
    assert upload["panggilan"] == 2          # tidak diunggah lagi setelah jendela habis
    assert len(email) == 1 and "Gagal posting" in email[0]


def test_ditolak_facebook_langsung_email_tanpa_coba_ulang(client, make_page, topics, with_gemini_key,
                                                         upload, email, db):
    upload["hasil"] = [DITOLAK]
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])
    sched.publish_retry_job()

    post = db.query(Post).one()
    assert post.status == "failed"
    assert post.publish_retry_until is None
    assert post.error_message == DITOLAK["message"]
    assert upload["panggilan"] == 1
    assert len(email) == 1


def test_publish_manual_mengambil_alih_coba_ulang(client, make_page, topics, with_gemini_key,
                                                 upload, email, fake_facebook, db):
    upload["hasil"] = [DNS_GAGAL]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    post_id = db.query(Post).one().id
    fake_facebook["fail_publish"] = True

    client.post(f"/api/posts/{post_id}/publish", json={})
    sched.publish_retry_job()

    db.expire_all()
    post = db.query(Post).one()
    assert post.status == "failed"
    assert post.publish_retry_until is None
    assert upload["panggilan"] == 1          # job tidak mengunggah lagi di belakang pengguna


def test_halaman_dijeda_tidak_dicoba_ulang(client, make_page, topics, with_gemini_key, upload, email, db):
    upload["hasil"] = [DNS_GAGAL]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one().autopilot_enabled = False
    db.commit()

    sched.publish_retry_job()

    db.expire_all()
    post = db.query(Post).one()
    assert post.status == "failed"
    assert post.publish_retry_until is None
    assert upload["panggilan"] == 1
    assert email == []
