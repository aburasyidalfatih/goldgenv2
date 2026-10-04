"""
The adaptive learning loop: how each Fanspage discovers what its own audience
wants. These tests pin down the behaviour the whole product is built on.
"""
from collections import Counter

from core.feedback_loop import (
    BASE_TOPIC_WEIGHT_FLOOR,
    get_next_recommended_topic,
    optimize_topic_weights,
    page_topic_weights,
    window_performance,
)


def test_halaman_baru_hanya_memakai_kurikulum_dasar(client, make_page, topics, db):
    """Konten awal: semua topik dasar, bobot netral, tanpa favorit."""
    page = make_page("111")

    weights = page_topic_weights(db, page["id"], topics)

    assert all(t.source == "seed" for t in topics)
    assert set(weights.values()) <= {1.0, 1.1, 1.2}  # baseline kurikulum


def test_halaman_baru_menyebar_merata_ke_semua_topik(client, make_page, topics, db):
    page = make_page("111")

    picks = Counter(get_next_recommended_topic(db, page["id"]).id for _ in range(600))

    assert len(picks) == len(topics), "semua topik dasar harus kebagian"
    # tidak boleh ada satu topik yang mendominasi sebelum ada data apa pun
    assert max(picks.values()) / 600 < 0.25


def test_bobot_naik_untuk_topik_yang_jangkauannya_tinggi(client, make_page, make_post, topics, db):
    page = make_page("111")
    favorit, biasa = topics[5], topics[0]
    make_post(page["id"], favorit, days_ago=3, reach=15000)
    make_post(page["id"], favorit, days_ago=5, reach=18000)
    make_post(page["id"], biasa, days_ago=4, reach=800)

    optimize_topic_weights(db, 7, page["id"])
    weights = page_topic_weights(db, page["id"], topics)

    assert weights[favorit.id] > weights[biasa.id]
    assert weights[favorit.id] > 1.5


def test_tahap_fokus_merotasi_tiga_pemenang_reach_teratas(client, make_page, make_post,
                                                       finish_test_phase, topics, db, monkeypatch):
    """Setelah konten pemenang #1 tayang, berikutnya #2, lalu #3, lalu kembali ke #1."""
    import core.feedback_loop as feedback
    page = make_page("111")
    pertama, kedua, ketiga = topics[5], topics[1], topics[7]
    finish_test_phase(page["id"], reach=900, skip={pertama.id, kedua.id, ketiga.id})
    make_post(page["id"], pertama, days_ago=5, reach=15000)
    make_post(page["id"], kedua, days_ago=5, reach=6000)
    make_post(page["id"], ketiga, days_ago=5, reach=4000)
    optimize_topic_weights(db, 7, page["id"])
    monkeypatch.setattr(feedback.random, "random", lambda: 0.99)   # tanpa eksplorasi

    urutan = [get_next_recommended_topic(db, page["id"]).id for _ in range(6)]

    assert urutan == [pertama.id, kedua.id, ketiga.id] * 2


def test_dua_halaman_belajar_terpisah(client, make_page, make_post, topics, db):
    """Inti multi-Fanspage: selera audiens tidak saling mencemari."""
    a = make_page("111")
    b = make_page("222")
    suka_a, suka_b = topics[3], topics[8]

    make_post(a["id"], suka_a, days_ago=3, reach=18000)
    make_post(a["id"], suka_b, days_ago=3, reach=700)
    make_post(b["id"], suka_b, days_ago=3, reach=22000)
    make_post(b["id"], suka_a, days_ago=3, reach=600)

    optimize_topic_weights(db, 7, a["id"])
    optimize_topic_weights(db, 7, b["id"])
    wa = page_topic_weights(db, a["id"], topics)
    wb = page_topic_weights(db, b["id"], topics)

    assert wa[suka_a.id] > wa[suka_b.id]
    assert wb[suka_b.id] > wb[suka_a.id]


def test_halaman_baru_tidak_mewarisi_selera_halaman_lain(client, make_page, make_post, topics, db):
    """Regresi: dulu halaman kedua mulai dengan bobot 3.0 milik halaman pertama."""
    a = make_page("111")
    favorit = topics[5]
    make_post(a["id"], favorit, days_ago=3, reach=30000)
    make_post(a["id"], favorit, days_ago=4, reach=28000)
    from core.feedback_loop import optimize_all_pages

    optimize_all_pages(db, 7)

    b = make_page("222")
    wb = page_topic_weights(db, b["id"], topics)

    assert set(wb.values()) <= {1.0, 1.1, 1.2}, "halaman baru harus mulai dari nol"


