# 🏆 AutoPoster Gold AI: Educational Infographic & Facebook Autonomous Publisher

### Pembaruan pengalaman pengguna

- Studio menyediakan **Simpan Draft**, status perubahan, dan peringatan sebelum meninggalkan edit. **Judul Catatan** tidak mengubah teks pada gambar; render ulang menggunakan prompt gambar tersimpan.
- Preferensi Fanspage (bahasa, rasio, jadwal, autopilot, dan jeda halaman) diterapkan bersama melalui **Simpan Preferensi**.
- Riwayat mendukung pencarian judul dan filter status pada seluruh data. Daftar topik dapat diringkas.
- Status Gemini hanya menyatakan terhubung setelah uji koneksi berhasil pada sesi tersebut. Muat ulang memerlukan verifikasi kembali.
- Analitik menampilkan “Belum cukup data” sebelum tersedia metrik, serta waktu pembaruan terakhir. Pemenang dihitung dari skor performa terkumpul.
- Jalankan pengujian alur UI tanpa layanan eksternal dengan `node --test tests/ui_state.test.cjs`; pengujian API menggunakan `python -m pytest -p no:cacheprovider` dengan database sementara.

Aplikasi cerdas untuk memproduksi konten edukasi visual (poster infografis geologi & pencarian emas ala *vintage field guide*) menggunakan **Google Gemini API (Gemini 2.5 Flash & Imagen 3)**, mempublikasikannya ke Facebook Fanspage via **Facebook Graph API**, serta mengoptimalkan topik postingan secara mandiri menggunakan **Feedback Learning Loop** berdasarkan data jangkauan (*Reach & Engagement*).

---

## ✨ Fitur Utama

1. **Manajemen Multi-Fanspage**:
   * Kelola beberapa Fanspage sekaligus dari halaman **Fanspage** khusus: tambah, verifikasi, jeda, dan hapus.
   * Setiap halaman punya **token, jam posting, bahasa konten, rasio poster, dan sakelar autopilot sendiri**.
   * **Pembelajaran topik dipisah per halaman** — audiens tiap Fanspage berbeda, jadi bobot topiknya juga berbeda.
   * **Konten diproduksi per halaman** (tidak dikembarkan): tiap Fanspage punya jadwal dan antrean sendiri.
   * Pemilih halaman aktif di header; Studio dan Analitik otomatis mengikuti halaman yang dipilih.
2. **Dashboard Interaktif Berbasis Web (Dark Gold Geology Theme)**:
   * Input dan simpan seluruh kredensial (**Gemini API Key, OpenAI API Key, Facebook Page ID, Page Access Token**) langsung dari UI tanpa perlu mengedit file kode.
   * **Pilihan mesin AI**: penyedia naskah dan penyedia gambar dipilih terpisah (Gemini atau OpenAI) — misalnya naskah dari Gemini, poster dari `gpt-image-1`. Berlaku untuk generate manual, render ulang, autopilot, dan evolusi topik. Hanya kunci penyedia yang dipilih yang wajib diisi.
   * OpenAI hanya menyediakan kanvas potret 2:3, persegi, dan lanskap; poster dibuat di kanvas yang paling dekat dengan rasio Fanspage.
   * Uji coba validitas API Key dan token dalam 1 klik.
   * Responsif penuh: navigasi tab tetap terjangkau di HP, tablet, dan desktop.
   * Seluruh aset UI (Tailwind, Alpine, FontAwesome) di-*bundle* lokal di `static/vendor/` — dashboard tetap utuh tanpa koneksi internet.
3. **AI Content Studio & Live Facebook Feed Mockup**:
   * *Progress pipeline* dengan penghitung waktu berjalan dan nama model yang benar-benar dipakai.
   * Preview menyerupai feed Facebook asli: caption terpotong 3 baris dengan "Lihat selengkapnya", poster tampil penuh sesuai rasio.
   * Editor caption & judul poster, penghitung karakter, dan tombol salin caption.
   * **Dialog konfirmasi sebelum publish** — menampilkan nama Fanspage tujuan dan cuplikan caption.
   * **Anti publish ganda**: postingan dikunci dengan status *Sedang Dipublikasikan* selama unggahan berlangsung, jadi klik ganda atau dua tab terbuka tidak pernah menghasilkan postingan kembar di Facebook. Bila aplikasi mati di tengah unggahan, postingan ditandai *Gagal* dengan peringatan untuk memeriksa Fanspage sebelum publish ulang.
   * Buka ulang postingan lama dari Riwayat ke Studio, render ulang gambar, atau hapus draft (beserta file posternya).
