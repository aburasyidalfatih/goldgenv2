"""
Tahap 3 — apa yang tersisa kalau aplikasi mati di tengah jalan,
dan apakah backup benar-benar bisa dipulihkan.
"""
import os, sys, tempfile, shutil, subprocess
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="autoposter_pulih_"))
DATA, STORAGE = ROOT / "data", ROOT / "storage"
os.environ["AUTOPOSTER_DATA_DIR"] = str(DATA)
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(STORAGE)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone
from unittest import mock
from fastapi.testclient import TestClient
import app as app_module
import scheduler as sched
from database.db_session import SessionLocal, engine
from database.models import AppSetting, FacebookPage, Post, ContentTopic
from config import IMAGES_DIR


def siapkan():
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    pg = FacebookPage(page_id="pulih", name="Halaman Pulih", access_token="tok",
                      autopilot_enabled=True, auto_post_times="09:00,21:00", created_at=now)
    db.add(pg)
    s = db.query(AppSetting).filter(AppSetting.key == "gemini_api_key").first()
    s.value = "dummy"
    db.commit()
    pid = pg.id
    db.close()
    return pid


def main():
    with TestClient(app_module.app):
        pid = siapkan()

        print("1. GAGAL DI TENGAH: render gambar berhasil, publish melempar exception")
        gambar_dibuat = []

        def buat_gambar(**kw):
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            f = IMAGES_DIR / f"yatim_{len(gambar_dibuat)}.jpg"
            f.write_bytes(b"\xff\xd8\xff\xdb" + b"0" * 100)
            gambar_dibuat.append(f)
            return f.name, str(f)

        with mock.patch.object(sched, "generate_post_content",
                               lambda *a, **k: {"visual_title": "J", "subtitle": "s",
                                                "imagen_prompt": "p", "caption": "c"}), \
             mock.patch.object(sched, "generate_poster_image", buat_gambar), \
             mock.patch.object(sched, "publish_photo_to_page",
                               side_effect=RuntimeError("koneksi putus")):
            sched.auto_generate_and_post_job(pid)

        db = SessionLocal()
        jml_post = db.query(Post).count()
        db.close()
        print(f"   postingan tersimpan : {jml_post}")
        print(f"   file gambar dibuat  : {len(gambar_dibuat)}")
        yatim = [f for f in gambar_dibuat if f.exists()]
        print(f"   -> gambar yatim tanpa baris database: {len(yatim)}")

        print("\n2. BACKUP & RESTORE")
        db = SessionLocal()
        t = db.query(ContentTopic).first()
        db.add(Post(page_id=pid, topic_id=t.id, topic_title=t.title, language="id",
                    visual_title="Sebelum backup", prompt_used="p", image_filename="n.jpg",
                    image_path="n.jpg", caption="c", status="ready",
                    created_at=datetime.now(timezone.utc)))
        db.commit()
        sebelum = db.query(Post).count()
        db.close()
        engine.dispose()   # tutup koneksi agar file WAL ter-checkpoint

        backup = ROOT / "backup"
        shutil.copytree(DATA, backup / "data")
        shutil.copytree(STORAGE, backup / "storage")
        berkas = sorted(p.name for p in (backup / "data").iterdir())
        print(f"   isi backup/data: {berkas}")

        shutil.rmtree(DATA); shutil.rmtree(STORAGE)
        shutil.copytree(backup / "data", DATA)
        shutil.copytree(backup / "storage", STORAGE)

        db = SessionLocal()
        sesudah = db.query(Post).count()
        halaman = db.query(FacebookPage).first()
        print(f"   postingan sebelum={sebelum} sesudah restore={sesudah}")
        print(f"   halaman pulih   : {halaman.name}, jam={halaman.auto_post_times}, "
              f"autopilot={bool(halaman.autopilot_enabled)}, token={'ada' if halaman.access_token else 'HILANG'}")
        db.close()

        print("\n3. JADWAL SETELAH RESTART")
        jadwal = sched.reload_autopost_schedule()
        print(f"   job dibangun ulang dari database: {jadwal}")

    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
