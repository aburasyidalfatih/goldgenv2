"""
Lightweight schema migrations for the SQLite database.

`Base.metadata.create_all()` only creates missing tables; it never adds a column
to a table that already exists. These helpers bring an existing database up to
date without requiring the user to delete data/autoposter.db.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy import text, inspect

from config import DEFAULT_CONTENT_LANGUAGE, DEFAULT_ASPECT_RATIO, POSTER_RATIOS, normalize_aspect_ratio

logger = logging.getLogger(__name__)

# (table, column, SQL type + default) added after the initial release
ADDED_COLUMNS = [
    ("content_topics", "source", "VARCHAR(20) DEFAULT 'seed'"),
    ("content_topics", "parent_topic_id", "INTEGER"),
    ("content_topics", "base_topic_id", "INTEGER"),
    ("content_topics", "origin_note", "TEXT"),
    ("content_topics", "created_at", "DATETIME"),
    ("content_topics", "is_active", "BOOLEAN DEFAULT 1"),
    ("content_topics", "origin_page_id", "INTEGER"),
    ("posts", "page_id", "INTEGER"),
    ("facebook_pages", "auto_reply_enabled", "BOOLEAN DEFAULT 0"),
    ("facebook_pages", "auto_reply_mode", "VARCHAR(20) DEFAULT 'auto'"),
    ("facebook_pages", "reply_max_per_hour", "INTEGER DEFAULT 20"),
    ("facebook_pages", "reply_last_run", "DATETIME"),
    ("facebook_pages", "reply_last_error", "TEXT"),
    ("facebook_pages", "focus_leader_key", "INTEGER"),
    ("facebook_pages", "focus_cursor", "INTEGER DEFAULT 0"),
    ("facebook_pages", "color_theme", "VARCHAR(30) DEFAULT 'parchment'"),
    ("facebook_pages", "promo_enabled", "BOOLEAN DEFAULT 0"),
    ("facebook_pages", "promo_url", "VARCHAR(500) DEFAULT ''"),
    ("facebook_pages", "promo_note", "TEXT DEFAULT ''"),
    ("facebook_pages", "promo_last_error", "TEXT"),
    ("posts", "promo_status", "VARCHAR(20)"),
    ("posts", "promo_comment", "TEXT"),
    ("posts", "promo_comment_fb_id", "VARCHAR(100)"),
    ("posts", "promo_attempts", "INTEGER DEFAULT 0"),
    ("facebook_pages", "ig_enabled", "BOOLEAN DEFAULT 0"),
    ("facebook_pages", "ig_enabled_at", "DATETIME"),
    ("facebook_pages", "ig_user_id", "VARCHAR(100)"),
    ("facebook_pages", "ig_username", "VARCHAR(200)"),
    ("facebook_pages", "ig_last_error", "TEXT"),
    ("posts", "ig_status", "VARCHAR(20)"),
    ("posts", "ig_media_id", "VARCHAR(100)"),
    ("posts", "ig_permalink", "VARCHAR(500)"),
    ("posts", "ig_error", "TEXT"),
    ("posts", "ig_attempts", "INTEGER DEFAULT 0"),
    ("posts", "ig_media_token", "VARCHAR(64)"),
    ("posts", "ig_media_expires", "DATETIME"),
    ("facebook_pages", "threads_enabled", "BOOLEAN DEFAULT 0"),
    ("facebook_pages", "threads_enabled_at", "DATETIME"),
    ("facebook_pages", "threads_access_token", "TEXT"),
    ("facebook_pages", "threads_user_id", "VARCHAR(100)"),
    ("facebook_pages", "threads_username", "VARCHAR(200)"),
    ("facebook_pages", "threads_token_expires", "DATETIME"),
    ("facebook_pages", "threads_token_refreshed_at", "DATETIME"),
    ("facebook_pages", "threads_last_error", "TEXT"),
    ("posts", "threads_status", "VARCHAR(20)"),
    ("posts", "threads_media_id", "VARCHAR(100)"),
    ("posts", "threads_permalink", "VARCHAR(500)"),
    ("posts", "threads_error", "TEXT"),
    ("posts", "threads_attempts", "INTEGER DEFAULT 0"),
    ("posts", "threads_parts_done", "INTEGER DEFAULT 0"),
    ("posts", "threads_last_id", "VARCHAR(100)"),
    ("posts", "threads_media_token", "VARCHAR(64)"),
    ("posts", "threads_media_expires", "DATETIME"),
]


def migrate_single_page_to_multi(engine) -> str | None:
    """
    Moves the original single-Fanspage configuration out of app_settings into the
    facebook_pages table, and attaches existing posts to it. Runs once: after the
    first page row exists it does nothing.
    """
    with engine.begin() as conn:
        tables = set(inspect(engine).get_table_names())
        if "facebook_pages" not in tables or "app_settings" not in tables:
            return None

        already = conn.execute(text("SELECT COUNT(*) FROM facebook_pages")).scalar()
        if already:
            return None

        settings = dict(conn.execute(text("SELECT key, value FROM app_settings")).all())
        page_id = (settings.get("fb_page_id") or "").strip()
        token = (settings.get("fb_page_access_token") or "").strip()
        if not page_id or not token:
            return None  # nothing configured yet; the user will add pages in the UI

        conn.execute(
            text("""
                INSERT INTO facebook_pages
                    (page_id, name, access_token, picture_url, content_language,
                     aspect_ratio, auto_post_times, autopilot_enabled, is_active,
                     token_status, created_at)
                VALUES
                    (:page_id, :name, :token, :picture, :lang,
                     :ratio, :times, :autopilot, 1,
                     :status, :created)
            """),
            {
                "page_id": page_id,
                "name": settings.get("fb_page_name") or f"Fanspage {page_id}",
                "token": token,
                "picture": settings.get("fb_page_picture") or None,
                "lang": settings.get("content_language") or DEFAULT_CONTENT_LANGUAGE,
                "ratio": normalize_aspect_ratio(settings.get("aspect_ratio")),
                "times": settings.get("auto_post_times") or "10:00,19:00",
                "autopilot": 1 if (settings.get("auto_scheduler_enabled") or "").lower() == "true" else 0,
                "status": settings.get("fb_token_status") or "Dipindahkan dari pengaturan lama",
                "created": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
            },
        )
        new_id = conn.execute(text("SELECT id FROM facebook_pages WHERE page_id = :p"), {"p": page_id}).scalar()

        # Every existing post belonged to that single page.
        conn.execute(text("UPDATE posts SET page_id = :id WHERE page_id IS NULL"), {"id": new_id})

        # The credentials now live on the page row: don't keep a second copy of the
        # token lying around in app_settings.
        conn.execute(text(
            "UPDATE app_settings SET value = '' "
            "WHERE key IN ('fb_page_id', 'fb_page_access_token', 'fb_page_name', 'fb_page_picture')"
        ))
        conn.execute(text(
            "UPDATE app_settings SET value = 'Dipindahkan ke manajemen Fanspage' WHERE key = 'fb_token_status'"
        ))

        logger.info(f"[Migration] Moved single-page config into facebook_pages (id={new_id}).")
        return settings.get("fb_page_name") or page_id


def run_migrations(engine) -> list:
    """
    Adds any missing column. Safe to call on every startup.
    Returns the list of columns that were actually added.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    applied = []

    with engine.begin() as conn:
        for table, column, ddl in ADDED_COLUMNS:
            if table not in existing_tables:
                continue  # create_all() will build it with the full schema
            columns = {c["name"] for c in inspector.get_columns(table)}
            if column in columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")
            logger.info(f"[Migration] Added column {table}.{column}")

        # Backfill rows that existed before these columns were introduced.
        if "content_topics" in existing_tables and applied:
            conn.execute(text(
                "UPDATE content_topics SET source = 'seed' WHERE source IS NULL"
            ))
            conn.execute(text(
                "UPDATE content_topics SET is_active = 1 WHERE is_active IS NULL"
            ))
            # A base topic is its own base, so grouping by base_topic_id is uniform.
            conn.execute(text(
                "UPDATE content_topics SET base_topic_id = id "
                "WHERE source = 'seed' AND base_topic_id IS NULL"
            ))
            # Existing variants inherit the base of the topic they came from.
            conn.execute(text(
                "UPDATE content_topics SET base_topic_id = parent_topic_id "
                "WHERE source = 'ai' AND base_topic_id IS NULL AND parent_topic_id IS NOT NULL"
            ))

        # Poster ratio standard: 4:5 (Facebook + Instagram) or 1:1. Pages still on
        # a retired ratio (3:4 before October 2026) move to 4:5 for their next posts.
        if "facebook_pages" in existing_tables:
            allowed = ", ".join(f"'{r}'" for r in POSTER_RATIOS)
            moved = conn.execute(text(
                f"UPDATE facebook_pages SET aspect_ratio = :std "
                f"WHERE aspect_ratio IS NULL OR aspect_ratio NOT IN ({allowed})"
            ), {"std": DEFAULT_ASPECT_RATIO}).rowcount
            if moved:
                logger.info(f"[Migration] {moved} Fanspage(s) switched to the {DEFAULT_ASPECT_RATIO} poster ratio.")

    return applied