def test_juara_yang_mulai_ditinggalkan_bobotnya_turun(client, make_page, make_post,
                                                     finish_test_phase, topics, db):
    """
    Topik fokus terus diposting ulang, jadi angka terbarunya ikut menilai: juara lama
    yang kini sepi tidak boleh terus mengalahkan topik yang stabil.
    """
    page = make_page("111")
    juara_lama, stabil = topics[0], topics[3]
    finish_test_phase(page["id"], reach=2000, skip={juara_lama.id, stabil.id})
    make_post(page["id"], juara_lama, days_ago=25, reach=12000)
    make_post(page["id"], juara_lama, days_ago=3, reach=500)
    make_post(page["id"], juara_lama, days_ago=4, reach=600)
    make_post(page["id"], stabil, days_ago=20, reach=6000)
    make_post(page["id"], stabil, days_ago=3, reach=6500)

    optimize_topic_weights(db, 7, page["id"])
    weights = page_topic_weights(db, page["id"], topics)

    assert weights[stabil.id] > weights[juara_lama.id]


def test_topik_dasar_punya_lantai_bobot(client, make_page, make_post, topics, db):
    """Fundamental tetap diajarkan walau sedang tidak diminati."""
    page = make_page("111")
    lemah, kuat = topics[1], topics[3]
    make_post(page["id"], lemah, days_ago=3, reach=50)
    make_post(page["id"], kuat, days_ago=3, reach=40000)

    optimize_topic_weights(db, 7, page["id"])
    weights = page_topic_weights(db, page["id"], topics)

    assert weights[lemah.id] >= BASE_TOPIC_WEIGHT_FLOOR


def test_bobot_relatif_terhadap_rata_rata_bukan_angka_mutlak(client, make_page, make_post, topics, db):
    """Halaman kecil dan halaman besar harus menghasilkan rentang bobot serupa."""
    kecil = make_page("111")
    besar = make_page("222")
    for page, skala in ((kecil, 1), (besar, 100)):
        make_post(page["id"], topics[3], days_ago=3, reach=120 * skala)
        make_post(page["id"], topics[0], days_ago=3, reach=40 * skala)
        optimize_topic_weights(db, 7, page["id"])

    w_kecil = page_topic_weights(db, kecil["id"], topics)
    w_besar = page_topic_weights(db, besar["id"], topics)

    assert abs(w_kecil[topics[3].id] - w_besar[topics[3].id]) < 0.35
    assert all(0.4 <= w <= 3.0 for w in list(w_kecil.values()) + list(w_besar.values()))


def test_jendela_waktu_hanya_menghitung_postingan_dalam_periode(client, make_page, make_post, topics, db):
    page = make_page("111")
    make_post(page["id"], topics[0], days_ago=3, reach=5000)
    make_post(page["id"], topics[1], days_ago=20, reach=90000)

    tujuh = window_performance(db, 7, page["id"])
    tiga_puluh = window_performance(db, 30, page["id"])

    assert tujuh["posts_in_window"] == 1
    assert tujuh["total_reach"] == 5000
    assert tiga_puluh["posts_in_window"] == 2


def test_metrik_halaman_lain_tidak_bocor_ke_jendela(client, make_page, make_post, topics, db):
    a = make_page("111")
    b = make_page("222")
    make_post(a["id"], topics[0], days_ago=2, reach=7000)
    make_post(b["id"], topics[0], days_ago=2, reach=3000)

    assert window_performance(db, 7, a["id"])["total_reach"] == 7000
    assert window_performance(db, 7, b["id"])["total_reach"] == 3000
    assert window_performance(db, 7)["total_reach"] == 10000  # gabungan semua halaman


def test_topik_nonaktif_tidak_pernah_dipilih(client, make_page, topics, db):
    page = make_page("111")
    dimatikan = topics[2]
    dimatikan.is_active = False
    db.commit()

    picks = {get_next_recommended_topic(db, page["id"]).id for _ in range(300)}

    assert dimatikan.id not in picks


def test_pemilihan_topik_aman_dipanggil_berulang_tanpa_commit(client, make_page, topics, db):
    """
    Regresi: memilih topik beberapa kali dalam satu sesi (setelah mark_topic_used)
    dulu melempar TypeError karena membandingkan datetime aware dengan naive.
    """
    from core.feedback_loop import mark_topic_used

    page = make_page("111")

    for _ in range(5):
        topik = get_next_recommended_topic(db, page["id"])
        mark_topic_used(db, topik, page["id"])   # menulis waktu ber-timezone
    # tidak ada commit di antaranya: nilai di memori tetap aware
    hasil = get_next_recommended_topic(db, page["id"])

    assert hasil is not None


def test_pembelajaran_konvergen_ke_topik_yang_disukai(client, make_page, make_post, topics, db):
    """Beberapa siklus berturut-turut harus mengendap, bukan berayun."""
    page = make_page("111")
    favorit = topics[4]

    juara = []
    for siklus in range(4):
        for i in range(4):
            topik = favorit if i < 2 else topics[(siklus + i) % len(topics)]
            reach = 20000 if topik is favorit else 1500
            make_post(page["id"], topik, days_ago=1 + siklus, reach=reach)
        optimize_topic_weights(db, 7, page["id"])
        w = page_topic_weights(db, page["id"], topics)
        juara.append(max(topics, key=lambda t: w[t.id]).id)

    assert juara[-3:] == [favorit.id] * 3, f"juara berayun: {juara}"
