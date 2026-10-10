"""Posting silang ke Threads: poster yang sama, caption dipecah jadi utas (postingan utama + balasan)."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_module
import scheduler as sched
from config import IMAGES_DIR, SECRET_MASK
from core import threads as th
from core import threads_client as thc
from core.instagram import PUBLIC_BASE_URL_KEY, remember_public_base_url
from core.utils import redact_secrets
from database.models import AppSetting, FacebookPage

BASE = "https://autoposter.example.com"
MENIT = 1 / 1440
TOKEN = "THtoken" + "x" * 40

CAPTION_PANJANG = (
    "Pernahkah Anda bertanya mengapa emas selalu berkumpul di tikungan dalam sungai? " * 3 + "\n\n"
    + "Saat air berbelok, arus di sisi luar lebih cepat dan di sisi dalam melambat. " * 4 + "\n\n"
    + "Tips lapangan:\n" + "\n".join(f"• Tips nomor {i}: gali sampai lapisan batuan dasar." for i in range(1, 5))
    + "\n\nCeritakan pengalaman Anda di kolom komentar! 👇\n\n#emas #dulangemas #geologi"
)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def th_api(monkeypatch):
    state = {"calls": [], "fail_at": None, "fail_result": None,
             "profile": {"success": True, "threads_user_id": "9001", "username": "miners24"}}

    def publish(user_id, token, text, image_url=None, reply_to_id=None, sleep=None):
        if state["fail_at"] is not None and len(state["calls"]) == state["fail_at"]:
            state["fail_at"] = None
            return state["fail_result"]
        state["calls"].append({"user": user_id, "token": token, "text": text,
                               "image_url": image_url, "reply_to": reply_to_id})
        n = len(state["calls"])
        return {"success": True, "media_id": f"t{n}",
                "permalink": None if reply_to_id else f"https://www.threads.net/@miners24/post/P{n}"}

    monkeypatch.setattr(th, "publish_to_threads", publish)
    monkeypatch.setattr(th, "get_threads_profile", lambda token: state["profile"])
    monkeypatch.delenv("THREADS_APP_SECRET", raising=False)
    return state


@pytest.fixture
def public_url(db):
    remember_public_base_url(db, BASE + "/")
    yield
    db.query(AppSetting).filter(AppSetting.key == PUBLIC_BASE_URL_KEY).delete()
    db.commit()


@pytest.fixture
def page(client, make_page, th_api, db):
    p = make_page("111")
    res = client.patch(f"/api/pages/{p['id']}", json={"threads_access_token": TOKEN}).json()
    assert res["success"], res
    res = client.patch(f"/api/pages/{p['id']}", json={"threads_enabled": True}).json()
    assert res["success"], res
    # Posts "published" a few minutes ago must count as published after enabling.
    row = db.query(FacebookPage).filter(FacebookPage.id == p["id"]).one()
    row.threads_enabled_at = _now() - timedelta(hours=1)
    db.commit()
    return res["page"]


# ------------------------------------------------------------ caption -> utas


def _size(text):
    return len(text.encode("utf-8"))


def test_caption_pendek_jadi_satu_postingan_dengan_satu_tag():
    parts = th.split_for_threads("Emas mengendap di tikungan dalam.\n\n#emas #geologi #sungai")
    assert parts == ["Emas mengendap di tikungan dalam.\n\n#emas"]


def test_caption_panjang_dipecah_di_batas_paragraf_tanpa_kehilangan_isi():
    parts = th.split_for_threads(CAPTION_PANJANG)

    assert len(parts) > 1 and all(_size(p) <= th.THREADS_TEXT_MAX for p in parts)
    assert parts[0].endswith("#emas")                               # tag di postingan utama
    assert "#dulangemas" not in "".join(parts)                      # Threads hanya memakai satu tag
    gabungan = " ".join(" ".join(parts).split())
    for kalimat in ("Tips nomor 4: gali sampai lapisan batuan dasar.", "Ceritakan pengalaman Anda di kolom komentar! 👇"):
        assert kalimat in gabungan
    assert not any(p.startswith(" ") or p.endswith(" ") for p in parts)


def test_paragraf_terlalu_panjang_dipecah_di_kalimat_lalu_kata():
    kalimat = "Ini kalimat yang cukup panjang tentang emas aluvial. "
    parts = th.split_for_threads(kalimat * 30)
    assert all(_size(p) <= th.THREADS_TEXT_MAX for p in parts)
    assert all(p.endswith(".") for p in parts)                      # tidak terpotong di tengah kalimat

    parts = th.split_for_threads("kata " * 400)
    assert all(_size(p) <= th.THREADS_TEXT_MAX and "kat " not in p for p in parts)


def test_emoji_dihitung_aman_dan_jumlah_bagian_dibatasi():
    parts = th.split_for_threads("🪙" * 2000)
    assert len(parts) == th.THREADS_MAX_PARTS
    assert all(_size(p) <= th.THREADS_TEXT_MAX for p in parts) and parts[-1].endswith("…")


def test_caption_kosong_tetap_satu_bagian():
    assert th.split_for_threads("") == [""]
    assert th.split_for_threads(None) == [""]


# ------------------------------------------------------------ menghubungkan


def test_token_threads_diperiksa_lalu_disimpan_tanpa_ditampilkan(page, db):
    assert page["threads_enabled"] is True and page["threads_username"] == "miners24"
    assert page["threads_access_token"] == SECRET_MASK
    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    assert row.threads_access_token == TOKEN and row.threads_user_id == "9001"


def test_token_threads_tidak_valid_ditolak(client, make_page, th_api):
    th_api["profile"] = {"success": False, "message": "Invalid OAuth access token."}
    p = make_page("222")

    res = client.patch(f"/api/pages/{p['id']}", json={"threads_access_token": "THsalah"}).json()

    assert res["success"] is False and "Invalid" in res["message"]
    assert client.get("/api/pages").json()["pages"][0]["threads_has_token"] is False


def test_tidak_bisa_dinyalakan_tanpa_token(client, make_page, th_api):
    p = make_page("333")
    res = client.patch(f"/api/pages/{p['id']}", json={"threads_enabled": True}).json()
    assert res["success"] is False and "Token Threads" in res["message"]


def test_token_pendek_ditukar_jadi_token_60_hari_bila_ada_app_secret(client, make_page, th_api, monkeypatch, db):
    monkeypatch.setenv("THREADS_APP_SECRET", "rahasia")
    monkeypatch.setattr(th, "exchange_long_lived_token",
                        lambda token, secret: {"success": True, "access_token": "THpanjang", "expires_in": 5184000})
    p = make_page("444")

    res = client.patch(f"/api/pages/{p['id']}", json={"threads_access_token": "THpendek"}).json()

    assert res["success"] and res["page"]["threads_token_expires"]
    row = db.query(FacebookPage).filter(FacebookPage.id == p["id"]).one()
    assert row.threads_access_token == "THpanjang"


# ------------------------------------------------------------ publikasi utas


def test_postingan_tayang_dikirim_sebagai_utas_sekali(page, make_post, topics, th_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.caption = CAPTION_PANJANG
    db.commit()
    parts = th.split_for_threads(CAPTION_PANJANG)

    sched.threads_job()
    sched.threads_job()

    calls = th_api["calls"]
    assert [c["text"] for c in calls] == parts
    assert calls[0]["image_url"].startswith(f"{BASE}/media/threads/") and calls[0]["reply_to"] is None
    for i in range(1, len(calls)):                                  # tiap balasan di bawah bagian sebelumnya
        assert calls[i]["image_url"] is None and calls[i]["reply_to"] == f"t{i}"
    assert all(c["user"] == "9001" and c["token"] == TOKEN for c in calls)
    db.refresh(post)
    assert post.threads_status == "published" and post.threads_parts_done == len(parts)
    assert post.threads_permalink == "https://www.threads.net/@miners24/post/P1"


def test_balasan_gagal_dilanjutkan_tanpa_mengulang_poster(page, make_post, topics, th_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.caption = CAPTION_PANJANG
    db.commit()
    parts = th.split_for_threads(CAPTION_PANJANG)
    th_api["fail_at"] = 1                                           # balasan pertama gagal
    th_api["fail_result"] = {"success": False, "message": "Rate limit"}

    sched.threads_job()
    db.refresh(post)
    assert post.threads_status == "incomplete" and post.threads_parts_done == 1
    assert "Bagian 2" in post.threads_error

    sched.threads_job()

    db.refresh(post)
    assert post.threads_status == "published" and post.threads_parts_done == len(parts)
    assert sum(1 for c in th_api["calls"] if c["image_url"]) == 1  # poster hanya sekali
    assert th_api["calls"][1]["reply_to"] == "t1"


def test_postingan_sebelum_threads_dinyalakan_tidak_dikirim(page, make_post, topics, th_api, public_url):
    make_post(page["id"], topics[0], days_ago=2 / 24)
    make_post(page["id"], topics[1], days_ago=1, status="ready")

    sched.threads_job()

    assert th_api["calls"] == []


def test_tanpa_alamat_publik_menunggu_dan_menjelaskan(page, make_post, topics, th_api, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)

    sched.threads_job()

    db.refresh(post)
    assert th_api["calls"] == [] and post.threads_status is None
    halaman = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    assert "PUBLIC_BASE_URL" in halaman.threads_last_error


def test_gagal_dicoba_lagi_maksimal_tiga_kali(page, make_post, topics, th_api, public_url, db, monkeypatch):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    monkeypatch.setattr(th, "publish_to_threads",
                        lambda *a, **k: {"success": False, "message": "Media download has failed"})

    for _ in range(5):
        sched.threads_job()

    db.refresh(post)
    assert post.threads_status == "failed" and post.threads_attempts == th.THREADS_MAX_ATTEMPTS
    assert "download" in post.threads_error


def test_koneksi_putus_saat_publish_tidak_diulang(page, make_post, topics, th_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    th_api["fail_at"] = 0
    th_api["fail_result"] = {"success": False, "message": "Koneksi ke Threads gagal", "uncertain": True}

    sched.threads_job()
    sched.threads_job()

    db.refresh(post)
    assert post.threads_status == "uncertain" and th_api["calls"] == []


def test_terhenti_saat_publish_ditandai_saat_start(page, make_post, topics, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.threads_status = "publishing"
    db.commit()

    assert th.recover_interrupted_threads(db) == 1
    db.commit()
    db.refresh(post)
    assert post.threads_status == "uncertain"


# ------------------------------------------------------------ perpanjangan token


def test_token_diperpanjang_berkala(page, th_api, monkeypatch, db):
    monkeypatch.setattr(th, "refresh_long_lived_token",
                        lambda token: {"success": True, "access_token": "THbaru", "expires_in": 5184000})
    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    assert th.refresh_due(row) is False                             # baru dihubungkan

    row.threads_token_refreshed_at = _now() - timedelta(days=2)    # umur token belum diketahui
    db.commit()
    sched.threads_job()

    db.refresh(row)
    assert row.threads_access_token == "THbaru" and row.threads_token_expires > _now() + timedelta(days=59)
    assert th.refresh_due(row) is False


def test_token_kedaluwarsa_menghentikan_dan_menjelaskan(page, make_post, topics, th_api, public_url, monkeypatch, db):
    monkeypatch.setattr(th, "refresh_long_lived_token",
                        lambda token: {"success": False, "message": "Session has expired", "token_error": True})
    make_post(page["id"], topics[0], days_ago=5 * MENIT)
    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    row.threads_token_refreshed_at = _now() - timedelta(days=10)
    db.commit()

    sched.threads_job()

    db.refresh(row)
    assert th_api["calls"] == [] and "token Threads baru" in row.threads_last_error


# ------------------------------------------------------------ link publik poster


def test_link_poster_threads_hanya_untuk_token_yang_berlaku(page, make_post, topics, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    path = IMAGES_DIR / "th_test.jpg"
    Image.new("RGB", (1080, 1350), (200, 170, 110)).save(path, "JPEG")
    post.image_path = str(path)
    post.threads_media_token = "t" * 32
    post.threads_media_expires = _now() + timedelta(minutes=30)
    db.commit()
    anonim = TestClient(app_module.app)

    ok = anonim.get(f"/media/threads/{'t' * 32}.jpg")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(ok.content)).size == (1080, 1350)
    assert anonim.get(f"/media/threads/{'x' * 32}.jpg").status_code == 404
    assert anonim.get(f"/media/ig/{'t' * 32}.jpg").status_code == 404   # token Threads bukan token Instagram

    post.threads_media_expires = _now() - timedelta(minutes=1)
    db.commit()
    assert anonim.get(f"/media/threads/{'t' * 32}.jpg").status_code == 404


def test_token_threads_disamarkan_di_pesan_error():
    assert TOKEN not in redact_secrets(f"gagal memakai {TOKEN}")


# ------------------------------------------------------------ klien Threads API


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_publish_gambar_menunggu_container_lalu_menayangkan(monkeypatch):
    sent, status = [], iter(["IN_PROGRESS", "FINISHED"])

    def post(url, data=None, timeout=None):
        sent.append((url.rsplit("/", 1)[-1], data))
        return _R({"id": "c1"} if url.endswith("/threads") else {"id": "p1"})

    def get(url, params=None, timeout=None):
        if params.get("fields") == "status,error_message":
            return _R({"status": next(status)})
        return _R({"permalink": "https://www.threads.net/@a/post/abc"})

    monkeypatch.setattr(thc.requests, "post", post)
    monkeypatch.setattr(thc.requests, "get", get)
    res = thc.publish_to_threads("9001", "tok", "halo", image_url=f"{BASE}/media/threads/x.jpg",
                                 sleep=lambda s: None)

    assert res == {"success": True, "media_id": "p1", "permalink": "https://www.threads.net/@a/post/abc"}
    assert [s[0] for s in sent] == ["threads", "threads_publish"]
    assert sent[0][1]["media_type"] == "IMAGE" and "reply_to_id" not in sent[0][1]


def test_balasan_teks_memakai_reply_to_id(monkeypatch):
    sent = []
    monkeypatch.setattr(thc.requests, "post", lambda url, data=None, timeout=None:
                        (sent.append(data), _R({"id": "c1"} if url.endswith("/threads") else {"id": "r1"}))[1])
    monkeypatch.setattr(thc.requests, "get", lambda url, params=None, timeout=None: _R({"status": "FINISHED"}))

    res = thc.publish_to_threads("9001", "tok", "lanjutan", reply_to_id="p1", sleep=lambda s: None)

    assert res["success"] and res["media_id"] == "r1"
    assert sent[0]["media_type"] == "TEXT" and sent[0]["reply_to_id"] == "p1"


def test_container_error_tidak_pernah_ditayangkan(monkeypatch):
    calls = []
    monkeypatch.setattr(thc.requests, "post", lambda url, data=None, timeout=None:
                        (calls.append(url), _R({"id": "c1"}))[1])
    monkeypatch.setattr(thc.requests, "get", lambda url, params=None, timeout=None:
                        _R({"status": "ERROR", "error_message": "Media download has failed"}))

    res = thc.publish_to_threads("9001", "tok", "t", image_url="u", sleep=lambda s: None)

    assert res["success"] is False and "download" in res["message"]
    assert not any(u.endswith("threads_publish") for u in calls)


def test_token_tidak_berlaku_menghentikan_antrean(monkeypatch):
    monkeypatch.setattr(thc.requests, "post", lambda url, data=None, timeout=None:
                        _R({"error": {"message": "Error validating access token", "code": 190}}))
    res = thc.publish_to_threads("9001", "tok", "t", image_url="u", sleep=lambda s: None)
    assert res["success"] is False and res["permission_error"] is True
