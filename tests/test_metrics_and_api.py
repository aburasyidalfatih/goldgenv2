"""Metric synchronisation, API surface, and data-hygiene guarantees."""
from datetime import datetime, timezone

from config import SECRET_MASK
from core.feedback_loop import update_all_post_metrics


# ------------------------------------------------------------ metrics


def test_sinkronisasi_dibatasi_30_hari_terakhir(client, make_page, make_post, topics, db, fake_metrics):
    """Regresi: dulu seluruh postingan sepanjang masa ditarik ulang tiap malam."""
    page = make_page("111")
    make_post(page["id"], topics[0], days_ago=200, reach=100)
    make_post(page["id"], topics[1], days_ago=120, reach=100)
    make_post(page["id"], topics[2], days_ago=5, reach=100)
    make_post(page["id"], topics[3], days_ago=1, reach=100)

    update_all_post_metrics(db, "111", "token-111", page["id"])

    assert len(fake_metrics["calls"]) == 2, "hanya postingan <=30 hari"


def test_postingan_belum_pernah_diukur_selalu_ikut(client, make_page, make_post, topics, db, fake_metrics):
    page = make_page("111")
    make_post(page["id"], topics[0], days_ago=150, reach=100, with_metrics=False)
    make_post(page["id"], topics[1], days_ago=150, reach=100, with_metrics=True)

    update_all_post_metrics(db, "111", "token-111", page["id"])

    assert len(fake_metrics["calls"]) == 1


def test_refresh_penuh_bisa_dipaksa(client, make_page, make_post, topics, db, fake_metrics):
    page = make_page("111")
    for umur in (200, 120, 5):
        make_post(page["id"], topics[0], days_ago=umur, reach=100)

    update_all_post_metrics(db, "111", "token-111", page["id"], max_age_days=None)

    assert len(fake_metrics["calls"]) == 3


def test_sinkronisasi_hanya_menyentuh_postingan_halamannya(client, make_page, make_post,
                                                           topics, db, fake_metrics):
    a = make_page("111")
    b = make_page("222")
    make_post(a["id"], topics[0], days_ago=2, reach=100)
    make_post(b["id"], topics[1], days_ago=2, reach=100)

    update_all_post_metrics(db, "111", "token-111", a["id"])

    assert len(fake_metrics["calls"]) == 1


def test_sinkronisasi_tanpa_fanspage_memberi_pesan(client):
    res = client.post("/api/analytics/sync").json()
    assert res["success"] is False
    assert "Belum ada Fanspage" in res["message"]


def test_metrik_tersimpan_dan_masuk_ringkasan(client, make_page, make_post, topics, db, fake_metrics):
    page = make_page("111")
    post = make_post(page["id"], topics[0], days_ago=2, reach=0, with_metrics=False)
    fake_metrics["values"][post.fb_post_id] = {
        "reactions": 50, "comments": 10, "shares": 5, "reach": 4000, "impressions": 5200
    }

    client.post(f"/api/analytics/sync?page={page['id']}")
    ringkasan = client.get(f"/api/analytics/summary?page={page['id']}").json()

    assert ringkasan["total_reach"] == 4000
    assert ringkasan["total_engagement"] == 65


# ------------------------------------------------------------ API surface


def test_rahasia_tidak_pernah_bocor_di_endpoint_manapun(client, make_page, with_gemini_key):
    make_page("111")
    rahasia = ["dummy-key", "token-111"]

    for path in ["/api/settings", "/api/pages", "/api/topics", "/api/posts",
                 "/api/analytics/summary", "/api/scheduler/status", "/api/topics/curriculum"]:
        body = client.get(path).text
        for nilai in rahasia:
            assert nilai not in body, f"{nilai} bocor di {path}"


def test_gemini_key_ditampilkan_sebagai_mask(client, with_gemini_key):
    assert client.get("/api/settings").json()["gemini_api_key"] == SECRET_MASK


def test_mengirim_mask_tidak_menghapus_key_tersimpan(client, with_gemini_key, db):
    from database.models import AppSetting

    client.post("/api/settings", json={"gemini_api_key": SECRET_MASK})

    row = db.query(AppSetting).filter(AppSetting.key == "gemini_api_key").first()
    db.refresh(row)
    assert row.value == "dummy-key"


def test_timestamp_selalu_bertanda_utc(client, make_page, make_post, topics):
    """Regresi: waktu dulu meleset 7 jam karena tanpa penanda zona waktu."""
    page = make_page("111")
    make_post(page["id"], topics[0], days_ago=1, reach=100)

    post = client.get("/api/posts").json()["items"][0]
    halaman = client.get("/api/pages").json()[0] if isinstance(client.get("/api/pages").json(), list) \
        else client.get("/api/pages").json()["pages"][0]

    assert post["created_at"].endswith("+00:00")
    assert post["published_at"].endswith("+00:00")
    assert halaman["last_verified_at"].endswith("+00:00")


def test_endpoint_facebook_lama_sudah_dihapus(client):
    res = client.post("/api/settings/test-facebook", json={"page_id": "1", "access_token": "x"})
    assert res.status_code == 404


def test_halaman_dashboard_terbuka(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "GOLD AI AUTOPOSTER" in res.text


def test_bobot_topik_mengikuti_halaman_terpilih(client, make_page, make_post, topics, db):
    a = make_page("111")
    b = make_page("222")
    make_post(a["id"], topics[3], days_ago=3, reach=20000)
    make_post(b["id"], topics[8], days_ago=3, reach=20000)
    client.post("/api/feedback/optimize")

    teratas_a = client.get(f"/api/topics?page={a['id']}").json()[0]
    teratas_b = client.get(f"/api/topics?page={b['id']}").json()[0]

    assert teratas_a["id"] == topics[3].id
    assert teratas_b["id"] == topics[8].id
    assert teratas_a["scoped_to_page"] is True
