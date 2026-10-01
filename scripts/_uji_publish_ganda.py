import os, sys, tempfile, shutil, threading, time
from pathlib import Path
ROOT = Path(tempfile.mkdtemp(prefix="autoposter_dupe_"))
os.environ["AUTOPOSTER_DATA_DIR"] = str(ROOT / "data")
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(ROOT / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import app as app_module
from scripts._masuk_uji import masuk
from database.db_session import SessionLocal
from database.models import ContentTopic, FacebookPage, Post
from config import IMAGES_DIR

terkirim = []
kunci = threading.Lock()
def publish_lambat(page_id, access_token, image_path, caption):
    time.sleep(0.8)                      # Graph API butuh waktu mengunggah foto
    with kunci:
        terkirim.append(page_id)
        n = len(terkirim)
    return {"success": True, "post_id": f"{page_id}_{n}", "post_url": f"https://fb/{n}", "message": "ok"}

app_module.publish_photo_to_page = publish_lambat

with TestClient(app_module.app) as c:
    db = SessionLocal()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (IMAGES_DIR / "p.jpg").write_bytes(b"\xff\xd8" + b"0" * 50)
    t = db.query(ContentTopic).first()
    pg = FacebookPage(page_id="dup", name="Halaman", access_token="tok", created_at=datetime.now(timezone.utc))
    db.add(pg); db.flush()
    post = Post(page_id=pg.id, topic_id=t.id, topic_title=t.title, language="id", visual_title="X",
                prompt_used="p", image_filename="p.jpg", image_path=str(IMAGES_DIR / "p.jpg"),
                caption="c", status="ready", created_at=datetime.now(timezone.utc))
    db.add(post); db.commit(); pid = post.id; db.close()

    hasil = []
    def klik():
        with TestClient(app_module.app) as cc:
            masuk(cc)
            hasil.append(cc.post(f"/api/posts/{pid}/publish", json={}).json())

    th = [threading.Thread(target=klik) for _ in range(3)]   # 3 permintaan hampir bersamaan
    for x in th: x.start()
    for x in th: x.join()

    print(f"  permintaan publish      : 3 (bersamaan)")
    print(f"  berhasil menurut API    : {sum(1 for h in hasil if h.get('success'))}")
    print(f"  TERKIRIM KE FACEBOOK    : {len(terkirim)}  <- harusnya 1")
    db = SessionLocal(); p = db.query(Post).get(pid)
    print(f"  fb_post_id tersimpan    : {p.fb_post_id} (postingan lain jadi yatim di Facebook)")
    db.close()
shutil.rmtree(ROOT, ignore_errors=True)
