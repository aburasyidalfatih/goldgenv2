"""
Penyedia OpenAI: pengaturan, pemilihan penyedia per peran, dan klien REST-nya
(tanpa jaringan — requests ditiru).
"""
import base64
import io

import pytest
from PIL import Image

from config import SECRET_MASK
from core import openai_client
from core.imagen_client import generate_poster_image
from database.models import AppSetting


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _set(db, key, value):
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    row.value = value
    db.commit()


@pytest.fixture
def with_openai(db):
    """Kunci OpenAI tersimpan, naskah DAN gambar memakai OpenAI."""
    for k, v in {"openai_api_key": "sk-test", "text_provider": "openai",
                 "image_provider": "openai"}.items():
        _set(db, k, v)
    yield
    for k, v in {"openai_api_key": "", "text_provider": "gemini",
                 "image_provider": "gemini"}.items():
        _set(db, k, v)


def _png_b64(w=40, h=60):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------ pengaturan


def test_kunci_openai_disamarkan(client, with_openai):
    settings = client.get("/api/settings").json()
    assert settings["openai_api_key"] == SECRET_MASK
    assert settings["text_provider"] == "openai"


def test_mengirim_mask_tidak_menghapus_kunci_openai(client, with_openai, db):
    client.post("/api/settings", json={"openai_api_key": SECRET_MASK})
    row = db.query(AppSetting).filter(AppSetting.key == "openai_api_key").first()
    db.refresh(row)
    assert row.value == "sk-test"


def test_penyedia_tidak_dikenal_ditolak(client):
    res = client.post("/api/settings", json={"text_provider": "claude-palsu"})
    assert res.status_code == 422


def test_uji_koneksi_memakai_kunci_tersimpan(client, with_openai, monkeypatch):
    dipakai = []

    def get(url, headers, timeout):
        dipakai.append((url, headers["Authorization"]))
        return FakeResponse(200, {"id": url.rsplit("/", 1)[-1]})

    monkeypatch.setattr(openai_client.requests, "get", get)
    res = client.post("/api/settings/test-openai", json={
        "api_key": SECRET_MASK, "text_model": "gpt-5-mini", "image_model": "gpt-image-1"}).json()

    assert res["success"] is True
    assert {u.rsplit("/", 1)[-1] for u, _ in dipakai} == {"gpt-5-mini", "gpt-image-1"}
    assert all(auth == "Bearer sk-test" for _, auth in dipakai)


def test_uji_koneksi_model_tidak_tersedia(client, with_openai, monkeypatch):
    monkeypatch.setattr(openai_client.requests, "get",
                        lambda url, headers, timeout: FakeResponse(404, {"error": {"message": "nope"}}))
    res = client.post("/api/settings/test-openai", json={"api_key": SECRET_MASK}).json()
    assert res["success"] is False
    assert "tidak tersedia" in res["message"]


# ------------------------------------------------------------ pemilihan penyedia


def test_generate_memakai_openai_bila_dipilih(client, make_page, fake_gemini, with_openai):
    make_page("111")
    res = client.post("/api/generate", json={"topic_id": "auto"}).json()

    assert res["success"], res
    assert fake_gemini["calls"][0]["provider"] == "openai"
    assert fake_gemini["calls"][0]["api_key"] == "sk-test"
    assert fake_gemini["calls"][0]["model"] == "gpt-5-mini"
    assert fake_gemini["images"][0] == {"provider": "openai", "api_key": "sk-test",
                                        "model": "gpt-image-1"}


def test_penyedia_campuran_naskah_gemini_gambar_openai(client, make_page, fake_gemini,
                                                     with_gemini_key, with_openai, db):
    _set(db, "text_provider", "gemini")
    make_page("111")
    assert client.post("/api/generate", json={"topic_id": "auto"}).json()["success"]

    assert fake_gemini["calls"][0]["provider"] == "gemini"
    assert fake_gemini["calls"][0]["api_key"] == "dummy-key"
    assert fake_gemini["images"][0]["provider"] == "openai"


def test_openai_dipilih_tanpa_kunci_ditolak_dengan_jelas(client, make_page, fake_gemini,
                                                        with_gemini_key, db):
    _set(db, "image_provider", "openai")
    try:
        make_page("111")
        res = client.post("/api/generate", json={"topic_id": "auto"}).json()
        assert res["success"] is False
        assert "OpenAI API Key" in res["message"]
        assert fake_gemini["calls"] == []   # tidak membuang kuota naskah lebih dulu
    finally:
        _set(db, "image_provider", "gemini")


# ------------------------------------------------------------ klien REST


def test_naskah_json_dan_coba_ulang_tanpa_temperature(monkeypatch):
    bodies = []

    def post(url, headers, json, timeout):
        bodies.append(dict(json))
        if "temperature" in json:
            return FakeResponse(400, {"error": {"message": "Unsupported value: 'temperature'"}})
        return FakeResponse(200, {"choices": [{"finish_reason": "stop",
                                               "message": {"content": '{"caption": "ok"}'}}]})

    monkeypatch.setattr(openai_client.requests, "post", post)
    raw = openai_client.complete_json("sk-x", "gpt-5-mini", "Balas JSON", "topik", 0.7)

    assert raw == '{"caption": "ok"}'
    assert len(bodies) == 2 and "temperature" not in bodies[1]
    assert bodies[0]["response_format"] == {"type": "json_object"}


def test_error_openai_tidak_membocorkan_kunci(monkeypatch):
    monkeypatch.setattr(openai_client.requests, "post", lambda url, headers, json, timeout:
                        FakeResponse(401, {"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(RuntimeError) as err:
        openai_client.complete_json("sk-rahasia-123", "gpt-5-mini", "s", "u", 0.7)
    assert "Incorrect API key" in str(err.value)
    assert "sk-rahasia-123" not in str(err.value)


@pytest.mark.parametrize("ratio, model, size", [
    ("3:4", "gpt-image-1", "1024x1536"),
    ("1:1", "gpt-image-1", "1024x1024"),
    ("16:9", "gpt-image-1", "1536x1024"),
    ("4:5", "dall-e-3", "1024x1792"),
])
def test_ukuran_kanvas_mengikuti_rasio(monkeypatch, ratio, model, size):
    dikirim = {}

    def post(url, headers, json, timeout):
        dikirim.update(json)
        return FakeResponse(200, {"data": [{"b64_json": _png_b64()}]})

    monkeypatch.setattr(openai_client.requests, "post", post)
    openai_client.generate_image_bytes("sk-x", model, "poster", ratio)

    assert dikirim["size"] == size
    assert ("response_format" in dikirim) == (model == "dall-e-3")


def test_poster_openai_disimpan_sebagai_jpeg(client, monkeypatch):
    monkeypatch.setattr(openai_client.requests, "post", lambda url, headers, json, timeout:
                        FakeResponse(200, {"data": [{"b64_json": _png_b64()}]}))

    filename, path = generate_poster_image("sk-x", "poster", "3:4", "gpt-image-1", provider="openai")

    assert filename.endswith(".jpg")
    with Image.open(path) as img:
        assert img.format == "JPEG" and img.mode == "RGB"


def test_poster_openai_gagal_memberi_pesan_jelas(client, monkeypatch):
    monkeypatch.setattr(openai_client.requests, "post", lambda url, headers, json, timeout:
                        FakeResponse(400, {"error": {"message": "safety system rejected"}}))
    with pytest.raises(RuntimeError, match="safety system rejected"):
        generate_poster_image("sk-x", "poster", "3:4", "gpt-image-1", provider="openai")
