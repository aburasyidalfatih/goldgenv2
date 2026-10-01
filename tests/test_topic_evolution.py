"""
Topic evolution: growing new topics from each page's winners, always rooted in
the base curriculum.
"""
from core.topic_evolution import (
    NEW_TOPIC_WEIGHT,
    build_evolution_prompt,
    evolve_topics,
    resolve_base_topic,
    retire_topic,
    validate_variant,
)
from core.taxonomy import SEED_TOPICS
from database.models import ContentTopic


def _variant(title="Rasio Pasir Hitam: Menakar Kepadatan Jebakan Emas", base=None, category="Minerals"):
    return {
        "base_topic": base or "",
        "category": category,
        "title": title,
        "core_concept": (
            "Perbandingan volume pasir hitam terhadap kerikil dalam satu dulang menunjukkan "
            "seberapa kuat energi hidrolik menyortir material di titik tersebut."
        ),
        "visual_blueprint": (
            "Poster panduan lapangan dengan tiga dulang berjajar, panel makro magnetit dan emas, "
            "latar kertas tua bergaris kontur topografi."
        ),
        "why_this_works": "Memperdalam materi dasar pasir hitam.",
    }


def _stub_generator(proposals, capture=None):
    def generator(api_key, winners, existing_titles, max_new, model_name, language,
                  base_curriculum, window_days, provider="gemini"):
        if capture is not None:
            capture.update(
                winners=winners,
                existing=existing_titles,
                curriculum=base_curriculum,
                language=language,
                prompt=build_evolution_prompt(
                    winners, existing_titles, max_new, language, base_curriculum, window_days
                ),
            )
        return proposals

    return generator


def test_tanpa_data_performa_tidak_membuat_topik(client, make_page, db):
    page = make_page("111")

    res = evolve_topics(db, api_key="k", page_id=page["id"], generator=_stub_generator([]))

    assert res["success"] is False
    assert "Belum ada data performa" in res["message"]


def test_seluruh_kurikulum_dasar_dikirim_sebagai_materi_belajar(client, make_page, make_post, topics, db):
    page = make_page("111")
    make_post(page["id"], topics[3], days_ago=3, reach=15000)
    capture = {}

    evolve_topics(db, api_key="k", page_id=page["id"],
                  generator=_stub_generator([_variant()], capture))

    assert len(capture["curriculum"]) == len(SEED_TOPICS)
    assert "KURIKULUM DASAR" in capture["prompt"]
    assert topics[0].core_concept[:60] in capture["prompt"]


def test_pemenang_halaman_itu_yang_dikirim_bukan_halaman_lain(client, make_page, make_post, topics, db):
    a = make_page("111")
    b = make_page("222")
    make_post(a["id"], topics[3], days_ago=3, reach=20000)
    make_post(b["id"], topics[8], days_ago=3, reach=25000)
    capture = {}

    evolve_topics(db, api_key="k", page_id=b["id"],
                  generator=_stub_generator([_variant()], capture))

    assert capture["winners"][0]["topic_id"] == topics[8].id


def test_topik_baru_berakar_ke_materi_dasar(client, make_page, make_post, topics, db):
    page = make_page("111")
    induk = topics[3]
    make_post(page["id"], induk, days_ago=3, reach=15000)

    res = evolve_topics(db, api_key="k", page_id=page["id"],
                        generator=_stub_generator([_variant(base=induk.title)]))

    assert res["success"]
    baru = db.query(ContentTopic).filter(ContentTopic.source == "ai").first()
    assert baru.base_topic_id == induk.id
    assert baru.weight == NEW_TOPIC_WEIGHT
    assert induk.title in baru.origin_note


