"""Content generation and publishing: the paths that touch the outside world."""
import pytest

from config import IMAGES_DIR
from database.models import Post


def test_generate_memakai_preferensi_halaman(client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111", content_language="en", aspect_ratio="1:1")

    res = client.post("/api/generate", json={"topic_id": "auto", "page_id": page["id"]}).json()

    assert res["success"], res
    assert res["post"]["page_id"] == page["id"]
    assert fake_gemini["calls"][0]["language"] == "en"
    assert fake_gemini["ratios"] == ["1:1"]


def test_generate_tanpa_gemini_key_ditolak_dengan_jelas(client, make_page):
    make_page("111")
    res = client.post("/api/generate", json={"topic_id": "auto"}).json()
    assert res["success"] is False
    assert "Gemini API Key" in res["message"]


def test_topic_id_ngawur_jatuh_ke_auto_select(client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111")
    res = client.post("/api/generate", json={"topic_id": "bukan-angka", "page_id": page["id"]}).json()
    assert res["success"] is True


def test_publish_memakai_caption_milik_postingan_itu(client, make_page, fake_gemini,
                                                     fake_facebook, with_gemini_key):
    """Regresi: dulu publish dari riwayat mengirim caption postingan lain."""
    page = make_page("111")
    satu = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    dua = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]

    client.post(f"/api/posts/{dua['id']}/publish", json={"caption": dua["caption"]})

    terkirim = fake_facebook["published"][-1]
    assert terkirim["caption"] == dua["caption"]
    assert terkirim["caption"] != satu["caption"]


def test_publish_memakai_token_halaman_yang_benar(client, make_page, fake_gemini,
                                                  fake_facebook, with_gemini_key):
    make_page("111")
    b = make_page("222")
    post = client.post("/api/generate", json={"page_id": b["id"]}).json()["post"]

    client.post(f"/api/posts/{post['id']}/publish", json={})

    terkirim = fake_facebook["published"][-1]
    assert terkirim["page_id"] == "222"
    assert terkirim["token"] == "token-222"


def test_publish_dua_kali_ditolak(client, make_page, fake_gemini, fake_facebook, with_gemini_key):
    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    client.post(f"/api/posts/{post['id']}/publish", json={})

    res = client.post(f"/api/posts/{post['id']}/publish", json={}).json()

    assert res["success"] is False
    assert "sudah pernah ditayangkan" in res["message"]
    assert len(fake_facebook["published"]) == 1


def test_publish_gagal_menyimpan_pesan_error(client, make_page, fake_gemini,
                                             fake_facebook, with_gemini_key, db):
    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    fake_facebook["fail_publish"] = True

    res = client.post(f"/api/posts/{post['id']}/publish", json={}).json()

    assert res["success"] is False
    row = db.query(Post).filter(Post.id == post["id"]).first()
    db.refresh(row)
    assert row.status == "failed"
    assert "permission denied" in row.error_message


def test_draft_yatim_tidak_dialihkan_diam_diam(client, make_page, fake_gemini,
                                               fake_facebook, with_gemini_key):
    """Regresi: draft milik halaman terhapus dulu ikut tayang ke halaman lain."""
    a = make_page("111")
    make_page("222")
    make_page("333")
    post = client.post("/api/generate", json={"page_id": a["id"]}).json()["post"]
    client.delete(f"/api/pages/{a['id']}")

    res = client.post(f"/api/posts/{post['id']}/publish", json={}).json()

    assert res["success"] is False
    assert res["needs_page"] is True
    assert fake_facebook["published"] == []


def test_draft_yatim_bisa_tayang_bila_tujuan_disebut(client, make_page, fake_gemini,
                                                     fake_facebook, with_gemini_key):
    a = make_page("111")
    b = make_page("222")
    make_page("333")
    post = client.post("/api/generate", json={"page_id": a["id"]}).json()["post"]
    client.delete(f"/api/pages/{a['id']}")

    res = client.post(f"/api/posts/{post['id']}/publish", json={"page_id": b["id"]}).json()

    assert res["success"] is True
    assert fake_facebook["published"][-1]["page_id"] == "222"


def test_draft_yatim_tetap_terlihat_di_daftar(client, make_page, fake_gemini, with_gemini_key):
    """Regresi: draft yatim dulu hilang dari UI karena selalu difilter per halaman."""
    a = make_page("111")
    b = make_page("222")
    post = client.post("/api/generate", json={"page_id": a["id"]}).json()["post"]
    client.delete(f"/api/pages/{a['id']}")

    terlihat = client.get(f"/api/posts?page={b['id']}").json()["items"]

    yatim = [p for p in terlihat if p["id"] == post["id"]]
    assert yatim and yatim[0]["is_orphan"] is True


def test_render_ulang_memakai_rasio_halaman(client, make_page, fake_gemini, with_gemini_key):
    """Regresi: dulu selalu 3:4 karena membaca setting global."""
    page = make_page("111", aspect_ratio="1:1")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    fake_gemini["ratios"].clear()

    client.post(f"/api/posts/{post['id']}/regenerate-image")

    assert fake_gemini["ratios"] == ["1:1"]


def test_render_ulang_menghapus_poster_lama(client, make_page, fake_gemini, with_gemini_key, db):
    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    lama = db.query(Post).filter(Post.id == post["id"]).first().image_path
    assert (IMAGES_DIR / lama.split("\\")[-1].split("/")[-1]).exists()

    client.post(f"/api/posts/{post['id']}/regenerate-image")

    from pathlib import Path

    assert not Path(lama).exists(), "file poster lama harus dihapus"


def test_hapus_draft_menghapus_filenya(client, make_page, fake_gemini, with_gemini_key, db):
    from pathlib import Path

    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    path = db.query(Post).filter(Post.id == post["id"]).first().image_path

    res = client.delete(f"/api/posts/{post['id']}").json()

    assert res["success"]
    assert not Path(path).exists()
    assert db.query(Post).filter(Post.id == post["id"]).first() is None


def test_postingan_tayang_tidak_bisa_dihapus(client, make_page, fake_gemini,
                                             fake_facebook, with_gemini_key):
    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    client.post(f"/api/posts/{post['id']}/publish", json={})

    res = client.delete(f"/api/posts/{post['id']}").json()

    assert res["success"] is False
    assert "riwayat metrik" in res["message"]


@pytest.mark.parametrize("endpoint", [
    "/api/posts/9999/publish",
    "/api/posts/9999/regenerate-image",
])
def test_postingan_tidak_ada_memberi_404(client, endpoint):
    assert client.post(endpoint, json={}).status_code == 404


def test_daftar_riwayat_berhalaman(client, make_page, make_post, topics):
    """Riwayat panjang tidak boleh dikirim sekaligus."""
    page = make_page("111")
    for i in range(12):
        make_post(page["id"], topics[i % len(topics)], days_ago=i + 1, reach=100)

    halaman_pertama = client.get(f"/api/posts?page={page['id']}&limit=5").json()

    assert len(halaman_pertama["items"]) == 5
    assert halaman_pertama["total"] == 12
    assert halaman_pertama["has_more"] is True

    halaman_kedua = client.get(f"/api/posts?page={page['id']}&limit=5&offset=5").json()
    assert len(halaman_kedua["items"]) == 5
    id_pertama = {p["id"] for p in halaman_pertama["items"]}
    id_kedua = {p["id"] for p in halaman_kedua["items"]}
    assert id_pertama.isdisjoint(id_kedua), "halaman tidak boleh tumpang tindih"

    terakhir = client.get(f"/api/posts?page={page['id']}&limit=5&offset=10").json()
    assert terakhir["has_more"] is False


def test_daftar_tidak_mengirim_caption_dan_prompt_penuh(client, make_page, fake_gemini, with_gemini_key):
    """Regresi skala: dua field ini dulu membengkakkan muatan sampai ratusan KB."""
    page = make_page("111")
    client.post("/api/generate", json={"page_id": page["id"]})

    item = client.get(f"/api/posts?page={page['id']}").json()["items"][0]

    assert "caption" not in item
    assert "prompt_used" not in item
    assert item["caption_preview"]


def test_detail_postingan_mengirim_isi_lengkap(client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111")
    dibuat = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]

    detail = client.get(f"/api/posts/{dibuat['id']}").json()

    assert detail["caption"] == dibuat["caption"]
    assert detail["prompt_used"] == dibuat["prompt_used"]


def test_detail_postingan_tidak_ada_memberi_404(client):
    assert client.get("/api/posts/9999").status_code == 404


def test_limit_dibatasi_agar_tidak_bisa_menarik_semuanya(client, make_page):
    page = make_page("111")
    res = client.get(f"/api/posts?page={page['id']}&limit=99999").json()
    assert res["limit"] == 200


def test_gambar_dihapus_bila_penyimpanan_gagal(client, make_page, fake_gemini,
                                               with_gemini_key, monkeypatch):
    """Regresi: job yang gagal setelah render meninggalkan poster yatim di disk."""
    import app as app_module
    from config import IMAGES_DIR
    from pathlib import Path

    page = make_page("111")
    sebelum = {p.name for p in IMAGES_DIR.iterdir()} if IMAGES_DIR.exists() else set()

    def gagal_simpan(*a, **k):
        raise RuntimeError("database penuh")

    monkeypatch.setattr(app_module, "mark_topic_used", gagal_simpan)
    res = client.post("/api/generate", json={"page_id": page["id"]}).json()

    assert res["success"] is False
    sesudah = {p.name for p in IMAGES_DIR.iterdir()} if IMAGES_DIR.exists() else set()
    assert sesudah - sebelum == set(), f"poster yatim tertinggal: {sesudah - sebelum}"


def test_sapu_bersih_menghapus_gambar_tanpa_pemilik(client, make_page, fake_gemini,
                                                    with_gemini_key, db):
    import os
    import time
    from config import IMAGES_DIR
    from core.maintenance import cleanup_orphan_images

    page = make_page("111")
    dipakai = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]

    yatim = IMAGES_DIR / "poster_yatim_lama.jpg"
    yatim.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 500)
    lama = time.time() - 60 * 60 * 48          # dua hari lalu
    os.utime(yatim, (lama, lama))

    hasil = cleanup_orphan_images(db)

    assert hasil["removed"] == 1
    assert not yatim.exists()
    masih_ada = client.get(f"/api/posts/{dipakai['id']}").json()
    from pathlib import Path
    assert Path(masih_ada["image_url"]).name  # postingan yang dipakai tidak tersentuh


