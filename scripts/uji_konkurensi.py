"""
Tahap 3 — dua Fanspage terjadwal pada menit yang sama.
SQLite terkenal rentan 'database is locked' saat ada tulis bersamaan.
"""
import os, sys, tempfile, shutil, threading, time
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="autoposter_konkuren_"))
os.environ["AUTOPOSTER_DATA_DIR"] = str(ROOT / "data")
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(ROOT / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
from unittest import mock
from fastapi.testclient import TestClient
import app as app_module
from scripts._masuk_uji import masuk
import scheduler as sched
from database.db_session import SessionLocal
from database.models import FacebookPage, Post, ContentTopic

JUMLAH_HALAMAN = 4


def main():
    with TestClient(app_module.app):
        db = SessionLocal()
        now = datetime.now(timezone.utc)
        ids = []
        for i in range(JUMLAH_HALAMAN):
            p = FacebookPage(page_id=f"kon{i}", name=f"Halaman {i}", access_token="tok",
                             autopilot_enabled=True, created_at=now)
            db.add(p); db.flush(); ids.append(p.id)
        from database.models import AppSetting
        s = db.query(AppSetting).filter(AppSetting.key == "gemini_api_key").first()
        s.value = "dummy"
        db.commit(); db.close()

        def lambat_generate(*a, **kw):
            time.sleep(0.15)   # tiru latensi API sungguhan
            return {"visual_title": "J", "subtitle": "s", "imagen_prompt": "p", "caption": "c"}

        def lambat_image(**kw):
            time.sleep(0.15)
            return ("x.jpg", "x.jpg")

        def publish(page_id, access_token, image_path, caption):
            time.sleep(0.1)
            return {"success": True, "post_id": f"fb_{page_id}", "post_url": "u", "message": "ok"}

        hasil = {"error": []}

        def jalankan(page_row_id):
            try:
                sched.auto_generate_and_post_job(page_row_id)
            except Exception as e:
                hasil["error"].append(f"{page_row_id}: {type(e).__name__}: {e}")

        with mock.patch.object(sched, "generate_post_content", lambat_generate), \
             mock.patch.object(sched, "generate_poster_image", lambat_image), \
             mock.patch.object(sched, "publish_photo_to_page", publish):
            print(f"Menjalankan {JUMLAH_HALAMAN} job autopilot BERSAMAAN...")
            t0 = time.perf_counter()
            threads = [threading.Thread(target=jalankan, args=(i,)) for i in ids]
            for t in threads: t.start()
            for t in threads: t.join()
            durasi = time.perf_counter() - t0

        db = SessionLocal()
        posts = db.query(Post).all()
        print(f"  selesai dalam {durasi:.2f} detik")
        print(f"  postingan tersimpan : {len(posts)} (diharapkan {JUMLAH_HALAMAN})")
        print(f"  status              : {sorted({p.status for p in posts})}")
        print(f"  halaman unik        : {len({p.page_id for p in posts})}")
        print(f"  error               : {hasil['error'] or 'tidak ada'}")

        # pembacaan bersamaan saat penulisan berlangsung
        print("\nMembaca API saat penulisan berlangsung...")
        baca_error = []

        def pembaca():
            with TestClient(app_module.app) as c:
                masuk(c)
                for _ in range(20):
                    try:
                        r = c.get("/api/posts")
                        if r.status_code != 200:
                            baca_error.append(r.status_code)
                    except Exception as e:
                        baca_error.append(str(e)[:60])

        def penulis():
            for i in range(20):
                s = SessionLocal()
                try:
                    t = s.query(ContentTopic).first()
                    s.add(Post(page_id=ids[0], topic_id=t.id, topic_title=t.title, language="id",
                               visual_title=f"tulis-{i}", prompt_used="p", image_filename="n.jpg",
                               image_path="n.jpg", caption="c", status="ready",
                               created_at=datetime.now(timezone.utc)))
                    s.commit()
                except Exception as e:
                    baca_error.append(f"tulis: {type(e).__name__}")
                finally:
                    s.close()

        tp = [threading.Thread(target=pembaca), threading.Thread(target=penulis),
              threading.Thread(target=pembaca)]
        for t in tp: t.start()
        for t in tp: t.join()
        print(f"  kegagalan baca/tulis : {baca_error or 'tidak ada'}")
        db.close()

    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
