"""Base curriculum: every base topic exists on every database, old or new."""
from core.taxonomy import SEED_TOPICS, seed_base_curriculum
from database.models import ContentTopic

FAQ_KEYS = {"after_the_flood_new_gold", "how_to_pan_for_gold", "blm_casual_use", "what_your_gold_is_worth"}


def test_semua_topik_dasar_terpasang(client, db):
    keys = {k for (k,) in db.query(ContentTopic.topic_key).filter(ContentTopic.source == "seed").all()}
    assert keys == {t["topic_key"] for t in SEED_TOPICS}
    assert FAQ_KEYS <= keys


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