def test_materi_dasar_ngawur_dicocokkan_lewat_isi(client, make_page, make_post, topics, db):
    """AI menyebut judul yang tidak ada -> sistem mencocokkan sendiri."""
    page = make_page("111")
    make_post(page["id"], topics[3], days_ago=3, reach=15000)
    tentang_belokan = _variant(
        title="Aliran Balik di Belokan Ganda: Perangkap Emas Tersembunyi",
        base="Judul yang tidak ada di kurikulum",
        category="Fluvial",
    )
    tentang_belokan["core_concept"] = (
        "Pada sungai dengan dua belokan berurutan, arus membentuk pusaran balik di sisi dalam "
        "belokan kedua. Inside bend deposition river curve water slows and drops heavy material."
    )

    evolve_topics(db, api_key="k", page_id=page["id"],
                  generator=_stub_generator([tentang_belokan]))

    baru = db.query(ContentTopic).filter(ContentTopic.source == "ai").first()
    assert baru.base_topic_id == topics[0].id  # The Inside Bend Rule


def test_judul_duplikat_ditolak(client, make_page, make_post, topics, db):
    page = make_page("111")
    make_post(page["id"], topics[3], days_ago=3, reach=15000)

    res = evolve_topics(db, api_key="k", page_id=page["id"],
                        generator=_stub_generator([_variant(title=topics[3].title)]))

    assert res["success"] is False
    assert any("sudah ada" in s["reason"] for s in res["skipped"])


def test_usulan_tidak_lengkap_ditolak(client, make_page, make_post, topics, db):
    page = make_page("111")
    make_post(page["id"], topics[3], days_ago=3, reach=15000)
    rusak = {"title": "Pendek", "core_concept": "x", "visual_blueprint": "", "category": "Fluvial"}

    res = evolve_topics(db, api_key="k", page_id=page["id"], generator=_stub_generator([rusak]))

    assert res["success"] is False
    assert db.query(ContentTopic).filter(ContentTopic.source == "ai").count() == 0


def test_validasi_menolak_isi_terlalu_pendek():
    assert validate_variant({"title": "x", "core_concept": "y", "visual_blueprint": "z"}) is None
    assert validate_variant(_variant()) is not None


def test_kategori_asing_diganti_default_bukan_dibuang():
    bersih = validate_variant(_variant(category="Astrologi"))
    assert bersih["category"] == "Geology"


def test_topik_dasar_tidak_bisa_dinonaktifkan(client, topics, db):
    res = retire_topic(db, topics[0].id)

    assert res["success"] is False
    assert "topik dasar" in res["message"]
    db.refresh(topics[0])
    assert topics[0].is_active is True


def test_topik_turunan_bisa_dinonaktifkan_dan_diaktifkan(client, make_page, make_post, topics, db):
    page = make_page("111")
    make_post(page["id"], topics[3], days_ago=3, reach=15000)
    evolve_topics(db, api_key="k", page_id=page["id"], generator=_stub_generator([_variant()]))
    turunan = db.query(ContentTopic).filter(ContentTopic.source == "ai").first()

    assert retire_topic(db, turunan.id)["success"] is True
    db.refresh(turunan)
    assert turunan.is_active is False

    assert client.post(f"/api/topics/{turunan.id}/reactivate").json()["success"] is True


def test_topik_baru_masuk_katalog_bersama(client, make_page, make_post, topics, db):
    """Penemuan di satu halaman tersedia untuk halaman lain."""
    a = make_page("111")
    b = make_page("222")
    make_post(a["id"], topics[3], days_ago=3, reach=15000)
    evolve_topics(db, api_key="k", page_id=a["id"], generator=_stub_generator([_variant()]))

    terlihat_b = client.get(f"/api/topics?page={b['id']}").json()

    assert any(t["source"] == "ai" for t in terlihat_b)


def test_pohon_kurikulum_menampilkan_induk_dan_turunan(client, make_page, make_post, topics, db):
    page = make_page("111")
    induk = topics[3]
    make_post(page["id"], induk, days_ago=3, reach=15000)
    evolve_topics(db, api_key="k", page_id=page["id"],
                  generator=_stub_generator([_variant(base=induk.title)]))

    pohon = client.get("/api/topics/curriculum").json()

    assert pohon["base_count"] == len(SEED_TOPICS)
    assert pohon["variant_count"] == 1
    assert pohon["unlinked_variants"] == []
    cabang = [b for b in pohon["curriculum"] if b["id"] == induk.id][0]
    assert len(cabang["variants"]) == 1
