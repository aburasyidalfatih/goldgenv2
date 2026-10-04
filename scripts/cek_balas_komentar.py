"""
Diagnosa fitur Balas Komentar Otomatis, langsung terhadap Facebook dan database asli.
Tidak mengirim balasan apa pun dan tidak mengubah database.

    python scripts/cek_balas_komentar.py            # semua Fanspage
    python scripts/cek_balas_komentar.py --uji-ai   # sekalian uji 1 panggilan model AI

Di container Dokploy: python /app/scripts/cek_balas_komentar.py
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from core import comment_reply as cr  # noqa: E402
from core.ai_provider import ai_backend  # noqa: E402
from core.fb_client import BASE_GRAPH_URL, fetch_recent_comments  # noqa: E402
from database.db_session import SessionLocal  # noqa: E402
from database.models import CommentReply, FacebookPage  # noqa: E402

NEEDED_SCOPES = ("pages_read_engagement", "pages_read_user_content", "pages_manage_engagement")


def check_token(page: FacebookPage) -> None:
    me = requests.get(f"{BASE_GRAPH_URL}/me", params={"fields": "id,name", "access_token": page.access_token},
                      timeout=15).json()
    if "error" in me:
        print(f"   [X] Token ditolak Facebook: {me['error'].get('message')}")
        return
    if str(me.get("id")) == str(page.page_id):
        print(f"   [OK] Page Access Token milik '{me.get('name')}'")
    else:
        print(f"   [X] Token milik '{me.get('name')}' (id {me.get('id')}), BUKAN Fanspage id {page.page_id}. "
              "Kemungkinan User Token: balasan tidak bisa dikirim atas nama Fanspage. "
              "Klik 'Verifikasi ulang' di tab Fanspage atau ganti dengan Page Access Token.")
    dbg = requests.get(f"{BASE_GRAPH_URL}/debug_token",
                       params={"input_token": page.access_token, "access_token": page.access_token},
                       timeout=15).json().get("data") or {}
    scopes = set(dbg.get("scopes") or [])
    if scopes:
        missing = [s for s in NEEDED_SCOPES if s not in scopes]
        print(f"   {'[X] Izin kurang: ' + ', '.join(missing) if missing else '[OK] Izin token lengkap'}")
    if dbg.get("expires_at"):
        exp = datetime.fromtimestamp(dbg["expires_at"], timezone.utc)
        print(f"   [!] Token kedaluwarsa pada {exp:%Y-%m-%d %H:%M} UTC (gunakan token permanen)")


def explain_comments(db, page: FacebookPage, posts: list) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(hours=cr.REPLY_LOOKBACK_HOURS)
    handled = {r.fb_comment_id: r.status for r in db.query(CommentReply).filter(CommentReply.page_id == page.id)}
    total = 0
    for post in posts:
        for c in (post.get("comments") or {}).get("data", []):
            total += 1
            author = c.get("from") or {}
            created = cr._parse_time(c.get("created_time"))
            message = (c.get("message") or "").strip()
            if not author:
                reason = "BALAS (tapi field 'from' kosong: izin pages_read_user_content/Page Token kurang)"
            else:
                reason = "BALAS"
            if str(author.get("id")) == str(page.page_id):
                reason = "lewati: komentar Fanspage sendiri"
            elif any(str((r.get("from") or {}).get("id")) == str(page.page_id)
                     for r in (c.get("comments") or {}).get("data", [])):
                reason = "lewati: sudah dibalas admin"
            elif not message:
                reason = "lewati: tanpa teks (stiker/foto)"
            elif created and created < cutoff:
                reason = f"lewati: lebih lama dari {cr.REPLY_LOOKBACK_HOURS} jam"
            elif c.get("id") in handled:
                reason = f"sudah diproses sebelumnya (status: {handled[c['id']]})"
            elif cr.LINK_PATTERN.search(message):
                reason = "lewati: berisi tautan"
            print(f"     - [{c.get('created_time')}] {author.get('name', '?')}: {message[:60]!r}\n       -> {reason}")
    if not total:
        print("     (tidak ada komentar di postingan 14 hari terakhir)")


def main():
    parser = argparse.ArgumentParser(description="Diagnosa Balas Komentar Otomatis.")
    parser.add_argument("--uji-ai", action="store_true", help="uji satu panggilan model AI")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        text_ai = ai_backend(db, "text")
        print(f"Penyedia teks: {text_ai['label']} / model {text_ai['model']}")
        if text_ai["missing"]:
            print(f"[X] {text_ai['missing']}")
        elif args.uji_ai:
            try:
                res = cr.generate_comment_reply(text_ai, "Uji", "id", "Emas mengendap di tikungan dalam sungai.",
                                                "Budi", "Kalau di sungai kecil juga bisa ya bang?", [])
                print(f"[OK] AI menjawab: {res}")
            except Exception as e:
                print(f"[X] Model AI gagal: {e}")

        pages = db.query(FacebookPage).all()
        if not pages:
            print("Belum ada Fanspage terdaftar.")
        for page in pages:
            print(f"\n=== {page.name} (id {page.page_id})")
            print(f"   aktif={page.is_active is not False}  balas_otomatis={bool(page.auto_reply_enabled)}  "
                  f"mode={page.auto_reply_mode}  batas/jam={page.reply_max_per_hour}")
            print(f"   putaran terakhir={page.reply_last_run} UTC  error terakhir={page.reply_last_error or '-'}")
            counts = {}
            for (status,) in db.query(CommentReply.status).filter(CommentReply.page_id == page.id):
                counts[status] = counts.get(status, 0) + 1
            print(f"   riwayat balasan: {counts or 'kosong'}")
            if not page.auto_reply_enabled:
                print("   [!] Balas otomatis MATI: nyalakan di tab Komentar lalu Simpan.")
            if page.auto_reply_mode == "review":
                print("   [!] Mode 'Perlu persetujuan': balasan hanya jadi draft, tidak terkirim sendiri.")
            if not page.access_token:
                print("   [X] Access Token kosong.")
                continue
            try:
                check_token(page)
            except requests.RequestException as e:
                print(f"   [X] Tidak bisa menghubungi Facebook ({type(e).__name__}). Periksa koneksi internet server.")
                continue
            fetched = fetch_recent_comments(page.page_id, page.access_token, cr.POST_SCAN_DAYS)
            if not fetched.get("success"):
                print(f"   [X] Gagal membaca komentar: {fetched.get('message')}")
                continue
            print(f"   {len(fetched['posts'])} postingan dalam {cr.POST_SCAN_DAYS} hari terakhir. Komentar:")
            explain_comments(db, page, fetched["posts"])
    finally:
        db.close()


if __name__ == "__main__":
    main()
