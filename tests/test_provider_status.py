"""'Uji Koneksi' status must survive a page reload, and lapse when key or model changes."""
import app as app_module


def _fake_gemini(monkeypatch, ok=True):
    monkeypatch.setattr(app_module, "test_gemini_key",
                        lambda key, model: {"success": ok, "message": "ok" if ok else "gagal"})


def test_status_terhubung_bertahan_setelah_reload(client, monkeypatch):
    _fake_gemini(monkeypatch)
    client.post("/api/settings", json={"gemini_api_key": "AIza-kunci", "gemini_text_model": "gemini-x"})
    assert client.get("/api/settings").json()["gemini_status"] == "Belum diverifikasi"

    # The UI tests a saved key by sending the mask back.
    client.post("/api/settings/test-gemini", json={"api_key": app_module.SECRET_MASK, "model_name": "gemini-x"})

    settings = client.get("/api/settings").json()
    assert settings["gemini_status"] == "Terhubung"
    assert "gemini_verified_fp" not in settings   # internal, never sent to the browser


def test_status_gugur_bila_key_atau_model_diganti(client, monkeypatch):
    _fake_gemini(monkeypatch)
    client.post("/api/settings", json={"gemini_api_key": "AIza-kunci", "gemini_text_model": "gemini-x"})
    client.post("/api/settings/test-gemini", json={"api_key": "AIza-kunci", "model_name": "gemini-x"})

    client.post("/api/settings", json={"gemini_text_model": "gemini-y"})
    assert client.get("/api/settings").json()["gemini_status"] == "Belum diverifikasi"

    client.post("/api/settings", json={"gemini_text_model": "gemini-x", "gemini_api_key": "AIza-lain"})
    assert client.get("/api/settings").json()["gemini_status"] == "Belum diverifikasi"


def test_uji_gagal_tidak_dianggap_terhubung(client, monkeypatch):
    _fake_gemini(monkeypatch, ok=False)
    client.post("/api/settings", json={"gemini_api_key": "AIza-salah", "gemini_text_model": "gemini-x"})
    client.post("/api/settings/test-gemini", json={"api_key": "AIza-salah", "model_name": "gemini-x"})
    assert client.get("/api/settings").json()["gemini_status"] == "Belum diverifikasi"


def test_status_tidak_bisa_dipalsukan_lewat_form(client):
    client.post("/api/settings", json={"gemini_api_key": "AIza-kunci",
                                       "gemini_status": "Terhubung", "gemini_verified_fp": "palsu"})
    assert client.get("/api/settings").json()["gemini_status"] == "Belum diverifikasi"
