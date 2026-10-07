"""Base curriculum: every base topic exists on every database, old or new."""
from core.taxonomy import SEED_TOPICS, seed_base_curriculum
from database.models import ContentTopic

FAQ_KEYS = {"after_the_flood_new_gold", "how_to_pan_for_gold", "blm_casual_use", "what_your_gold_is_worth"}
BEGINNER_KEYS = {"first_trip_minimal_kit", "picking_your_first_spot", "practice_panning_at_home",
                 "no_color_beginner_mistakes", "your_first_flake", "beginner_friendly_places",
                 "prospecting_logbook"}


def test_semua_topik_dasar_terpasang(client, db):
    keys = {k for (k,) in db.query(ContentTopic.topic_key).filter(ContentTopic.source == "seed").all()}
    assert keys == {t["topic_key"] for t in SEED_TOPICS}
    assert FAQ_KEYS <= keys
    assert BEGINNER_KEYS <= keys


def test_database_lama_mendapat_topik_baru_tanpa_mengubah_yang_lama(client, db):
    """Local and production databases were seeded with only the first 10 topics."""
    lama = db.query(ContentTopic).filter(ContentTopic.topic_key == "inside_bend_rule").first()
    lama.weight, lama.is_active = 2.5, False          # learned weight, retired by the user
    db.query(ContentTopic).filter(ContentTopic.topic_key.in_(FAQ_KEYS)).delete(synchronize_session=False)
    db.commit()

    added = seed_base_curriculum(db)

    assert len(added) == len(FAQ_KEYS)
    baru = db.query(ContentTopic).filter(ContentTopic.topic_key == "blm_casual_use").first()
    assert baru.source == "seed" and baru.is_active and baru.base_topic_id == baru.id
    db.refresh(lama)
    assert (lama.weight, lama.is_active) == (2.5, False)   # untouched
    assert seed_base_curriculum(db) == []                  # idempotent
    lama.weight, lama.is_active = 1.2, True
    db.commit()


def test_topik_baru_ikut_diuji_lewat_mode_explore(client, make_page, db, monkeypatch):
    """A fresh page with no history must be able to draw any base topic, FAQ ones included."""
    import core.feedback_loop as feedback
    page = make_page("111")
    monkeypatch.setattr(feedback.random, "random", lambda: 0.0)   # force EXPLORE
    drawn = {feedback.get_next_recommended_topic(db, page["id"]).topic_key for _ in range(400)}
    assert FAQ_KEYS & drawn


def test_topik_pemula_masuk_ke_database_yang_sudah_berjalan(client, db):
    """Database produksi sudah punya 33 topik: 7 topik pemula harus ditambahkan saat start."""
    db.query(ContentTopic).filter(ContentTopic.topic_key.in_(BEGINNER_KEYS)).delete(synchronize_session=False)
    db.commit()

    added = seed_base_curriculum(db)

    expected = {t["title"] for t in SEED_TOPICS if t["topic_key"] in BEGINNER_KEYS}
    assert set(added) == expected
    rows = db.query(ContentTopic).filter(ContentTopic.topic_key.in_(BEGINNER_KEYS)).all()
    assert len(rows) == 7 and all(r.source == "seed" and r.is_active for r in rows)


def test_topik_pemula_ikut_tahap_uji(client, make_page, make_post, topics, db):
    """Fanspage yang sudah selesai menguji 33 topik lama kembali menguji topik pemula yang baru."""
    from core.feedback_loop import learning_phase
    page = make_page("111")
    for t in db.query(ContentTopic).filter(ContentTopic.source == "seed",
                                           ContentTopic.topic_key.notin_(BEGINNER_KEYS)):
        make_post(page["id"], t, days_ago=10)

    phase = learning_phase(db, page["id"])

    assert phase["phase"] == "test"
    assert {t.topic_key for t in phase["untested"]} == BEGINNER_KEYS
