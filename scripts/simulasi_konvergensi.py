"""
Tahap 2 — menjalankan 20 minggu siklus pembelajaran secara simulasi.
Pertanyaannya: apakah bobot mengendap di topik yang benar, atau berayun liar?
"""
import os, sys, tempfile, shutil, random
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="autoposter_konvergen_"))
os.environ["AUTOPOSTER_DATA_DIR"] = str(ROOT / "data")
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(ROOT / "storage")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
import app as app_module
from database.db_session import SessionLocal
from database.models import ContentTopic, FacebookPage, Post, PostMetric
from core.feedback_loop import (optimize_topic_weights, get_next_recommended_topic,
                                page_topic_weights, mark_topic_used)

MINGGU = 20
POST_PER_MINGGU = 14          # autopilot 2x sehari
SELERA = {}                    # daya tarik "sebenarnya" tiap topik bagi audiens


def reach_sebenarnya(topic_id, rng):
    """Audiens punya selera tetap + noise besar, seperti dunia nyata."""
    dasar = SELERA[topic_id]
    return max(50, int(rng.gauss(dasar, dasar * 0.45)))


def main():
    rng = random.Random(7)
    with TestClient(app_module.app):
        db = SessionLocal()
        topics = db.query(ContentTopic).all()
        # dua topik benar-benar disukai, sisanya biasa saja
        for i, t in enumerate(topics):
            SELERA[t.id] = 14000 if i in (3, 7) else rng.randint(800, 2500)

        now = datetime.now(timezone.utc)
        pg = FacebookPage(page_id="konv", name="Konvergensi", access_token="tok", created_at=now)
        db.add(pg); db.commit(); pid = pg.id

        favorit = [t for t in topics if SELERA[t.id] > 10000]
        print(f"Selera audiens (disembunyikan dari sistem): {[t.title[:32] for t in favorit]}\n")
        print(f"{'Minggu':>6} {'top-1 sesuai selera':>20} {'porsi favorit':>14} {'bobot tertinggi':>16}  topik teratas")

        riwayat = []
        for minggu in range(1, MINGGU + 1):
            for n in range(POST_PER_MINGGU):
                topik = get_next_recommended_topic(db, pid)
                pub = (now - timedelta(days=(MINGGU - minggu) * 7 + rng.random() * 6)).replace(tzinfo=None)
                reach = reach_sebenarnya(topik.id, rng)
                post = Post(page_id=pid, topic_id=topik.id, topic_title=topik.title, language="id",
                            visual_title=f"m{minggu}-{n}", prompt_used="p", image_filename="n.jpg",
                            image_path="n.jpg", caption="c", status="published",
                            fb_post_id=f"k{minggu}_{n}", created_at=pub, published_at=pub)
                db.add(post); db.flush()
                rx, cm, sh = reach // 60, reach // 250, reach // 180
                db.add(PostMetric(post_id=post.id, fb_post_id=post.fb_post_id, reactions=rx,
                                  comments=cm, shares=sh, reach=reach, impressions=int(reach * 1.3),
                                  calculated_score=sh*4 + cm*3 + rx*1.5 + reach*0.05))
                mark_topic_used(db, topik, pid)
            db.commit()

            optimize_topic_weights(db, 7, pid)
            w = page_topic_weights(db, pid, topics)
            teratas = max(topics, key=lambda t: w[t.id])
            terpilih = [get_next_recommended_topic(db, pid).id for _ in range(200)]
            porsi = sum(1 for i in terpilih if SELERA[i] > 10000) / len(terpilih)
            benar = teratas in favorit
            riwayat.append((teratas.id, w[teratas.id], porsi))
            if minggu <= 5 or minggu % 5 == 0:
                print(f"{minggu:>6} {'ya' if benar else 'TIDAK':>20} {porsi*100:>13.0f}% "
                      f"{w[teratas.id]:>16.2f}  {teratas.title[:34]}")

        print()
        benar_akhir = sum(1 for tid, _, _ in riwayat[-10:] if SELERA[tid] > 10000)
        porsi_akhir = sum(p for _, _, p in riwayat[-5:]) / 5
        pergantian = sum(1 for i in range(1, len(riwayat)) if riwayat[i][0] != riwayat[i-1][0])
        print(f"  10 minggu terakhir menempatkan topik favorit di puncak : {benar_akhir}/10")
        print(f"  porsi produksi untuk topik favorit (5 minggu terakhir) : {porsi_akhir*100:.0f}%")
        print(f"  pergantian juara sepanjang {MINGGU} minggu             : {pergantian}x")
        print(f"  rentang bobot puncak                                   : "
              f"{min(w for _, w, _ in riwayat):.2f} – {max(w for _, w, _ in riwayat):.2f}")
        db.close()
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
