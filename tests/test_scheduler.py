"""Background scheduling: one independent cron per Fanspage."""
import pytest

import scheduler as sched
from database.models import Post


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("10:00,19:00", [(10, 0), (19, 0)]),
        (" 07:30 , 12:00 ", [(7, 30), (12, 0)]),
        ("10:00,ngawur,19:00", [(10, 0), (19, 0)]),
        ("25:00,10:61", []),
        ("", []),
        (None, []),
    ],
)
def test_parsing_jam_posting(raw, expected):
    assert sched.parse_post_times(raw) == expected


def test_tiap_halaman_dapat_job_sendiri(client, make_page):
    make_page("111", auto_post_times="07:00,17:00", autopilot_enabled=True)
    make_page("222", auto_post_times="12:30", autopilot_enabled=True)

    status = client.get("/api/scheduler/status").json()

    jadwal = {entry["page_name"]: entry["times"] for entry in status["pages"]}
    assert jadwal == {"Halaman 111": ["07:00", "17:00"], "Halaman 222": ["12:30"]}


def test_autopilot_mati_tidak_dijadwalkan(client, make_page):
    make_page("111", autopilot_enabled=False)

    assert client.get("/api/scheduler/status").json()["pages"] == []


def test_halaman_tanpa_token_tidak_dijadwalkan(client, make_page, db):
    """Regresi: dulu UI menampilkan 'Aktif' padahal job pasti berhenti."""
    from database.models import FacebookPage

    page = make_page("111", autopilot_enabled=True)
    assert len(client.get("/api/scheduler/status").json()["pages"]) == 1

    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).first()
    row.access_token = ""
    db.commit()
    client.patch(f"/api/pages/{page['id']}", json={"autopilot_enabled": True})

    assert client.get("/api/scheduler/status").json()["pages"] == []


def test_halaman_dijeda_tidak_dijadwalkan(client, make_page):
    page = make_page("111", autopilot_enabled=True)
    client.patch(f"/api/pages/{page['id']}", json={"is_active": False})

    assert client.get("/api/scheduler/status").json()["pages"] == []


def test_menghapus_halaman_menghapus_jadwalnya(client, make_page):
    page = make_page("111", autopilot_enabled=True)
    client.delete(f"/api/pages/{page['id']}")

    assert client.get("/api/scheduler/status").json()["pages"] == []


def test_job_evolusi_mingguan_terpasang(client):
    status = client.get("/api/scheduler/status").json()
    assert status["next_evolution"] is not None
    assert sched.scheduler.get_job("feedback_job") is not None


def test_job_autopost_memproduksi_dan_menayangkan(client, make_page, make_post, topics,
                                                  fake_gemini, fake_facebook, with_gemini_key,
                                                  monkeypatch, db):
    """Jalankan job autopilot langsung, tanpa menunggu jadwal."""
    monkeypatch.setattr(sched, "generate_post_content", lambda *a, **k: {
        "visual_title": "Judul", "subtitle": "s",
        "imagen_prompt": "prompt", "caption": "caption otomatis",
    })
    monkeypatch.setattr(sched, "generate_poster_image", lambda **k: ("auto.jpg", "auto.jpg"))
    terkirim = {}

    def publish(page_id, access_token, image_path, caption):
        terkirim.update(page_id=page_id, token=access_token, caption=caption)
        return {"success": True, "post_id": "fb_1", "post_url": "https://fb/1", "message": "ok"}

    monkeypatch.setattr(sched, "publish_photo_to_page", publish)
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    assert terkirim["page_id"] == "111"
    assert terkirim["token"] == "token-111"
    post = db.query(Post).filter(Post.page_id == page["id"]).first()
    assert post.status == "published"
    assert post.caption == "caption otomatis"


def test_job_autopost_berhenti_bila_token_kosong(client, make_page, with_gemini_key,
                                                 monkeypatch, db):
    from database.models import FacebookPage

    page = make_page("111", autopilot_enabled=True)
    row = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).first()
    row.access_token = ""
    db.commit()

    def jangan_dipanggil(*a, **k):
        raise AssertionError("tidak boleh memanggil Gemini untuk halaman tanpa token")

    monkeypatch.setattr(sched, "generate_post_content", jangan_dipanggil)

    sched.auto_generate_and_post_job(page["id"])  # harus berhenti diam-diam

    assert db.query(Post).count() == 0


def test_job_autopost_aman_bila_halaman_sudah_dihapus(client, make_page):
    page = make_page("111", autopilot_enabled=True)
    client.delete(f"/api/pages/{page['id']}")

    sched.auto_generate_and_post_job(page["id"])  # tidak boleh melempar error