4. **Feedback Learning Loop (Kecerdasan Adaptif)**:
   * Menghitung skor performa postingan: $\text{Score} = (\text{Shares} \times 4) + (\text{Comments} \times 3) + (\text{Reactions} \times 1.5) + (\text{Reach} \times 0.05)$.
   * Aturan **70% Exploit** (memperbanyak variasi topik pemenang) dan **30% Explore** (menguji topik baru).
   * Visual leaderboard bobot topik yang terus diperbarui.
   * **Bobot berbasis jendela waktu**: performa 7 hari terakhir menentukan 70% bobot, riwayat seumur hidup 30%. Topik yang tidak tayang dalam periode analisis dibatasi di bobot 0.95 — tepat di bawah titik netral — sehingga juara lama maupun topik yang belum pernah dicoba tidak bisa mengalahkan topik yang sedang perform.
   * Bobot bersifat **relatif terhadap rata-rata** (1.0 = rata-rata minggu ini), jadi skalanya tetap masuk akal baik jangkauan halaman Anda ratusan maupun ratusan ribu.
   * Bila hanya **satu topik** yang tayang minggu itu, ia dibandingkan dengan postingan tipikal halaman sebelumnya (median 60 hari) — pemenang jelas mendapat bobot tinggi, yang hasilnya jeblok tetap di 1.0 tanpa didongkrak.
   * Kartu **Topik Pemenang** selalu menampilkan topik yang benar-benar diprioritaskan mesin pembelajaran, dan *Belum cukup data* sebelum ada metrik.
5. **Kurikulum Dasar & Evolusi Topik (Topik Dinamis)**:
   * **10 topik seed adalah kurikulum dasar** — fondasi permanen yang selalu diajarkan. Seluruh isinya dikirim ke AI sebagai materi belajar setiap kali topik baru dirancang.
   * Setiap Senin pukul 04:00, AI membaca topik dengan **jangkauan tertinggi selama 7 hari terakhir**, lalu menulis "bab lanjutan" dari salah satu materi dasar — lengkap dengan konsep geologi dan blueprint visualnya.
   * Setiap topik turunan **wajib berakar ke satu topik dasar**. Bila AI menyebut materi dasar yang tidak ada, sistem mencocokkan sendiri berdasarkan kemiripan isi.
   * Topik dasar ditandai badge **DASAR** (permanen, tidak bisa dinonaktifkan, bobot minimal 0.8 agar fundamental tetap tayang). Topik turunan ditandai **TURUNAN**, bobot awal 1.4, bisa dinonaktifkan bila tidak perform.
   * Validasi otomatis menolak usulan yang duplikat atau tidak lengkap.
   * Bisa dijalankan manual kapan saja lewat tombol **"Buat Topik Baru dari Pemenang"** di tab Analitik.
6. **Mode Autopilot (Background Scheduler)**:
   * Sakelar On/Off untuk posting otomatis dan sinkronisasi metrik di latar belakang.
   * Jam posting diatur **per Fanspage** di tab Fanspage (`10:00,19:00`), dan tiap halaman menampilkan kapan jadwal berikutnya berjalan.
7. **Kesiapan Deploy via Dokploy**:
   * Dilengkapi `Dockerfile`, `docker-compose.yml`, dan volume persisten untuk database SQLite dan gambar yang di-generate.

---

## 💬 Balas Komentar Otomatis

Tab **Komentar** membalas komentar pengikut di **semua postingan** Fanspage, baik yang dibuat aplikasi ini maupun yang diposting manual, dengan gaya admin manusia: singkat, santai, **maksimal 2 kalimat**, mengikuti bahasa si pengomentar, tanpa hashtag atau tautan.

