"""Coba ulang otomatis bila layanan AI gagal sesaat (timeout, sibuk) saat membuat konten autopilot."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import requests
from google.genai import errors as genai_errors

import scheduler as sched
from core import openai_client
from core.ai_provider import is_transient_ai_error
from database.models import FacebookPage, Post

TIMEOUT_OPENAI = requests.exceptions.ReadTimeout(
    "HTTPSConnectionPool(host='api.openai.com', port=443): Read timed out. (read timeout=120)")


def _dibungkus(asal):
    """Seperti imagen_client: galat asli dibungkus RuntimeError `from err`."""
    try:
        raise RuntimeError(f"Gagal menghasilkan gambar. model: {asal}") from asal
    except RuntimeError as e:
        return e


@pytest.mark.parametrize("galat, sementara", [
    (TIMEOUT_OPENAI, True),
    (requests.exceptions.ConnectionError("koneksi putus"), True),
    (_dibungkus(requests.exceptions.ReadTimeout("gambar lambat")), True),
    (openai_client.OpenAIUnavailable("HTTP 503"), True),
    (httpx.ReadTimeout("gemini lambat"), True),
    (genai_errors.ServerError(503, {"error": {"message": "overloaded"}}), True),
    (genai_errors.ClientError(429, {"error": {"message": "rate limit"}}), True),
    # Menunggu tidak memperbaiki ini: pengguna harus bertindak, jadi langsung email.
    (genai_errors.ClientError(400, {"error": {"message": "API key not valid"}}), False),
    (RuntimeError("OpenAI menolak permintaan: Incorrect API key provided"), False),
    (_dibungkus(RuntimeError("OpenAI menolak permintaan: content policy")), False),
    (ValueError("JSON rusak"), False),
])
def test_klasifikasi_galat_ai_sementara(galat, sementara):
    assert is_transient_ai_error(galat) is sementara


class _Respons:
    def __init__(self, status, error=None):
        self.status_code = status
        self._body = {"error": error} if error else {}

    def json(self):
        return self._body


@pytest.mark.parametrize("status, error, sementara", [
    (500, {"message": "server error"}, True),
    (503, None, True),
    (429, {"message": "Rate limit reached", "code": "rate_limit_exceeded"}, True),
    (429, {"message": "You exceeded your current quota", "type": "insufficient_quota",
           "code": "insufficient_quota"}, False),
    (401, {"message": "Incorrect API key provided"}, False),
])
def test_openai_membedakan_sibuk_dari_ditolak(status, error, sementara):
    with pytest.raises(RuntimeError) as info:
        openai_client._raise_for_error(_Respons(status, error))
    assert isinstance(info.value, openai_client.OpenAIUnavailable) is sementara


def test_openai_tidak_melempar_untuk_jawaban_sukses():
    openai_client._raise_for_error(_Respons(200))


@pytest.fixture
def email(monkeypatch):
    terkirim = []
    monkeypatch.setattr(sched, "notify",
                        lambda db, key, subject, body, **k: terkirim.append((subject, body)) or True)
    return terkirim


@pytest.fixture
def ai(monkeypatch):
    """AI palsu: `gagal` berisi galat yang dilempar berurutan; setelah habis, konten jadi."""
    state = {"gagal": [], "panggilan": 0, "upload": 0}

    def konten(*a, **k):
        state["panggilan"] += 1
        if state["gagal"]:
            raise state["gagal"].pop(0)
        return {"visual_title": "Judul", "subtitle": "s", "imagen_prompt": "prompt", "caption": "caption"}

    def publish(**k):
        state["upload"] += 1
        return {"success": True, "post_id": "fb_1", "post_url": "https://fb/1", "message": "ok"}

    monkeypatch.setattr(sched, "generate_post_content", konten)
    monkeypatch.setattr(sched, "generate_poster_image", lambda **k: ("auto.jpg", "auto.jpg"))
    monkeypatch.setattr(sched, "publish_photo_to_page", publish)
    return state


def _job_ulang(page_id):
    return sched.scheduler.get_job(f"{sched.CONTENT_RETRY_JOB_PREFIX}{page_id}")


def test_timeout_ai_tidak_langsung_email_tetapi_slot_dicoba_ulang(client, make_page, topics, with_gemini_key,
                                                                  ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    assert email == []
    assert db.query(Post).count() == 0
    job = _job_ulang(page["id"])
    assert job is not None
    sekarang = datetime.now(timezone.utc)
    assert timedelta(minutes=4) < job.next_run_time - sekarang <= timedelta(minutes=5)
    halaman, batas = job.args
    assert halaman == page["id"]
    assert timedelta(minutes=55) < batas - sekarang <= timedelta(minutes=60)


def test_coba_ulang_menayangkan_saat_ai_pulih(client, make_page, topics, with_gemini_key, ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])

    sched.auto_generate_and_post_job(*_job_ulang(page["id"]).args)   # yang dijalankan job 5 menit kemudian

    post = db.query(Post).one()
    assert post.status == "published"
    assert ai["panggilan"] == 2 and ai["upload"] == 1
    assert email == []


def test_gagal_berulang_tetap_dalam_batas_slot_yang_sama(client, make_page, topics, with_gemini_key,
                                                         ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI, openai_client.OpenAIUnavailable("HTTP 503")]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    _, batas_awal = _job_ulang(page["id"]).args

    sched.auto_generate_and_post_job(page["id"], batas_awal)

    _, batas = _job_ulang(page["id"]).args
    assert batas == batas_awal          # jendela tidak diperpanjang tiap kali gagal
    assert email == []


def test_menyerah_dan_email_sekali_saat_jendela_habis(client, make_page, topics, with_gemini_key,
                                                      ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)
    hampir_habis = datetime.now(timezone.utc) + timedelta(minutes=2)

    sched.auto_generate_and_post_job(page["id"], hampir_habis)

    assert _job_ulang(page["id"]) is None
    assert len(email) == 1
    subjek, isi = email[0]
    assert "gagal membuat konten" in subjek
    assert "dicoba ulang otomatis" in isi and "Read timed out" in isi


def test_galat_permanen_langsung_email_tanpa_coba_ulang(client, make_page, topics, with_gemini_key,
                                                       ai, email, db):
    ai["gagal"] = [RuntimeError("OpenAI menolak permintaan: Incorrect API key provided")]
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    assert _job_ulang(page["id"]) is None
    assert len(email) == 1 and "Incorrect API key" in email[0][1]


def test_timeout_saat_membuat_gambar_juga_dicoba_ulang(client, make_page, topics, with_gemini_key,
                                                      ai, email, monkeypatch, db):
    def gambar(**k):
        raise _dibungkus(requests.exceptions.ReadTimeout("read timeout=300"))

    monkeypatch.setattr(sched, "generate_poster_image", gambar)
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    assert _job_ulang(page["id"]) is not None
    assert email == []


def test_coba_ulang_batal_bila_halaman_sudah_dapat_postingan(client, make_page, topics, with_gemini_key,
                                                            ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    db.add(Post(page_id=page["id"], topic_title="manual", visual_title="manual", prompt_used="p",
                image_filename="m.jpg", image_path="m.jpg", caption="c", status="published"))
    db.commit()

    sched.auto_generate_and_post_job(*_job_ulang(page["id"]).args)

    assert ai["panggilan"] == 1          # tidak membuat konten kedua: slot sudah terisi
    assert db.query(Post).count() == 1


def test_coba_ulang_dilewati_bila_autopilot_dimatikan(client, make_page, topics, with_gemini_key,
                                                     ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])
    db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one().autopilot_enabled = False
    db.commit()

    sched.auto_generate_and_post_job(*_job_ulang(page["id"]).args)

    assert ai["panggilan"] == 1
    assert db.query(Post).count() == 0
    assert email == []


def test_muat_ulang_jadwal_tidak_membatalkan_coba_ulang(client, make_page, topics, with_gemini_key,
                                                       ai, email, db):
    ai["gagal"] = [TIMEOUT_OPENAI]
    page = make_page("111", autopilot_enabled=True)
    sched.auto_generate_and_post_job(page["id"])

    sched.reload_autopost_schedule()       # mis. pengguna menyunting pengaturan Fanspage

    assert _job_ulang(page["id"]) is not None
    assert "content_retry" not in str(sched.get_schedule_status())
