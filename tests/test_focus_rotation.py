"""
Two-phase learning per Fanspage:
TEST  - every base topic is posted once before anything repeats;
FOCUS - posts rotate over the page's top-3 reach winners (#1 -> #2 -> #3 -> #1),
        using fresh close variants of each winner first, restarting at #1 whenever
        a new winner takes the lead; 10% of picks still explore.
"""
from collections import Counter

import pytest

import core.feedback_loop as feedback
import scheduler as scheduler_module
from core.feedback_loop import (
    EXPLORE_RATE,
    focus_winners,
    get_next_recommended_topic,
    learning_phase,
    optimize_topic_weights,
    topics_for_page,
)
from core.topic_evolution import evolve_topics
from database.models import ContentTopic, FacebookPage


@pytest.fixture
def no_explore(monkeypatch):
    monkeypatch.setattr(feedback.random, "random", lambda: 0.99)


def _variant(db, parent, page_id, title, created_order=0):
    from datetime import datetime, timedelta, timezone
    topic = ContentTopic(
        category=parent.category, topic_key=f"var_{title.lower().replace(' ', '_')}",
        title=title, core_concept="close variant " * 5, visual_blueprint="poster " * 10,
        weight=1.4, source="ai", parent_topic_id=parent.id,
        base_topic_id=parent.base_topic_id or parent.id, origin_page_id=page_id,
        created_at=datetime.now(timezone.utc) + timedelta(seconds=created_order), is_active=True,
    )
    db.add(topic)
    db.commit()
    return topic


def _three_winners(page_id, make_post, finish_test_phase, topics):
    pertama, kedua, ketiga = topics[5], topics[1], topics[7]
    finish_test_phase(page_id, reach=900, skip={pertama.id, kedua.id, ketiga.id})
    make_post(page_id, pertama, days_ago=5, reach=15000)
    make_post(page_id, kedua, days_ago=5, reach=6000)
    make_post(page_id, ketiga, days_ago=5, reach=4000)
    return pertama, kedua, ketiga


# ------------------------------------------------------------------ test phase


def test_tahap_uji_memposting_setiap_topik_dasar_tepat_sekali(client, make_page, make_post, topics, db):
    page = make_page("111")
    bases = [t for t in topics if t.source == "seed"]

    picked = []
    for _ in bases:
        topic = get_next_recommended_topic(db, page["id"])
        picked.append(topic.id)
        make_post(page["id"], topic, status="ready")   # a draft counts as tested

    assert sorted(picked) == sorted(t.id for t in bases), "tidak boleh ada yang terulang"
    assert learning_phase(db, page["id"])["phase"] == "focus"


def test_tahap_uji_berjalan_terpisah_per_fanspage(client, make_page, finish_test_phase, topics, db):
    a, b = make_page("111"), make_page("222")
    finish_test_phase(a["id"])

    assert learning_phase(db, a["id"])["phase"] == "focus"
    assert learning_phase(db, b["id"])["phase"] == "test"


def test_postingan_di_bawah_48_jam_belum_dinilai(client, make_page, make_post, finish_test_phase,
                                                  topics, db):
    """Reach masih naik sehari dua setelah tayang; postingan baru tidak boleh tampak kalah/menang."""
    page = make_page("111")
    baru = topics[3]
    finish_test_phase(page["id"], reach=1000, skip={baru.id})
    make_post(page["id"], baru, days_ago=1, reach=50000)

    optimize_topic_weights(db, 7, page["id"])

    assert focus_winners(db, page["id"])[0]["leader"].id != baru.id


# ------------------------------------------------------------------ focus rotation


