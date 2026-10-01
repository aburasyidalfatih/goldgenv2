"""
Regresi dari audit 25 September: bug yang lolos dari suite sebelumnya karena
tesnya hanya memeriksa urutan atau jalur tunggal, bukan perilaku yang dirasakan.
"""
import threading
import time
from collections import Counter
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app as app_module
from app import PublishRequest, publish_post_endpoint
from config import IMAGES_DIR
from core.feedback_loop import (
    STALE_TOPIC_CAP,
    get_next_recommended_topic,
    optimize_topic_weights,
    page_topic_weights,
)
from database.db_session import SessionLocal
from database.models import Post


# ------------------------------------------------------- publish ganda


def _draft(db, page_id, topic, status="ready"):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    img = IMAGES_DIR / f"audit_{topic.id}_{status}_{page_id}.jpg"
    img.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 64)
    post = Post(page_id=page_id, topic_id=topic.id, topic_title=topic.title, language="id",
                visual_title="Draft audit", prompt_used="p", image_filename=img.name,
                image_path=str(img), caption="c", status=status,
                created_at=datetime.now(timezone.utc))
    db.add(post)
    db.commit()
    return post.id


def test_publish_bersamaan_hanya_tayang_sekali(client, make_page, topics, db, monkeypatch):
    """Regresi: 3 permintaan bersamaan dulu menghasilkan 3 postingan publik di Facebook."""
    page = make_page("111")
    post_id = _draft(db, page["id"], topics[0])
    terkirim = []
    kunci = threading.Lock()

    def upload_lambat(page_id, access_token, image_path, caption):
        time.sleep(0.5)   # unggah foto sungguhan butuh beberapa detik
        with kunci:
            terkirim.append(page_id)
            n = len(terkirim)
        return {"success": True, "post_id": f"{page_id}_{n}",
                "post_url": f"https://fb/{n}", "message": "ok"}

    monkeypatch.setattr(app_module, "publish_photo_to_page", upload_lambat)
    hasil = []

    def klik():
        sesi = SessionLocal()
        try:
            hasil.append(publish_post_endpoint(post_id, PublishRequest(), sesi))
        finally:
            sesi.close()

    utas = [threading.Thread(target=klik) for _ in range(3)]
    for u in utas:
        u.start()
    for u in utas:
        u.join()

    assert len(terkirim) == 1, f"tayang {len(terkirim)}x di Facebook"
    assert sum(1 for h in hasil if h.get("success")) == 1


def test_postingan_sedang_dipublikasikan_terkunci(client, make_page, topics, db):
    page = make_page("111")
    post_id = _draft(db, page["id"], topics[0], status="publishing")

    assert client.post(f"/api/posts/{post_id}/publish", json={}).json()["success"] is False
    assert client.delete(f"/api/posts/{post_id}").json()["success"] is False
    assert client.post(f"/api/posts/{post_id}/regenerate-image").status_code == 409
    assert client.patch(f"/api/posts/{post_id}",
                        json={"visual_title": "x", "caption": "y"}).status_code == 409


def test_publish_gagal_koneksi_tidak_meninggalkan_status_macet(client, make_page, topics,
                                                              db, monkeypatch):
    page = make_page("111")
    post_id = _draft(db, page["id"], topics[0])

    def putus(*args, **kwargs):
        raise ConnectionError("jaringan putus")

    monkeypatch.setattr(app_module, "publish_photo_to_page", putus)

    res = client.post(f"/api/posts/{post_id}/publish", json={}).json()

    assert res["success"] is False
    row = db.get(Post, post_id)
    db.refresh(row)
    assert row.status == "failed"


