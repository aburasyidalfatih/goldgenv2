"""
Schema and data migrations. These guard the upgrade path for a database that is
already in use: nobody should have to delete data/autoposter.db to upgrade.
"""
import pytest
from sqlalchemy import create_engine, inspect, text

from database.migrations import migrate_single_page_to_multi, run_migrations


@pytest.fixture
def db_versi_lama(tmp_dir):
    """Sebuah database gaya rilis awal: tanpa kolom & tabel yang baru."""
    path = tmp_dir / "lama.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE app_settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at DATETIME
            )"""))
        conn.execute(text("""
            CREATE TABLE content_topics (
                id INTEGER PRIMARY KEY,
                category VARCHAR(50),
                topic_key VARCHAR(100) UNIQUE,
                title VARCHAR(200) NOT NULL,
                core_concept TEXT NOT NULL,
                visual_blueprint TEXT NOT NULL,
                weight FLOAT DEFAULT 1.0,
                posts_count INTEGER DEFAULT 0,
                total_score FLOAT DEFAULT 0.0,
                avg_reach FLOAT DEFAULT 0.0,
                last_used_at DATETIME
            )"""))
        conn.execute(text("""
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                topic_title VARCHAR(200) NOT NULL,
                language VARCHAR(10),
                visual_title VARCHAR(250) NOT NULL,
                prompt_used TEXT NOT NULL,
                image_filename VARCHAR(255) NOT NULL,
                image_path VARCHAR(500) NOT NULL,
                caption TEXT NOT NULL,
                status VARCHAR(50),
                fb_post_id VARCHAR(100),
                fb_post_url VARCHAR(500),
                created_at DATETIME,
                published_at DATETIME,
                error_message TEXT
            )"""))
        for key, value in [
            ("fb_page_id", "108234981273891"),
            ("fb_page_access_token", "EAA-token-lama-rahasia"),
            ("fb_page_name", "Fanspage Emas Lama"),
            ("fb_page_picture", "https://example.test/lama.jpg"),
            ("fb_token_status", "Terhubung"),
            ("content_language", "id"),
            ("aspect_ratio", "1:1"),
            ("auto_post_times", "08:00,20:00"),
            ("auto_scheduler_enabled", "true"),
        ]:
            conn.execute(text("INSERT INTO app_settings (key, value) VALUES (:k, :v)"),
                         {"k": key, "v": value})
        conn.execute(text(
            "INSERT INTO content_topics (id, category, topic_key, title, core_concept, visual_blueprint, weight)"
            " VALUES (1, 'Fluvial', 'inside_bend_rule', 'Inside Bend', 'konsep', 'blueprint', 1.2)"
        ))
        for i in range(3):
            conn.execute(text(
                "INSERT INTO posts (topic_id, topic_title, language, visual_title, prompt_used,"
                " image_filename, image_path, caption, status, fb_post_id)"
                " VALUES (1, 'Inside Bend', 'id', :t, 'p', 'f.jpg', 'f.jpg', 'c', 'published', :fb)"
            ), {"t": f"Postingan lama {i}", "fb": f"fb_{i}"})
    return engine, path


def test_kolom_baru_ditambahkan_tanpa_menghapus_data(db_versi_lama):
    engine, _ = db_versi_lama

    applied = run_migrations(engine)

    kolom_topik = {c["name"] for c in inspect(engine).get_columns("content_topics")}
    assert {"source", "parent_topic_id", "base_topic_id", "origin_note", "is_active"} <= kolom_topik
    assert "posts.page_id" in applied
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM posts")).scalar() == 3


def test_topik_lama_ditandai_sebagai_kurikulum_dasar(db_versi_lama):
    engine, _ = db_versi_lama

    run_migrations(engine)

    with engine.connect() as conn:
        source, base_id, aktif = conn.execute(
            text("SELECT source, base_topic_id, is_active FROM content_topics WHERE id = 1")
        ).fetchone()
    assert source == "seed"
    assert base_id == 1, "topik dasar adalah akar dirinya sendiri"
    assert aktif == 1


def test_migrasi_berjalan_dua_kali_tanpa_efek_samping(db_versi_lama):
    engine, _ = db_versi_lama

    pertama = run_migrations(engine)
    kedua = run_migrations(engine)

    assert pertama and kedua == [], "jalan kedua tidak boleh mengubah apa pun"


def test_fanspage_lama_pindah_lengkap_dengan_preferensinya(db_versi_lama):
    engine, _ = db_versi_lama
    run_migrations(engine)
    from database.models import Base

    Base.metadata.create_all(bind=engine)

    nama = migrate_single_page_to_multi(engine)

    assert nama == "Fanspage Emas Lama"
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT name, page_id, access_token, content_language, aspect_ratio,"
            " auto_post_times, autopilot_enabled FROM facebook_pages"
        )).fetchone()
    assert row.name == "Fanspage Emas Lama"
    assert row.page_id == "108234981273891"
    assert row.access_token == "EAA-token-lama-rahasia"
    assert (row.content_language, row.aspect_ratio) == ("id", "1:1")
    assert row.auto_post_times == "08:00,20:00"
    assert row.autopilot_enabled == 1


def test_riwayat_postingan_ikut_tertaut_ke_halaman(db_versi_lama):
    engine, _ = db_versi_lama
    run_migrations(engine)
    from database.models import Base

    Base.metadata.create_all(bind=engine)

    migrate_single_page_to_multi(engine)

    with engine.connect() as conn:
        yatim = conn.execute(text("SELECT COUNT(*) FROM posts WHERE page_id IS NULL")).scalar()
        tertaut = conn.execute(text("SELECT COUNT(*) FROM posts WHERE page_id IS NOT NULL")).scalar()
    assert (yatim, tertaut) == (0, 3)


def test_token_lama_dibersihkan_dari_app_settings(db_versi_lama):
    """Regresi: kredensial dulu tersimpan ganda setelah migrasi."""
    engine, _ = db_versi_lama
    run_migrations(engine)
    from database.models import Base

    Base.metadata.create_all(bind=engine)

    migrate_single_page_to_multi(engine)

    with engine.connect() as conn:
        sisa = dict(conn.execute(text(
            "SELECT key, value FROM app_settings WHERE key LIKE 'fb_page%'"
        )).all())
    assert all(v == "" for v in sisa.values()), sisa


def test_migrasi_fanspage_hanya_sekali(db_versi_lama):
    engine, _ = db_versi_lama
    run_migrations(engine)
    from database.models import Base

    Base.metadata.create_all(bind=engine)

    assert migrate_single_page_to_multi(engine) is not None
    assert migrate_single_page_to_multi(engine) is None

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM facebook_pages")).scalar() == 1


def test_database_tanpa_kredensial_tidak_membuat_halaman_kosong(tmp_dir):
    from database.models import Base

    path = tmp_dir / "baru.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(bind=engine)

    assert migrate_single_page_to_multi(engine) is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM facebook_pages")).scalar() == 0
