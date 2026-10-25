"""
Menghapus postingan Fanspage di Facebook, memakai Page Access Token yang tersimpan
di aplikasi. Tanpa --hapus hanya menampilkan daftar postingan (tidak menghapus apa pun).

    python scripts/hapus_postingan_fanspage.py "Old Man Nugget"           # lihat daftar saja
    python scripts/hapus_postingan_fanspage.py "Old Man Nugget" --hapus   # hapus (diminta konfirmasi)

Di container Dokploy: python /app/scripts/hapus_postingan_fanspage.py "Old Man Nugget"

- Yang dihapus: semua postingan milik Fanspage itu sendiri (/posts), baik yang dibuat
  aplikasi ini maupun yang diposting manual. Postingan orang lain di Fanspage tidak.
- Penghapusan di Facebook TIDAK BISA dibatalkan. Salinan di Instagram/Threads tidak ikut terhapus.
- Riwayat di database aplikasi (postingan, reach, pembelajaran topik) tidak diubah.
- Butuh izin token pages_manage_posts.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from core.fb_client import BASE_GRAPH_URL  # noqa: E402
from core.utils import redact_secrets  # noqa: E402
from database.db_session import SessionLocal  # noqa: E402
from database.models import FacebookPage  # noqa: E402

JEDA_DETIK = 1.0
# Batas laju Facebook: berhenti, jalankan lagi nanti (yang sudah terhapus tidak muncul lagi).
RATE_LIMIT_CODES = {4, 17, 32, 613}


def cari_halaman(db, nama: str) -> FacebookPage:
    pages = db.query(FacebookPage).all()
    cocok = [p for p in pages if (p.name or "").strip().lower() == nama.strip().lower()
             or str(p.page_id) == nama.strip()]
    if len(cocok) != 1:
        daftar = ", ".join(f"'{p.name}' ({p.page_id})" for p in pages) or "-"
        sys.exit(f"Fanspage '{nama}' tidak ditemukan secara tepat. Yang terdaftar: {daftar}")
    page = cocok[0]
    if not page.access_token:
        sys.exit(f"Fanspage '{page.name}' belum punya Access Token.")
    return page


def ambil_semua_postingan(page: FacebookPage) -> list:
    url = f"{BASE_GRAPH_URL}/{page.page_id}/posts"
    params = {"fields": "id,created_time,message,permalink_url", "limit": 100,
              "access_token": page.access_token}
    hasil = []
    while url:
        data = requests.get(url, params=params, timeout=30).json()
        if "error" in data:
            sys.exit(f"Gagal mengambil daftar postingan: {redact_secrets(data['error'].get('message'))}")
        hasil.extend(data.get("data", []))
        url, params = (data.get("paging") or {}).get("next"), None   # 'next' sudah berisi parameternya
    return hasil


def hapus(page: FacebookPage, post_id: str) -> dict:
    try:
        data = requests.delete(f"{BASE_GRAPH_URL}/{post_id}",
                               params={"access_token": page.access_token}, timeout=30).json()
    except Exception as e:
        return {"error": {"message": redact_secrets(e)}}
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fanspage", help="Nama Fanspage (atau Page ID) seperti di tab Fanspage")
    ap.add_argument("--hapus", action="store_true", help="Benar-benar menghapus (diminta konfirmasi)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        page = cari_halaman(db, args.fanspage)
    finally:
        db.close()

    posts = ambil_semua_postingan(page)
    print(f"Fanspage '{page.name}' ({page.page_id}): {len(posts)} postingan.")
    for p in posts:
        teks = " ".join((p.get("message") or "").split())[:70]
        print(f"  {p.get('created_time', '')[:10]}  {p['id']}  {teks}")
    if not posts:
        return
    if not args.hapus:
        print("\nTidak ada yang dihapus. Tambahkan --hapus untuk menghapus semuanya.")
        return

    print(f"\nPERINGATAN: {len(posts)} postingan di atas akan dihapus PERMANEN dari Facebook.")
    if input(f"Ketik nama Fanspage ('{page.name}') untuk melanjutkan: ").strip() != page.name:
        print("Dibatalkan. Tidak ada yang dihapus.")
        return

    terhapus, gagal = 0, []
    for i, p in enumerate(posts, 1):
        res = hapus(page, p["id"])
        if res.get("success"):
            terhapus += 1
            print(f"  [{i}/{len(posts)}] terhapus {p['id']}")
        else:
            err = res.get("error") or {}
            pesan = redact_secrets(err.get("message") or res)
            gagal.append((p["id"], pesan))
            print(f"  [{i}/{len(posts)}] GAGAL {p['id']}: {pesan}")
            if err.get("code") in RATE_LIMIT_CODES:
                print("Batas laju Facebook tercapai. Berhenti; jalankan lagi beberapa saat kemudian.")
                break
        time.sleep(JEDA_DETIK)

    print(f"\nSelesai: {terhapus} terhapus, {len(gagal)} gagal.")
    if gagal and any("permission" in m.lower() for _, m in gagal):
        print("Token kemungkinan belum punya izin pages_manage_posts.")


if __name__ == "__main__":
    main()