def test_rotasi_mulai_ulang_dari_pemenang_baru(client, make_page, make_post, finish_test_phase,
                                               topics, db, no_explore):
    page = make_page("111")
    pertama, kedua, ketiga = _three_winners(page["id"], make_post, finish_test_phase, topics)
    optimize_topic_weights(db, 7, page["id"])
    assert get_next_recommended_topic(db, page["id"]).id == pertama.id
    assert get_next_recommended_topic(db, page["id"]).id == kedua.id

    # Konten kedua meledak dan menyalip pemenang lama.
    make_post(page["id"], kedua, days_ago=3, reach=60000)
    optimize_topic_weights(db, 7, page["id"])

    urutan = [get_next_recommended_topic(db, page["id"]).id for _ in range(3)]
    assert urutan == [kedua.id, pertama.id, ketiga.id]


def test_variasi_pemenang_dipakai_dulu_baru_topik_aslinya(client, make_page, make_post,
                                                         finish_test_phase, topics, db, no_explore):
    page = make_page("111")
    pertama, kedua, ketiga = _three_winners(page["id"], make_post, finish_test_phase, topics)
    v1 = _variant(db, pertama, page["id"], "Variant One", created_order=1)
    v2 = _variant(db, pertama, page["id"], "Variant Two", created_order=2)
    optimize_topic_weights(db, 7, page["id"])

    serve = []
    for _ in range(9):
        topic = get_next_recommended_topic(db, page["id"])
        serve.append(topic.id)
        make_post(page["id"], topic, status="ready")

    assert serve[0::3] == [v1.id, v2.id, pertama.id], "slot #1: variasi lama, variasi baru, lalu aslinya"
    assert serve[1::3] == [kedua.id] * 3
    assert serve[2::3] == [ketiga.id] * 3


def test_sekitar_sepuluh_persen_tetap_eksplorasi(client, make_page, make_post, finish_test_phase,
                                                 topics, db):
    page = make_page("111")
    top3 = {t.id for t in _three_winners(page["id"], make_post, finish_test_phase, topics)}
    optimize_topic_weights(db, 7, page["id"])

    picks = Counter(get_next_recommended_topic(db, page["id"]).id in top3 for _ in range(1000))

    assert 0.05 < picks[False] / 1000 < 0.17, picks
    assert EXPLORE_RATE == 0.10


def test_variasi_milik_fanspage_lain_tidak_dipaksakan(client, make_page, finish_test_phase, topics, db):
    a, b = make_page("111"), make_page("222")
    milik_a = _variant(db, topics[5], a["id"], "Only For A")

    assert milik_a.id in {t.id for t in topics_for_page(db, a["id"])}
    assert milik_a.id not in {t.id for t in topics_for_page(db, b["id"])}


def test_variasi_jadi_milik_bersama_saat_fanspage_dihapus(client, make_page, topics, db):
    a, b = make_page("111"), make_page("222")
    variasi = _variant(db, topics[5], a["id"], "Orphan Soon")

    client.delete(f"/api/pages/{a['id']}")
    db.expire_all()

    assert db.get(ContentTopic, variasi.id).origin_page_id is None
    assert variasi.id in {t.id for t in topics_for_page(db, b["id"])}


# ------------------------------------------------------------------ variant stocking


def _stub_generator(calls):
    def generator(**kwargs):
        target = kwargs["winners"][0]["topic_title"]
        calls.append(target)
        n = len(calls)
        return [{
            "base_topic": "nama yang salah sengaja",
            "category": "Fluvial",
            "title": f"Close variant {n} of {target[:30]}",
            "core_concept": "Same mechanism as the winner, one new field angle, explained clearly.",
            "visual_blueprint": "Vintage field guide poster with the same cross-section and a new inset.",
            "why_this_works": "same subject, new angle",
        }]
    return generator


@pytest.fixture
def stub_evolution(monkeypatch):
    calls = []
    real = evolve_topics

    def fake(**kwargs):
        return real(generator=_stub_generator(calls), **kwargs)

    monkeypatch.setattr(scheduler_module, "evolve_topics", fake)
    return calls