* **Per Fanspage**: sakelar nyala/mati, mode **Otomatis** (langsung terkirim) atau **Perlu persetujuan** (AI menyiapkan draft, Anda yang menekan kirim), dan batas balasan per jam (bawaan 20).
* Diperiksa **tiap 10 menit**, dengan jeda acak 15–45 detik antar balasan. Tombol *Periksa sekarang* menjalankannya seketika.
* **Tidak dibalas**: komentar Fanspage sendiri, komentar yang sudah dibalas admin, stiker/foto tanpa teks, komentar bertautan, spam/judi/kasar (diputuskan AI), dan komentar yang lebih lama dari 48 jam.
* **Sekali balas**: setiap komentar dikunci dengan ID uniknya, jadi tidak pernah dibalas dua kali walau pemeriksaan berjalan bersamaan.
* Balasan sebelumnya ikut diberikan ke AI agar kalimatnya tidak berulang.
* Memakai penyedia teks yang dipilih di Pengaturan (Gemini atau OpenAI).
* **Izin token yang dibutuhkan**: `pages_read_engagement`, `pages_read_user_content`, `pages_manage_engagement`. Bila kurang, pesan kesalahannya tampil di panel Balasan Otomatis.

## 🚀 Cara Menjalankan Lokal di Windows

1. **Jalankan via Script Otomatis**:
   * Cukup klik ganda file **`run.bat`**.
   * Script akan menyalakan server lokal dan otomatis membuka browser Anda ke `http://127.0.0.1:8000`.

2. **Atau Jalankan via Terminal (PowerShell / Command Prompt)**:
   ```bash
   python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
   ```
   Buka browser di: `http://127.0.0.1:8000`.

3. **Login**: semua halaman dan API butuh login. Akun dibuat (atau password-nya diganti) dari terminal:
   ```bash
   python scripts/atur_login.py email@anda.com
   ```
   Password diketik tersembunyi dan hanya disimpan sebagai hash. Setelah masuk, password juga bisa diganti dari kartu **Akun Login** di tab Pengaturan — perangkat lain otomatis ikut keluar.

---

## 🧪 Menjalankan Tes

```bash
pip install -r requirements-dev.txt
pytest
```

Suite ini berjalan di database dan folder gambar sementara — `data/autoposter.db` milik Anda **tidak pernah tersentuh**. Seluruh panggilan ke Gemini dan Facebook digantikan tiruan, jadi tes jalan tanpa API key, tanpa kuota, dan tanpa internet.

| Berkas | Cakupan |
|---|---|
| `test_pages.py` | Manajemen Fanspage: pendaftaran, verifikasi, token termask, preferensi per halaman, penghapusan |
| `test_learning.py` | Pembelajaran adaptif: pemisahan antar-halaman, cold start netral, bobot relatif, lantai & batas |
| `test_publishing.py` | Generate & publish: caption yang benar, draft yatim, rasio poster, pembersihan berkas |
| `test_topic_evolution.py` | Evolusi topik: kurikulum dasar sebagai materi belajar, penautan akar, penolakan duplikat |
| `test_scheduler.py` | Jadwal per halaman, gerbang token, parsing jam, job autopilot |
| `test_metrics_and_api.py` | Batas sinkronisasi metrik, kebocoran rahasia, penanda UTC |
| `test_migrations.py` | Upgrade database lama tanpa kehilangan data |
| `test_drafts_ui.py` | Simpan draft, penguncian postingan tayang, filter riwayat |
| `test_audit_regresi.py` | Publish bersamaan, pemulihan status macet, pemenang tunggal, konsistensi kartu pemenang |
| `test_openai.py` | Kunci OpenAI tersamar, pemilihan penyedia per peran (termasuk campuran), klien REST OpenAI, ukuran kanvas |
| `test_comment_reply.py` | Komentar mana yang dibalas, mode otomatis/persetujuan, sekali balas (termasuk kirim bersamaan), batas per jam, izin token, kealamian & batas 2 kalimat |

Tiap tes yang menyandang komentar `Regresi:` mengunci bug yang pernah benar-benar terjadi, supaya tidak kembali.

### Skrip audit (folder `scripts/`)

Dijalankan manual saat ingin memeriksa perilaku jangka panjang. Semuanya memakai database sementara.

