"""Per-Fanspage poster identity: color theme in the prompt, name watermark on the image."""
from PIL import Image

from core.poster_style import DEFAULT_THEME, THEMES, apply_watermark, theme_prompt
from core import imagen_client


def test_cukup_tema_berbeda_untuk_lima_fanspage():
    assert len(THEMES) >= 5, "pemilik punya 5 Fanspage, masing-masing perlu warna sendiri"
    for key, theme in THEMES.items():
        assert theme["label"] and theme["palette"] and len(theme["swatch"]) >= 3, key
    assert len({t["palette"] for t in THEMES.values()}) == len(THEMES)
    assert len({tuple(t["swatch"]) for t in THEMES.values()}) == len(THEMES)


def test_palet_tema_masuk_ke_prompt_gambar():
    prompt = theme_prompt("midnight_gold")
    assert THEMES["midnight_gold"]["palette"] in prompt
    assert "overrides" in prompt                       # mengalahkan "parchment" di blueprint topik
    assert THEMES[DEFAULT_THEME]["palette"] in theme_prompt("tema-ngawur")


def test_tema_fanspage_tersimpan_dan_divalidasi(client, make_page):
    page = make_page("111")
    assert page["color_theme"] == DEFAULT_THEME

    ok = client.patch(f"/api/pages/{page['id']}", json={"color_theme": "forest_copper"}).json()
    salah = client.patch(f"/api/pages/{page['id']}", json={"color_theme": "pelangi"}).json()

    assert ok["success"] and ok["page"]["color_theme"] == "forest_copper"
    assert salah["success"] is False
    assert client.get("/api/pages").json()["pages"][0]["color_theme"] == "forest_copper"


def test_watermark_tertempel_di_pojok_kanan_bawah(tmp_dir):
    path = tmp_dir / "poster.jpg"
    Image.new("RGB", (900, 1200), (233, 220, 188)).save(path, "JPEG")

    apply_watermark(path, "Old Man Nugget")

    with Image.open(path) as img:
        assert img.size == (900, 1200)
        corner = img.crop((600, 1100, 900, 1200)).convert("L")
        top_left = img.crop((0, 0, 300, 100)).convert("L")
        assert corner.getextrema()[0] < 150, "pojok kanan bawah harus berisi label gelap"
        assert top_left.getextrema()[0] > 200, "bagian lain tidak boleh tersentuh"


def test_tanpa_nama_tidak_ada_watermark(tmp_dir):
    path = tmp_dir / "poster.jpg"
    Image.new("RGB", (300, 400), (200, 200, 200)).save(path, "JPEG")
    before = path.read_bytes()
    apply_watermark(path, "  ")
    assert path.read_bytes() == before


def test_poster_gemini_memakai_tema_dan_diberi_watermark(tmp_dir, monkeypatch):
    dipakai = {}

    def fake_route(client, model, prompt, ratio, filepath):
        dipakai["prompt"] = prompt
        Image.new("RGB", (600, 800), (240, 240, 240)).save(filepath, "JPEG")
        return True

    monkeypatch.setattr(imagen_client, "_generate_via_gemini", fake_route)
    monkeypatch.setattr(imagen_client, "IMAGES_DIR", tmp_dir)
    monkeypatch.setattr(imagen_client.genai, "Client", lambda api_key, **kw: object())
    _, path = imagen_client.generate_poster_image("k", "poster", "3:4", "gemini-3.1-flash-image",
                                                  theme="desert_sunset", watermark="Old Man Nugget")

    assert THEMES["desert_sunset"]["palette"] in dipakai["prompt"]
    with Image.open(path) as img:
        assert img.crop((400, 740, 600, 800)).convert("L").getextrema()[0] < 150


def test_generate_dan_render_ulang_meneruskan_tema_dan_nama(client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111", color_theme="midnight_gold")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    client.post(f"/api/posts/{post['id']}/regenerate-image")

    for kwargs in fake_gemini["image_kwargs"]:
        assert kwargs["theme"] == "midnight_gold"
        assert kwargs["watermark"] == page["name"]
    assert len(fake_gemini["image_kwargs"]) == 2
