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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.page_cleanup import delete_all_posts, list_page_posts  # noqa: E402
from database.db_session import SessionLocal  # noqa: E402
from database.models import FacebookPage  # noqa: E402


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

    listed = list_page_posts(page.page_id, page.access_token)
    if not listed.get("success"):
        sys.exit(listed["message"])
    posts = listed["posts"]
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

    def tampilkan(i, post, res):
        hasil = "terhapus" if res.get("success") else f"GAGAL: {res.get('message')}"
        print(f"  [{i}/{len(posts)}] {post['id']} {hasil}")

    progress = delete_all_posts(page.page_id, page.access_token, posts, on_item=tampilkan)
    print(f"\nSelesai: {progress['deleted']} terhapus, {progress['failed']} gagal. {progress['message']}".rstrip())


if __name__ == "__main__":
    main()