```bash
python scripts/benchmark_skala.py        # waktu respons dengan setahun data
python scripts/simulasi_konvergensi.py   # 20 minggu siklus pembelajaran
python scripts/uji_konkurensi.py         # beberapa halaman posting bersamaan
python scripts/uji_pemulihan.py          # gagal di tengah jalan + backup/restore
```

---

## 💾 Backup

Cukup salin dua folder ini saat aplikasi **berhenti**:

```
data/       -> database (kredensial, riwayat, pembelajaran)
storage/    -> poster hasil generate
```

Database memakai mode **WAL**, jadi saat aplikasi sedang berjalan ada berkas pendamping `autoposter.db-wal` dan `-shm`. Kalau menyalin tanpa menghentikan aplikasi, sertakan ketiganya — kalau tidak, transaksi terakhir bisa hilang. Memulihkan cukup mengembalikan kedua folder; jadwal autopilot dibangun ulang sendiri dari database saat aplikasi dinyalakan.

---

## 🐳 Cara Deploy di Dokploy (VPS)

Repository ini siap di-deploy ke **Dokploy** sebagai *Application* (Dockerfile):

1. **Buat Aplikasi Baru**: *Project* → **Create Service** → **Application**.
2. **Source**: pilih **GitHub** (atau *Git* dengan URL `https://github.com/aburasyidalfatih/goldgenv2.git`), branch `main`.
3. **Build Type**: **Dockerfile** (path `Dockerfile`, context `.`).
4. **Environment** (tab *Environment*, lihat `.env.example`):
   ```env
   AUTOPOSTER_ADMIN_EMAIL=email@anda.com
   AUTOPOSTER_ADMIN_PASSWORD=password-awal-yang-panjang
   SCHEDULER_TIMEZONE=Asia/Jakarta
   ```
   Akun login dibuat otomatis saat start pertama **hanya bila database belum punya akun**. Setelah berhasil login, hapus `AUTOPOSTER_ADMIN_PASSWORD` dari Dokploy dan ganti password lewat **Pengaturan → Akun Login**.
   `SCHEDULER_TIMEZONE` menentukan zona waktu jam posting Autopilot (default `Asia/Jakarta`).
5. **PENTING — Volume persisten** (tab *Advanced* → *Volumes/Mounts*), supaya database & poster tidak hilang saat redeploy:
   * Volume Mount `autoposter_data` → Mount Path `/app/data`
   * Volume Mount `autoposter_storage` → Mount Path `/app/storage`