def test_stok_variasi_menunggu_tahap_uji_selesai(client, make_page, make_post, topics, db,
                                                with_gemini_key, stub_evolution):
    page = make_page("111")
    make_post(page["id"], topics[0], days_ago=5, reach=9000)

    assert scheduler_module.stock_focus_variants(db) == {}
    assert stub_evolution == []


def test_stok_variasi_untuk_setiap_pemenang_rotasi(client, make_page, make_post, finish_test_phase,
                                                   topics, db, with_gemini_key, stub_evolution, no_explore):
    page = make_page("111")
    pertama, kedua, ketiga = _three_winners(page["id"], make_post, finish_test_phase, topics)
    optimize_topic_weights(db, 7, page["id"])

    created = scheduler_module.stock_focus_variants(db)

    assert stub_evolution == [pertama.title, kedua.title, ketiga.title]
    assert len(created[page["id"]]) == 3
    for winner in focus_winners(db, page["id"]):
        variants = winner["fresh_variants"]
        assert len(variants) == 1
        assert variants[0].origin_page_id == page["id"]
        assert variants[0].parent_topic_id == winner["leader"].id   # tetap dalam keluarga pemenang
    # Rotasi kini menyajikan variasi tiap pemenang lebih dulu.
    first_round = [get_next_recommended_topic(db, page["id"]) for _ in range(3)]
    assert [t.source for t in first_round] == ["ai"] * 3

    # Stok masih ada: tidak membuat lagi.
    assert scheduler_module.stock_focus_variants(db) == {}


def test_analitik_melaporkan_tahap_dan_rotasi(client, make_page, make_post, finish_test_phase,
                                              topics, db, no_explore):
    page = make_page("111")
    awal = client.get(f"/api/analytics/summary?page={page['id']}").json()["learning"]
    assert (awal["phase"], awal["tested"], awal["total"]) == ("test", 0, len([t for t in topics if t.source == "seed"]))

    pertama, kedua, ketiga = _three_winners(page["id"], make_post, finish_test_phase, topics)
    optimize_topic_weights(db, 7, page["id"])
    get_next_recommended_topic(db, page["id"])   # #1 dipakai
    db.commit()

    fokus = client.get(f"/api/analytics/summary?page={page['id']}").json()["learning"]
    assert fokus["phase"] == "focus"
    assert [w["title"] for w in fokus["rotation"]] == [pertama.title, kedua.title, ketiga.title]
    assert fokus["rotation"][0]["avg_reach"] == 15000
    assert fokus["next_rank"] == 2


def test_postingan_gagal_tidak_dihitung_sudah_diuji(client, make_page, make_post, topics, db):
    """Token kedaluwarsa dll.: topiknya belum pernah sampai ke audiens, jadi dicoba lagi."""
    page = make_page("111")
    gagal = topics[3]
    make_post(page["id"], gagal, status="failed")
    make_post(page["id"], topics[4], status="ready")      # draft menunggu: tetap dihitung

    phase = learning_phase(db, page["id"])

    assert gagal.id in {t.id for t in phase["untested"]}
    assert topics[4].id not in {t.id for t in phase["untested"]}


def test_variasi_menunggu_metrik_lalu_tidak_tertahan_selamanya(client, make_page, make_post,
                                                              finish_test_phase, topics, db):
    page = make_page("111")
    finish_test_phase(page["id"], reach=1000, skip={topics[2].id})
    baru = make_post(page["id"], topics[2], days_ago=1, reach=500)     # belum 48 jam

    fase = learning_phase(db, page["id"])
    assert (fase["phase"], fase["pending"], fase["ready_for_variants"]) == ("focus", 1, False)

    # Seminggu lebih tanpa metrik (Insights tidak tersedia): tidak lagi menahan.
    from datetime import datetime, timedelta, timezone
    db.delete(baru.metrics[0])
    baru.published_at = (datetime.now(timezone.utc) - timedelta(days=9)).replace(tzinfo=None)
    db.commit()
    fase = learning_phase(db, page["id"])
    assert (fase["pending"], fase["ready_for_variants"]) == (0, True)