def test_status_macet_ditandai_gagal_saat_aplikasi_menyala(client, make_page, topics, db):
    """Aplikasi mati di tengah unggahan: jangan diam di 'publishing' selamanya."""
    page = make_page("111")
    post_id = _draft(db, page["id"], topics[0], status="publishing")

    with TestClient(app_module.app):   # startup ulang = aplikasi dinyalakan kembali
        pass

    row = db.get(Post, post_id)
    db.refresh(row)
    assert row.status == "failed"
    assert "Periksa Fanspage" in row.error_message


# ------------------------------------------------------- pemenang tunggal


def _porsi(db, page_id, topic, n=3000):
    pilihan = Counter(get_next_recommended_topic(db, page_id).id for _ in range(n))
    lain = [v for k, v in pilihan.items() if k != topic.id]
    return pilihan[topic.id] / n, (sum(lain) / len(lain) / n) if lain else 0.0


def test_pemenang_tunggal_diproduksi_lebih_sering(client, make_page, make_post, topics, db):
    """
    Regresi: bila hanya satu topik tayang minggu ini, pemenang dengan reach 8x
    rata-rata halaman dulu diproduksi LEBIH JARANG dari topik lain.
    """
    page = make_page("111")
    for i in range(15):
        make_post(page["id"], topics[0], days_ago=20 + i, reach=2500)
    pemenang = topics[5]
    make_post(page["id"], pemenang, days_ago=2, reach=20000)
    make_post(page["id"], pemenang, days_ago=4, reach=22000)

    optimize_topic_weights(db, 7, page["id"])
    porsi_pemenang, porsi_lain = _porsi(db, page["id"], pemenang)

    assert page_topic_weights(db, page["id"], topics)[pemenang.id] >= 2.0
    assert porsi_pemenang > 2 * porsi_lain, f"{porsi_pemenang:.1%} vs {porsi_lain:.1%}"


def test_pemenang_tunggal_di_halaman_baru_tetap_diunggulkan(client, make_page, make_post,
                                                           topics, db):
    """Tanpa riwayat pembanding sama sekali (minggu pertama sebuah halaman)."""
    page = make_page("111")
    pemenang = topics[2]
    make_post(page["id"], pemenang, days_ago=1, reach=5000)

    optimize_topic_weights(db, 7, page["id"])
    w = page_topic_weights(db, page["id"], topics)

    assert w[pemenang.id] > STALE_TOPIC_CAP + 0.3
    porsi_pemenang, porsi_lain = _porsi(db, page["id"], pemenang)
    assert porsi_pemenang > porsi_lain


def test_pemenang_tunggal_yang_jeblok_tidak_didongkrak(client, make_page, make_post, topics, db):
    """Satu-satunya topik minggu ini, tapi hasilnya jauh di bawah biasanya."""
    page = make_page("111")
    for i in range(10):
        make_post(page["id"], topics[1], days_ago=15 + i, reach=8000)
    jeblok = topics[4]
    make_post(page["id"], jeblok, days_ago=2, reach=300)

    optimize_topic_weights(db, 7, page["id"])
    w = page_topic_weights(db, page["id"], topics)

    assert w[jeblok.id] == 1.0, "tetap di atas topik dorman, tapi tanpa dongkrak"


# ------------------------------------------------------- kartu pemenang


def test_kartu_pemenang_sama_dengan_yang_diprioritaskan(client, make_page, make_post, topics, db):
    """Regresi: kartu dulu memakai total skor seumur hidup dan bisa bertentangan."""
    page = make_page("111")
    for i in range(15):
        make_post(page["id"], topics[0], days_ago=20 + i, reach=2500)
    make_post(page["id"], topics[5], days_ago=2, reach=20000)
    make_post(page["id"], topics[5], days_ago=4, reach=22000)
    optimize_topic_weights(db, 7, page["id"])

    kartu = client.get(f"/api/analytics/summary?page={page['id']}").json()["winning_topic"]
    paling_sering = Counter(
        get_next_recommended_topic(db, page["id"]).title for _ in range(2000)
    ).most_common(1)[0][0]

    assert kartu == topics[5].title
    assert kartu == paling_sering