6. **Domain** (tab *Domains*): isi domain Anda, **Container Port `8000`**, aktifkan **HTTPS** (Let's Encrypt).
7. **Deploy**. Health check `/healthz` sudah ada di Dockerfile; cookie login otomatis bertanda `Secure` lewat HTTPS.

Catatan:
* Jalankan **satu replika saja**. Scheduler Autopilot berjalan di dalam proses aplikasi dan database-nya SQLite — dua replika berarti posting ganda.
* Alternatif: tipe **Docker Compose** memakai `docker-compose.yml` di repo ini (volume & environment sudah didefinisikan; atur domain ke port `8000`).
* Ingin memindahkan data lokal (API key, Fanspage, riwayat postingan)? Hentikan aplikasi lokal, lalu salin `data/autoposter.db` ke volume `/app/data` di server sebelum start pertama. Jika tidak, cukup isi ulang pengaturan dari dashboard.
* Lupa password? Dari terminal container di Dokploy: `python scripts/atur_login.py email@anda.com`.

---

## ⚙️ Konfigurasi Lanjutan (Environment Variables)

| Variable | Default | Fungsi |
|---|---|---|
| `SCHEDULER_TIMEZONE` | `Asia/Jakarta` | Zona waktu yang dipakai Autopilot saat membaca jam posting (`auto_post_times`). |
| `AUTOPOSTER_ADMIN_EMAIL` / `AUTOPOSTER_ADMIN_PASSWORD` | — | Membuat akun login pertama saat start, hanya bila belum ada akun. |
| `FORWARDED_ALLOW_IPS` | `*` (di Dockerfile) | Proxy yang dipercaya untuk header `X-Forwarded-*` (Traefik Dokploy). |
| `FB_GRAPH_API_VERSION` | `v25.0` | Versi Facebook Graph API. Ganti bila Meta men-sunset versi ini. |
| `AUTOPOSTER_DATA_DIR` | `./data` | Lokasi database SQLite. Dipakai suite tes agar tidak menyentuh data asli. |
| `AUTOPOSTER_STORAGE_DIR` | `./storage` | Lokasi poster hasil generate. |

Pengaturan evolusi topik (`auto_topic_evolution`, `topic_window_days`, `max_new_topics_per_cycle`) diatur dari tab **Pengaturan**, bukan environment variable.

**Catatan soal data metrik:** tabel metrik menyimpan *snapshot terakhir* per postingan, bukan riwayat harian. Jadi "7 hari terakhir" berarti *postingan yang tayang dalam 7 hari terakhir beserta angkanya saat ini* — bukan jumlah tayangan yang terjadi persis dalam rentang itu.

Jam posting Autopilot diatur per Fanspage di tab **Fanspage** (format `HH:MM,HH:MM`, default `10:00,19:00`) dan jadwal cron langsung dibangun ulang begitu pengaturan disimpan.

**Migrasi otomatis:** bila sebelumnya Anda sudah mengisi satu Fanspage di tab Pengaturan, konfigurasi itu dipindahkan sendiri ke tabel `facebook_pages` saat aplikasi pertama kali dijalankan, lengkap dengan seluruh riwayat postingannya — tidak ada yang perlu diisi ulang.

Aset front-end berada di `static/vendor/` (Tailwind runtime 3.4.16, Alpine 3.14.9, FontAwesome 6.4.0). Tailwind di sini memakai *runtime compiler* sehingga console browser menampilkan peringatan "cdn.tailwindcss.com should not be used in production" — aman diabaikan; mengganti ke hasil build Tailwind CLI adalah opsi optimasi lanjutan.

## ⚠️ Catatan Keamanan

Dashboard, API, dan poster hasil generate hanya bisa dibuka setelah login. Password disimpan sebagai hash PBKDF2, sesi berupa cookie HttpOnly (berlaku 30 hari) yang berakhir saat keluar atau saat password diganti, dan login gagal dibatasi (5 kali per IP, 20 kali per email dari IP mana pun) dengan kunci 15 menit. Pakai password yang panjang — terutama bila di-deploy ke domain publik. API key dan Page Access Token tidak pernah dikirim balik ke browser dalam bentuk asli (hanya ditampilkan sebagai `••••••••••••`). Jangan pernah commit folder `data/` ke Git — di situlah kredensial disimpan (sudah dicegah lewat `.gitignore`).

---

## 📖 Panduan Penggunaan Pertama Kali

1. Buka tab **Pengaturan**:
   * Pilih **penyedia naskah** dan **penyedia gambar** (Gemini atau OpenAI).
   * Masukkan API Key penyedia yang dipilih, lalu klik **"Uji Koneksi Gemini API"** atau **"Uji Koneksi OpenAI API"** (uji OpenAI hanya memeriksa kunci dan ketersediaan model, tanpa memakai token).
   * Masukkan **Facebook Page ID** dan **Page Access Token**, lalu klik tombol **"Verifikasi & Deteksi Fanspage"**. Sistem akan otomatis menampilkan nama dan avatar Fanspage Anda.
   * Klik **"Simpan Seluruh Pengaturan"**.
2. Masuk ke tab **Studio**:
   * Pilih topik atau biarkan pada opsi *Auto-Select by AI*.
   * Klik **"Generate Konten Baru"**.
   * AI akan merumuskan naskah dan merender poster infografis penampang sungai ala panduan lapangan.
   * Anda bisa mengedit caption jika diperlukan, lalu klik **"Publish ke Fanspage Facebook Sekarang"**.
3. Masuk ke tab **Analitik & Feedback**:
   * Klik **"Refresh Metrik Facebook"** untuk menarik data likes, comments, shares, dan reach dari postingan yang sudah tayang.
   * Klik **"Hitung Ulang Bobot Sekarang"** untuk melihat topik apa yang paling diminati audiens Anda.
