"""Posting silang ke Instagram: poster & caption yang sama, akun yang terhubung ke Fanspage."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_module
import scheduler as sched
from config import IMAGES_DIR
from core import fb_client as fbc
from core import instagram as ig
from database.models import AppSetting, FacebookPage, Post

BASE = "https://autoposter.example.com"
MENIT = 1 / 1440


@pytest.fixture
def ig_api(monkeypatch):
    state = {"published": [], "result": None,
             "account": {"success": True, "ig_user_id": "1784", "username": "miners24"}}

    def publish(ig_user_id, token, image_url, caption, sleep=None):
        if state["result"]:
            return state["result"]
        state["published"].append({"ig": ig_user_id, "token": token, "url": image_url, "caption": caption})
        n = len(state["published"])
        return {"success": True, "media_id": f"m{n}", "permalink": f"https://www.instagram.com/p/X{n}/"}

    monkeypatch.setattr(ig, "publish_photo_to_instagram", publish)
    monkeypatch.setattr(ig, "find_instagram_account", lambda page_id, token: state["account"])
    return state


@pytest.fixture
def public_url(db):
    ig.remember_public_base_url(db, BASE + "/")
    yield
    db.query(AppSetting).filter(AppSetting.key == ig.PUBLIC_BASE_URL_KEY).delete()
    db.commit()


@pytest.fixture
def page(client, make_page, ig_api, db):
    p = make_page("111")
    res = client.patch(f"/api/pages/{p['id']}", json={"ig_enabled": True}).json()
    assert res["success"], res
    # Posts "published" a few minutes ago must count as published after enabling.
    row = db.query(FacebookPage).filter(FacebookPage.id == p["id"]).one()
    row.ig_enabled_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    db.commit()
    return res["page"]


# ------------------------------------------------------------ gambar


def _poster(w, h):
    path = IMAGES_DIR / f"ig_test_{w}x{h}.jpg"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (200, 170, 110)).save(path, "JPEG")
    return path


def test_poster_3_4_diberi_bingkai_jadi_4_5_tanpa_dipotong():
    out = Image.open(io.BytesIO(ig.instagram_image_bytes(str(_poster(900, 1200)))))
    assert out.size == (960, 1200)                       # 4:5, tinggi utuh (tidak dipotong)

    besar = Image.open(io.BytesIO(ig.instagram_image_bytes(str(_poster(1536, 2048)))))
    assert besar.size == (1080, 1350)                    # poster besar diperkecil ke lebar 1080


def test_poster_persegi_tidak_diubah_rasionya():
    out = Image.open(io.BytesIO(ig.instagram_image_bytes(str(_poster(1024, 1024)))))
    assert out.size == (1024, 1024)


# ------------------------------------------------------------ menghubungkan


def test_menyalakan_instagram_mencari_akun_terhubung(page):
    assert page["ig_enabled"] is True and page["ig_username"] == "miners24"


def test_fanspage_tanpa_instagram_ditolak_dengan_jelas(client, make_page, ig_api):
    ig_api["account"] = {"success": False, "message": "Fanspage ini belum terhubung ke akun Instagram Profesional."}
    p = make_page("222")

    res = client.patch(f"/api/pages/{p['id']}", json={"ig_enabled": True}).json()

    assert res["success"] is False and "Instagram" in res["message"]
    assert client.get("/api/pages").json()["pages"][0]["ig_enabled"] is False


# ------------------------------------------------------------ publikasi


def test_postingan_tayang_dikirim_ke_instagram_sekali(page, make_post, topics, ig_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)

    sched.instagram_job()
    sched.instagram_job()

    assert len(ig_api["published"]) == 1
    sent = ig_api["published"][0]
    assert sent["ig"] == "1784" and sent["token"] == "token-111"
    assert sent["caption"] == post.caption                         # caption sama dengan Facebook
    assert sent["url"].startswith(f"{BASE}/media/ig/") and sent["url"].endswith(".jpg")
    db.refresh(post)
    assert post.ig_status == "published" and post.ig_permalink == "https://www.instagram.com/p/X1/"


def test_postingan_sebelum_instagram_dinyalakan_tidak_dikirim(page, make_post, topics, ig_api, public_url, db):
    make_post(page["id"], topics[0], days_ago=2 / 24)       # tayang 2 jam lalu, sebelum dinyalakan
    make_post(page["id"], topics[1], days_ago=1, status="ready")

    sched.instagram_job()

    assert ig_api["published"] == []


def test_tanpa_alamat_publik_menunggu_dan_menjelaskan(page, make_post, topics, ig_api, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)

    sched.instagram_job()

    assert ig_api["published"] == []
    db.refresh(post)
    assert post.ig_status is None                          # belum diklaim, dikirim nanti
    halaman = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    assert "PUBLIC_BASE_URL" in halaman.ig_last_error


def test_gagal_dicoba_lagi_maksimal_tiga_kali(page, make_post, topics, ig_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    ig_api["result"] = {"success": False, "message": "(#9004) The media could not be fetched"}

    for _ in range(5):
        sched.instagram_job()

    db.refresh(post)
    assert post.ig_status == "failed" and post.ig_attempts == ig.IG_MAX_ATTEMPTS
    assert "could not be fetched" in post.ig_error


def test_koneksi_putus_saat_publish_tidak_diulang(page, make_post, topics, ig_api, public_url, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    ig_api["result"] = {"success": False, "message": "Koneksi ke Instagram gagal", "uncertain": True}
    sched.instagram_job()
    ig_api["result"] = None
    sched.instagram_job()

    db.refresh(post)
    assert post.ig_status == "uncertain" and ig_api["published"] == []


def test_terhenti_saat_publish_ditandai_saat_start(page, make_post, topics, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.ig_status = "publishing"
    db.commit()

    assert ig.recover_interrupted_instagram(db) == 1
    db.commit()
    db.refresh(post)
    assert post.ig_status == "uncertain"


# ------------------------------------------------------------ link publik poster


def test_link_poster_publik_hanya_untuk_token_yang_berlaku(page, make_post, topics, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.image_path = str(_poster(900, 1200))
    post.ig_media_token = "t" * 32
    post.ig_media_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
    db.commit()
    anonim = TestClient(app_module.app)                    # tanpa login, seperti server Instagram

    ok = anonim.get(f"/media/ig/{'t' * 32}.jpg")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(ok.content)).size == (960, 1200)

    assert anonim.get(f"/media/ig/{'x' * 32}.jpg").status_code == 404
    assert anonim.get(f"/storage/generated_images/{post.image_filename}").status_code == 401   # tetap terkunci

    post.ig_media_expires = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    db.commit()
    assert anonim.get(f"/media/ig/{'t' * 32}.jpg").status_code == 404


def test_alamat_publik_hanya_dari_https_bukan_localhost(db):
    for lokal in ("http://127.0.0.1:8000/", "https://localhost/", "http://autoposter.example.com/"):
        ig.remember_public_base_url(db, lokal)
    assert ig.public_base_url(db) == ""
    ig.remember_public_base_url(db, "https://autoposter.example.com/")
    assert ig.public_base_url(db) == "https://autoposter.example.com"
    db.query(AppSetting).filter(AppSetting.key == ig.PUBLIC_BASE_URL_KEY).delete()
    db.commit()


# ------------------------------------------------------------ klien Graph API Instagram


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_publish_menunggu_container_selesai_lalu_menayangkan(monkeypatch):
    calls, status = [], iter(["IN_PROGRESS", "FINISHED"])

    def post(url, data=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        return _R({"id": "c1"} if url.endswith("/media") else {"id": "m1"})

    def get(url, params=None, timeout=None):
        if params.get("fields") == "status_code":
            return _R({"status_code": next(status)})
        return _R({"permalink": "https://www.instagram.com/p/abc/"})

    monkeypatch.setattr(fbc.requests, "post", post)
    monkeypatch.setattr(fbc.requests, "get", get)
    res = fbc.publish_photo_to_instagram("1784", "tok", f"{BASE}/media/ig/x.jpg", "caption", sleep=lambda s: None)

    assert res == {"success": True, "media_id": "m1", "permalink": "https://www.instagram.com/p/abc/"}
    assert calls == ["media", "media_publish"]


def test_container_error_tidak_pernah_ditayangkan(monkeypatch):
    calls = []
    monkeypatch.setattr(fbc.requests, "post", lambda url, data=None, timeout=None:
                        (calls.append(url), _R({"id": "c1"}))[1])
    monkeypatch.setattr(fbc.requests, "get", lambda url, params=None, timeout=None: _R({"status_code": "ERROR"}))

    res = fbc.publish_photo_to_instagram("1784", "tok", "u", "c", sleep=lambda s: None)

    assert res["success"] is False and not any(u.endswith("media_publish") for u in calls)
