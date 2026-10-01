"""Fanspage management: registration, credentials, per-page settings, deletion."""
from config import SECRET_MASK


def test_menambah_fanspage_mengambil_nama_dan_foto(client, fake_facebook):
    res = client.post("/api/pages", json={"page_id": "111", "access_token": "EAAtoken"}).json()

    assert res["success"]
    assert res["page"]["name"] == "Halaman 111"
    assert res["page"]["picture_url"].endswith("111.jpg")
    assert res["page"]["token_status"] == "Terverifikasi"
    assert fake_facebook["verified"] == [("111", "EAAtoken")]


def test_token_tidak_pernah_dikirim_ke_browser(client, make_page):
    make_page("111")

    pages = client.get("/api/pages").json()["pages"]

    assert pages[0]["access_token"] == SECRET_MASK
    assert pages[0]["has_token"] is True
    # the raw token must not appear anywhere in the response body
    assert "token-111" not in client.get("/api/pages").text


def test_page_id_ganda_ditolak(client, make_page):
    make_page("111")

    res = client.post("/api/pages", json={"page_id": "111", "access_token": "lain"}).json()

    assert res["success"] is False
    assert "sudah terdaftar" in res["message"]


def test_kredensial_salah_ditolak_sebelum_disimpan(client, fake_facebook):
    fake_facebook["fail_verify"].add("999")

    res = client.post("/api/pages", json={"page_id": "999", "access_token": "salah"}).json()

    assert res["success"] is False
    assert client.get("/api/pages").json()["pages"] == []


def test_field_wajib_divalidasi(client):
    res = client.post("/api/pages", json={"page_id": "", "access_token": ""}).json()
    assert res["success"] is False


def test_setiap_halaman_punya_preferensi_sendiri(client, make_page):
    make_page("111", content_language="id", aspect_ratio="3:4", auto_post_times="07:00,17:00")
    make_page("222", content_language="en", aspect_ratio="1:1", auto_post_times="12:30")

    a, b = client.get("/api/pages").json()["pages"]

    assert (a["content_language"], a["aspect_ratio"], a["auto_post_times"]) == ("id", "3:4", "07:00,17:00")
    assert (b["content_language"], b["aspect_ratio"], b["auto_post_times"]) == ("en", "1:1", "12:30")


def test_jam_posting_tidak_valid_ditolak(client, make_page):
    page = make_page("111")

    res = client.patch(f"/api/pages/{page['id']}", json={"auto_post_times": "25:99"}).json()

    assert res["success"] is False
    assert "Format jam posting tidak valid" in res["message"]
    # the stored value must be untouched
    assert client.get("/api/pages").json()["pages"][0]["auto_post_times"] == "10:00,19:00"


def test_mask_tidak_menimpa_token_tersimpan(client, make_page, db):
    from database.models import FacebookPage

    page = make_page("111")
    client.patch(f"/api/pages/{page['id']}", json={"access_token": SECRET_MASK})

    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).first()
    db.refresh(row)
    assert row.access_token == "token-111"


def test_ganti_token_menandai_perlu_verifikasi_ulang(client, make_page, db):
    from database.models import FacebookPage

    page = make_page("111")
    client.patch(f"/api/pages/{page['id']}", json={"access_token": "EAAbaru"})

    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).first()
    db.refresh(row)
    assert row.access_token == "EAAbaru"
    assert row.token_status == "Belum diverifikasi"


def test_hapus_halaman_menyimpan_riwayat_postingan(client, make_page, make_post, topics, db):
    from database.models import Post

    page = make_page("111")
    make_post(page["id"], topics[0], reach=5000)

    res = client.delete(f"/api/pages/{page['id']}").json()

    assert res["success"]
    assert "tetap tersimpan sebagai riwayat" in res["message"]
    surviving = db.query(Post).all()
    assert len(surviving) == 1
    assert surviving[0].page_id is None  # detached, not deleted


def test_hapus_halaman_membersihkan_pembelajarannya(client, make_page, make_post, topics, db):
    from database.models import PageTopicWeight

    page = make_page("111")
    make_post(page["id"], topics[0], reach=5000)
    client.post(f"/api/feedback/optimize?page={page['id']}")
    assert db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page["id"]).count() > 0

    client.delete(f"/api/pages/{page['id']}")

    assert db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page["id"]).count() == 0


def test_halaman_tidak_ada_memberi_pesan_jelas(client):
    assert client.delete("/api/pages/9999").json()["success"] is False
    assert client.patch("/api/pages/9999", json={"name": "x"}).json()["success"] is False
    assert client.post("/api/pages/9999/verify").json()["success"] is False
