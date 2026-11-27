"""
Uji silang: subjek yang terbukti menang di satu Fanspage dicoba TRIAL_POSTS kali di
Fanspage lain (1 dari tiap TRIAL_EVERY slot), lalu reach halaman itu sendiri yang
memutuskan. Pemenang dinilai hanya dari postingan gaya baru (sejak STYLE_EPOCH).
"""
from datetime import datetime, timedelta, timezone

import pytest

import core.feedback_loop as feedback
import scheduler as sched
from core.feedback_loop import (
    TRIAL_EVERY,
    TRIAL_POSTS,
    cross_page_trials,
    focus_winners,
    pick_next_topic,
)
from database.models import ContentTopic, Post


@pytest.fixture
def no_explore(monkeypatch):
    monkeypatch.setattr(feedback.random, "random", lambda: 0.99)


@pytest.fixture
def gaya_baru(monkeypatch):
    """Gaya baru dimulai 10 hari lalu: postingan tahap uji (30 hari lalu) bergaya lama."""
    monkeypatch.setattr(feedback, "STYLE_EPOCH",
                        (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None))


def _variant(db, parent, page_id, title):
    topic = ContentTopic(
        category=parent.category, topic_key=f"var_{title.lower().replace(' ', '_')}",
        title=title, core_concept="close variant " * 5, visual_blueprint="poster " * 10,
        weight=1.4, source="ai", parent_topic_id=parent.id, base_topic_id=parent.id,
        origin_page_id=page_id, is_active=True,
    )
    db.add(topic)
    db.commit()
    return topic


@pytest.fixture
def dua_halaman(client, make_page, make_post, finish_test_phase, topics, gaya_baru):
    """
    Halaman A: subjek X menang besar di gaya baru (3 postingan 15.000, median A 900).
    Halaman B: belum pernah memosting X sejak gaya baru; rotasinya topik lain.
    """
    a, b = make_page("111"), make_page("222")
    finish_test_phase(a["id"], days_ago=30)
    finish_test_phase(b["id"], days_ago=30)
    x = topics[5]
    for hari in (3, 4, 5):
        make_post(a["id"], x, days_ago=hari, reach=15000)
    for t in topics[10:16]:
        make_post(a["id"], t, days_ago=4, reach=900)
    for t in topics[20:24]:
        make_post(b["id"], t, days_ago=4, reach=1000)
    return a, b, x


def _catat(db, page_id, topic, alasan):
    """Postingan yang baru dibuat autopilot (belum matang) beserta alasan pilihannya."""
    post = Post(page_id=page_id, topic_id=topic.id, topic_title=topic.title, visual_title=topic.title,
                prompt_used="p", image_filename="x.jpg", image_path="x.jpg", caption="c",
                status="ready", pick_reason=alasan, created_at=datetime.now(timezone.utc))
    db.add(post)
    db.commit()


def test_pemenang_halaman_lain_diuji_di_halaman_ini(dua_halaman, db, no_explore):
    a, b, x = dua_halaman

    calon = cross_page_trials(db, b["id"])
    topic, alasan = pick_next_topic(db, b["id"])

    assert [c["key"] for c in calon] == [x.id]
    assert calon[0]["source_page_id"] == a["id"] and calon[0]["ratio"] >= 2
    assert (topic.id, alasan) == (x.id, "trial")      # uji pertama: topik dasarnya sendiri


def test_satu_dari_empat_slot_dan_berhenti_setelah_dua_kali(dua_halaman, db, no_explore):
    _, b, _ = dua_halaman
    urutan = []
    for _ in range(TRIAL_EVERY * TRIAL_POSTS + 2):
        topic, alasan = pick_next_topic(db, b["id"])
        urutan.append(alasan)
        _catat(db, b["id"], topic, alasan)

    assert urutan == ["trial", "focus", "focus", "focus", "trial", "focus", "focus", "focus", "focus", "focus"]


def test_uji_kedua_memakai_varian_terbaik_dari_halaman_asal(dua_halaman, db, make_post, no_explore):
    a, b, x = dua_halaman
    varian = _variant(db, x, a["id"], "Frequency Is Not Depth")
    make_post(a["id"], varian, days_ago=3, reach=40000)
    _catat(db, b["id"], x, "trial")                    # uji pertama sudah tayang
    for _ in range(TRIAL_EVERY - 1):
        _catat(db, b["id"], db.get(ContentTopic, x.id + 15), "focus")

    topic, alasan = pick_next_topic(db, b["id"])

    assert (topic.id, alasan) == (varian.id, "trial")  # sudut lain yang terbukti di halaman asal


