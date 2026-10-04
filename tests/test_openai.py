"""
Penyedia OpenAI: pengaturan, pemilihan penyedia per peran, dan klien REST-nya
(tanpa jaringan — requests ditiru).
"""
import base64
import io

import pytest
from PIL import Image

from core import openai_client
from core.imagen_client import generate_poster_image
from database.models import AppSetting
from config import SECRET_MASK, DEFAULT_OPENAI_IMAGE_MODEL, DEFAULT_OPENAI_TEXT_MODEL, OPENAI_RETIRED_MODELS


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
    assert fake_gemini["calls"][0]["model"] == DEFAULT_OPENAI_TEXT_MODEL
    assert fake_gemini["images"][0] == {"provider": "openai", "api_key": "sk-test",
                                        "model": DEFAULT_OPENAI_IMAGE_MODEL}


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



def test_model_openai_yang_dihentikan_diganti_saat_start(client, db):
    """gpt-image-1 berhenti 23 Okt 2026, gpt-5-mini 11 Des 2026: setelan lama tidak boleh jadi gagal total."""
    import app as app_module
    from database.models import AppSetting
    rows = {r.key: r for r in db.query(AppSetting).filter(
        AppSetting.key.in_(["openai_text_model", "openai_image_model"]))}
    rows["openai_text_model"].value, rows["openai_image_model"].value = "gpt-5-mini", "gpt-image-1"
    db.commit()

    changed = app_module.replace_retired_openai_models(db)

    assert changed == {"openai_text_model": ("gpt-5-mini", DEFAULT_OPENAI_TEXT_MODEL),
                       "openai_image_model": ("gpt-image-1", DEFAULT_OPENAI_IMAGE_MODEL)}
    settings = client.get("/api/settings").json()
    assert (settings["openai_text_model"], settings["openai_image_model"]) == (
        DEFAULT_OPENAI_TEXT_MODEL, DEFAULT_OPENAI_IMAGE_MODEL)


def test_model_pilihan_sendiri_yang_masih_aktif_tidak_disentuh(client, db):
    import app as app_module
    from database.models import AppSetting
    row = db.query(AppSetting).filter(AppSetting.key == "openai_text_model").first()
    row.value = "gpt-6-luna"
    db.commit()

    assert app_module.replace_retired_openai_models(db) == {}
    db.refresh(row)
    assert row.value == "gpt-6-luna"
    assert DEFAULT_OPENAI_TEXT_MODEL not in OPENAI_RETIRED_MODELS
    assert DEFAULT_OPENAI_IMAGE_MODEL not in OPENAI_RETIRED_MODELS


# ------------------------------------------------------------ kendali biaya


def test_tingkat_penalaran_dikirim_dan_dibuang_bila_model_menolak(monkeypatch):
    bodies = []

    def post(url, headers, json, timeout):
        bodies.append(dict(json))
        if "reasoning_effort" in json:
            return FakeResponse(400, {"error": {"message": "Unrecognized request argument: reasoning_effort"}})
        return FakeResponse(200, {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})

    monkeypatch.setattr(openai_client.requests, "post", post)
    openai_client.complete_json("sk-x", "model-lama", "s", "u", 0.7, reasoning="low")

    assert bodies[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies[-1]
    assert bodies[-1]["temperature"] == 0.7      # parameter lain tidak ikut dibuang


def test_temperature_dan_penalaran_ditolak_berurutan_tetap_berhasil(monkeypatch):
    calls = []

    def post(url, headers, json, timeout):
        calls.append(dict(json))
        if "temperature" in json:
            return FakeResponse(400, {"error": {"message": "Unsupported value: 'temperature'"}})
        return FakeResponse(200, {"choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}]})

    monkeypatch.setattr(openai_client.requests, "post", post)
    assert openai_client.complete_json("sk-x", "gpt-6.1-sol", "s", "u", 0.7, reasoning="low") == '{"a": 1}'
    assert calls[-1]["reasoning_effort"] == "low" and "temperature" not in calls[-1]


def test_kualitas_gambar_dikirim_kecuali_dall_e(monkeypatch):
    dikirim = []

    def post(url, headers, json, timeout):
        dikirim.append(dict(json))
        return FakeResponse(200, {"data": [{"b64_json": _png_b64()}]})

    monkeypatch.setattr(openai_client.requests, "post", post)
    openai_client.generate_image_bytes("sk-x", "gpt-image-2.5-flare", "poster", "3:4", quality="medium")
    openai_client.generate_image_bytes("sk-x", "dall-e-3", "poster", "3:4", quality="medium")

    assert dikirim[0]["quality"] == "medium"
    assert "quality" not in dikirim[1]


def test_setelan_biaya_sampai_ke_pemanggilan_api(client, make_page, fake_gemini, with_openai, db):
    """Default hemat: penalaran rendah untuk teks, kualitas sedang untuk gambar OpenAI."""
    from core.ai_provider import ai_backend
    make_page("111")

    assert ai_backend(db, "text")["reasoning"] == "low"
    assert ai_backend(db, "image")["quality"] == "medium"
    _set(db, "text_reasoning", "ngawur")
    assert ai_backend(db, "text")["reasoning"] == "low", "nilai tak dikenal jatuh ke default hemat"
    _set(db, "text_reasoning", "low")


def test_gemini_menerima_tingkat_berpikir_dan_mundur_bila_tidak_didukung(monkeypatch):
    import core.ai_provider as provider

    configs = []

    class Models:
        def generate_content(self, model, contents, config):
            configs.append(config.thinking_config)
            if config.thinking_config is not None:
                raise ValueError("thinking_level is not supported for this model")
            return type("R", (), {"text": '{"ok": true}'})()

    monkeypatch.setattr(provider.genai, "Client", lambda api_key, **kw: type("C", (), {"models": Models()})())
    raw = provider.complete_json("gemini", "k", "gemini-2.5-flash", "s", "u", 0.7, reasoning="low")

    assert raw == '{"ok": true}'
    assert configs[0].thinking_level.value == "LOW" and configs[1] is None


def test_rasio_poster_dikirim_ke_model_gambar_gemini(tmp_dir):
    """Regresi: rasio Fanspage dulu tidak pernah dikirim, sehingga Gemini memakai rasionya sendiri."""
    from core import imagen_client
    dipakai = {}

    class Models:
        def generate_content(self, model, contents, config):
            dipakai["rasio"] = config.image_config.aspect_ratio
            return type("R", (), {"candidates": []})()

    client = type("C", (), {"models": Models()})()
    imagen_client._generate_via_gemini(client, "gemini-3.1-flash-image", "poster", "3:4", tmp_dir / "x.jpg")

    assert dipakai["rasio"] == "3:4"