def test_sapu_bersih_tidak_menyentuh_gambar_baru(client, db):
    """Gambar yang baru dirender mungkin barisnya belum tersimpan."""
    from config import IMAGES_DIR
    from core.maintenance import cleanup_orphan_images

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    baru = IMAGES_DIR / "poster_baru_saja.jpg"
    baru.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 100)

    hasil = cleanup_orphan_images(db)

    assert hasil["removed"] == 0
    assert baru.exists()
    baru.unlink()


def test_buat_ulang_caption_memakai_bahasa_halaman_dan_mempertahankan_gambar(
        client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111", content_language="id")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]

    client.patch(f"/api/pages/{page['id']}", json={"content_language": "en"})
    res = client.post(f"/api/posts/{post['id']}/regenerate-caption").json()

    assert res["success"], res
    assert res["language"] == "en"
    assert fake_gemini["calls"][-1]["language"] == "en"
    detail = client.get(f"/api/posts/{post['id']}").json()
    assert detail["caption"] == res["caption"] != post["caption"]
    assert detail["image_url"] == post["image_url"]
    assert len(fake_gemini["ratios"]) == 1   # no new poster was rendered


def test_buat_ulang_caption_ditolak_untuk_postingan_tayang(client, make_page, fake_gemini,
                                                           fake_facebook, with_gemini_key):
    page = make_page("111")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    client.post(f"/api/posts/{post['id']}/publish", json={"caption": post["caption"]})

    res = client.post(f"/api/posts/{post['id']}/regenerate-caption")

    assert res.status_code == 409


def test_buat_ulang_caption_tidak_menimpa_dengan_template(client, make_page, fake_gemini,
                                                          with_gemini_key, monkeypatch):
    import app as app_module
    page = make_page("111", content_language="en")
    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]
    monkeypatch.setattr(app_module, "generate_post_content",
                        lambda api_key, topic_dict, language="id", **kw:
                        app_module.template_content(topic_dict, language))

    res = client.post(f"/api/posts/{post['id']}/regenerate-caption").json()

    assert res["success"] is False
    assert client.get(f"/api/posts/{post['id']}").json()["caption"] == post["caption"]


def test_fanspage_baru_default_caption_bahasa_inggris(client, make_page, fake_gemini, with_gemini_key):
    page = make_page("111")
    assert page["content_language"] == "en"

    post = client.post("/api/generate", json={"page_id": page["id"]}).json()["post"]

    assert fake_gemini["calls"][-1]["language"] == "en"
    assert post["caption"].startswith("caption #1 en")
