"""
Tahap 2 — mengukur perilaku aplikasi setelah dipakai lama.
Berjalan di database sementara; data asli tidak tersentuh.
"""
import os, sys, tempfile, time, shutil
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="autoposter_bench_"))
os.environ["AUTOPOSTER_DATA_DIR"] = str(ROOT / "data")
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(ROOT / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import app as app_module
from scripts._masuk_uji import masuk
from database.db_session import SessionLocal, Base, engine
from database.models import ContentTopic, FacebookPage, Post, PostMetric
from core.feedback_loop import optimize_all_pages, window_performance, get_next_recommended_topic

JUMLAH_HALAMAN = 3
POSTINGAN_PER_HALAMAN = 350          # ~ setahun autopilot 2x/hari di 2 halaman
random_state = 12345


def ukur(label, fn, *a, **kw):
    t0 = time.perf_counter()
    hasil = fn(*a, **kw)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  {label:52} {ms:8.1f} ms")
    return hasil, ms


def main():
    import random
    random.seed(random_state)

    with TestClient(app_module.app):
        db = SessionLocal()
        topics = db.query(ContentTopic).all()
        now = datetime.now(timezone.utc)

        print(f"Menyiapkan {JUMLAH_HALAMAN} halaman x {POSTINGAN_PER_HALAMAN} postingan...")
        t0 = time.perf_counter()
        halaman = []
        for i in range(JUMLAH_HALAMAN):
            p = FacebookPage(page_id=f"bench{i}", name=f"Halaman Bench {i}",
                             access_token="tok", created_at=now)
            db.add(p); db.flush(); halaman.append(p)

        for p in halaman:
            favorit = random.choice(topics)
            for n in range(POSTINGAN_PER_HALAMAN):
                topik = favorit if random.random() < 0.4 else random.choice(topics)
                umur = n * 365 / POSTINGAN_PER_HALAMAN
                pub = (now - timedelta(days=umur)).replace(tzinfo=None)
                reach = random.randint(8000, 25000) if topik is favorit else random.randint(300, 4000)
                post = Post(page_id=p.id, topic_id=topik.id, topic_title=topik.title, language="id",
                            visual_title=f"Post {n}", prompt_used="x", image_filename="n.jpg",
                            image_path="n.jpg", caption="c", status="published",
                            fb_post_id=f"{p.page_id}_{n}", created_at=pub, published_at=pub)
                db.add(post); db.flush()
                rx, cm, sh = reach // 60, reach // 250, reach // 180
                db.add(PostMetric(post_id=post.id, fb_post_id=post.fb_post_id, reactions=rx,
                                  comments=cm, shares=sh, reach=reach, impressions=int(reach * 1.3),
                                  calculated_score=sh*4 + cm*3 + rx*1.5 + reach*0.05))
        db.commit()
        total = db.query(Post).count()
        print(f"  {total} postingan dalam {time.perf_counter()-t0:.1f} detik\n")

        print("Waktu respons endpoint yang dipakai UI:")
        with TestClient(app_module.app) as c:
            masuk(c)
            pid = halaman[0].id
            ukur("GET /api/posts?page=N (riwayat)", c.get, f"/api/posts?page={pid}")
            ukur("GET /api/analytics/summary?page=N", c.get, f"/api/analytics/summary?page={pid}")
            ukur("GET /api/analytics/window?page=N", c.get, f"/api/analytics/window?page={pid}")
            ukur("GET /api/topics?page=N", c.get, f"/api/topics?page={pid}")
            ukur("GET /api/pages (hitung postingan tiap halaman)", c.get, "/api/pages")
            ukur("POST /api/feedback/optimize (semua halaman)", c.post, "/api/feedback/optimize")

        print("\nOperasi latar belakang:")
        ukur("window_performance 7 hari (1 halaman)", window_performance, db, 7, halaman[0].id)
        ukur("window_performance 30 hari (semua halaman)", window_performance, db, 30, None)
        ukur("optimize_all_pages", optimize_all_pages, db, 7)
        ukur("pilih topik 100x", lambda: [get_next_recommended_topic(db, halaman[0].id) for _ in range(100)])

        print("\nUkuran database:")
        db_file = Path(os.environ["AUTOPOSTER_DATA_DIR"]) / "autoposter.db"
        print(f"  {db_file.stat().st_size/1024/1024:.2f} MB untuk {total} postingan")
        print(f"  proyeksi 3 tahun (3x): {db_file.stat().st_size/1024/1024*3:.1f} MB")
        db.close()

    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