@pytest.mark.parametrize("jumlah, reach", [
    (2, 15000),     # baru 2 postingan matang di halaman asal: belum cukup bukti
    (3, 1500),      # hanya ~1,3x median halaman asal: bukan pemenang
])
def test_bukti_lemah_tidak_diuji(client, make_page, make_post, finish_test_phase, topics, gaya_baru,
                                db, no_explore, jumlah, reach):
    a, b = make_page("111"), make_page("222")
    finish_test_phase(a["id"], days_ago=30)
    finish_test_phase(b["id"], days_ago=30)
    for hari in range(3, 3 + jumlah):
        make_post(a["id"], topics[5], days_ago=hari, reach=reach)
    for t in topics[10:16]:
        make_post(a["id"], t, days_ago=4, reach=900)
    for t in topics[20:24]:
        make_post(b["id"], t, days_ago=4, reach=1000)

    assert cross_page_trials(db, b["id"]) == []
    assert pick_next_topic(db, b["id"])[1] == "focus"


def test_subjek_yang_sudah_di_rotasi_sendiri_tidak_diuji(dua_halaman, db, make_post):
    _, b, x = dua_halaman
    make_post(b["id"], x, days_ago=3, reach=5000)      # B sudah menemukannya sendiri

    assert x.id in {w["key"] for w in focus_winners(db, b["id"])}
    assert cross_page_trials(db, b["id"]) == []


def test_halaman_tanpa_tetangga_tidak_pernah_menguji(client, make_page, make_post, finish_test_phase,
                                                     topics, gaya_baru, db, no_explore):
    page = make_page("111")
    finish_test_phase(page["id"], days_ago=30)
    make_post(page["id"], topics[3], days_ago=4, reach=2000)

    assert pick_next_topic(db, page["id"])[1] == "focus"


def test_pemenang_dinilai_hanya_dari_gaya_baru(client, make_page, make_post, finish_test_phase, topics,
                                              gaya_baru, db):
    page = make_page("111")
    finish_test_phase(page["id"], days_ago=30, reach=900)
    lama, baru = topics[2], topics[8]
    make_post(page["id"], lama, days_ago=25, reach=60000)   # viral, tetapi gaya lama
    make_post(page["id"], baru, days_ago=4, reach=3000)

    pemenang = focus_winners(db, page["id"])

    assert pemenang[0]["key"] == baru.id
    assert lama.id not in {w["key"] for w in pemenang if w["posts"]}
    assert pemenang[0]["avg_reach"] == 3000 and pemenang[0]["posts"] == 1


def test_tanpa_batas_gaya_postingan_lama_ikut_dinilai(client, make_page, make_post, finish_test_phase,
                                                     topics, db):
    """Pembanding: dengan batas jauh di masa lalu (bawaan tes), postingan viral lama memimpin."""
    page = make_page("111")
    finish_test_phase(page["id"], days_ago=30, reach=900)
    make_post(page["id"], topics[2], days_ago=25, reach=60000)
    make_post(page["id"], topics[8], days_ago=4, reach=3000)

    assert focus_winners(db, page["id"])[0]["key"] == topics[2].id


def test_alasan_pilihan_tersimpan_di_postingan_autopilot(client, make_page, topics, with_gemini_key,
                                                        monkeypatch, db):
    monkeypatch.setattr(sched, "generate_post_content", lambda *a, **k: {
        "visual_title": "Judul", "subtitle": "s", "imagen_prompt": "prompt", "caption": "caption"})
    monkeypatch.setattr(sched, "generate_poster_image", lambda **k: ("auto.jpg", "auto.jpg"))
    monkeypatch.setattr(sched, "publish_photo_to_page",
                        lambda **k: {"success": True, "post_id": "fb_1", "post_url": "https://fb/1"})
    page = make_page("111", autopilot_enabled=True)

    sched.auto_generate_and_post_job(page["id"])

    post = db.query(Post).one()
    assert post.pick_reason == "test"                  # halaman baru: tahap uji
    assert client.get(f"/api/posts/{post.id}").json()["pick_reason"] == "test"


def test_dasbor_menampilkan_antrean_uji_silang(dua_halaman, client):
    a, b, x = dua_halaman

    learning = client.get(f"/api/analytics/summary?page={b['id']}").json()["learning"]

    assert learning["phase"] == "focus"
    assert learning["trials"] == [{"title": x.title, "source": a["name"], "ratio": learning["trials"][0]["ratio"],
                                   "done": 0, "of": TRIAL_POSTS}]
    assert learning["trials"][0]["ratio"] >= 2
