import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from config import (
    SCHEDULER_TIMEZONE,
    DEFAULT_SETTINGS,
    DEFAULT_CONTENT_LANGUAGE,
)
from database.db_session import SessionLocal
from database.models import AppSetting, Post, FacebookPage
from core.feedback_loop import (
    get_next_recommended_topic,
    update_all_post_metrics,
    optimize_all_pages,
    mark_topic_used,
)
from core.topic_evolution import evolve_topics
from core.maintenance import remove_generated_image, cleanup_orphan_images
from core.ai_provider import ai_backend
from core.comment_reply import process_page_comments
from core.gemini_client import generate_post_content
from core.imagen_client import generate_poster_image
from core.fb_client import publish_photo_to_page

logger = logging.getLogger(__name__)

try:
    SCHEDULER_TZ = ZoneInfo(SCHEDULER_TIMEZONE)
except Exception:
    logger.warning(f"Unknown timezone '{SCHEDULER_TIMEZONE}', falling back to UTC.")
    SCHEDULER_TZ = timezone.utc

scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)

AUTOPOST_JOB_PREFIX = "autopost_"
CATCHUP_JOB_PREFIX = "catchup_"
# A posting slot may still run this late (also the cron jobs' misfire_grace_time).
AUTOPOST_GRACE_SECONDS = 3600
# Delay before a catch-up post after startup, so a restart loop cannot burst.
CATCHUP_DELAY_SECONDS = 60
COMMENT_REPLY_INTERVAL_MINUTES = 10

def get_setting_val(db, key, default=""):
    s = db.query(AppSetting).filter(AppSetting.key == key).first()
    return s.value if s and s.value else default

def _int_setting(db, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(get_setting_val(db, key, str(default)))))
    except (TypeError, ValueError):
        return default

def parse_post_times(raw: str) -> list[tuple[int, int]]:
    """
    Parses "10:00,19:00" into [(10, 0), (19, 0)], skipping malformed entries.
    """
    times = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hour_str, minute_str = chunk.split(":")
            hour, minute = int(hour_str), int(minute_str)
        except ValueError:
            logger.warning(f"[Scheduler] Invalid time entry '{chunk}' in auto_post_times.")
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            times.append((hour, minute))
        else:
            logger.warning(f"[Scheduler] Out-of-range time '{chunk}' in auto_post_times.")
    return times


def auto_generate_and_post_job(page_row_id: int):
    """
    Scheduled job for ONE Fanspage: picks that page's best topic, generates its own
    poster & caption, and publishes it there. Every page produces its own content.
    """
    db = SessionLocal()
    poster_sementara = None
    try:
        page = db.query(FacebookPage).filter(FacebookPage.id == page_row_id).first()
        if not page:
            logger.warning(f"[Scheduler] Page {page_row_id} no longer exists. Skipping.")
            return
        if page.is_active is False or not page.autopilot_enabled:
            logger.info(f"[Scheduler] Autopilot off for '{page.name}'. Skipping.")
            return
        if not page.access_token:
            logger.warning(f"[Scheduler] '{page.name}' has no access token. Skipping.")
            return

        text_ai, image_ai = ai_backend(db, "text"), ai_backend(db, "image")
        for backend in (text_ai, image_ai):
            if backend["missing"]:
                logger.warning(f"[Scheduler] {backend['label']} API key missing. Skipping auto-post.")
                return

        lang = page.content_language or DEFAULT_CONTENT_LANGUAGE
        aspect_ratio = page.aspect_ratio or "3:4"

        logger.info(f"[Scheduler] Autonomous content cycle for '{page.name}'...")

        # 1. Pick a topic using THIS page's learned weights
        topic = get_next_recommended_topic(db, page.id)
        topic_dict = {
            "title": topic.title,
            "category": topic.category,
            "core_concept": topic.core_concept,
            "visual_blueprint": topic.visual_blueprint,
        }

        # 2. Generate copy & prompt
        content = generate_post_content(text_ai["api_key"], topic_dict, language=lang,
                                        model_name=text_ai["model"], provider=text_ai["provider"])

        # 3. Render the poster
        filename, abspath = generate_poster_image(
            api_key=image_ai["api_key"],
            prompt=content["imagen_prompt"],
            aspect_ratio=aspect_ratio,
            model_name=image_ai["model"],
            provider=image_ai["provider"],
        )
        poster_sementara = abspath

        # 4. Record the post as "publishing" BEFORE uploading. If the app is stopped
        # mid-upload (restart, redeploy), startup recovery flags this row as failed
        # with a "check the page first" warning, instead of a post that may already
        # be live on Facebook leaving no trace in the dashboard.
        post = Post(
            page_id=page.id,
            topic_id=topic.id,
            topic_title=topic.title,
            language=lang,
            visual_title=content.get("visual_title", topic.title),
            prompt_used=content["imagen_prompt"],
            image_filename=filename,
            image_path=abspath,
            caption=content["caption"],
            status="publishing",
        )
        db.add(post)
        mark_topic_used(db, topic, page.id)
        db.commit()
        poster_sementara = None   # the row now owns the file

        # 5. Publish to this page
        try:
            fb_res = publish_photo_to_page(
                page_id=page.page_id,
                access_token=page.access_token,
                image_path=abspath,
                caption=content["caption"],
            )
        except Exception as e:
            fb_res = {"success": False, "message": f"Koneksi ke Facebook gagal: {e}"}

        if fb_res.get("success"):
            post.status = "published"
            post.fb_post_id = fb_res.get("post_id")
            post.fb_post_url = fb_res.get("post_url")
            post.published_at = datetime.now(timezone.utc)
        else:
            post.status = "failed"
            post.error_message = fb_res.get("message")
        db.commit()

        if fb_res.get("success"):
            logger.info(f"[Scheduler] '{page.name}' published: {fb_res.get('post_url')}")
        else:
            logger.error(f"[Scheduler] '{page.name}' publish failed: {fb_res.get('message')}")

    except Exception as e:
        logger.error(f"[Scheduler] Error during auto-post job for page {page_row_id}: {e}")
        db.rollback()
        # A poster was rendered but never saved: remove it instead of leaking a file.
        if poster_sementara:
            remove_generated_image(poster_sementara)
    finally:
        db.close()


def metrics_sync_job():
    """
    Daily: refreshes metrics for every page using its own token, then re-runs the
    learning cycle per page.
    """
    db = SessionLocal()
    try:
        pages = db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all()
        for page in pages:
            if not page.access_token:
                continue
            logger.info(f"[Scheduler] Syncing metrics for '{page.name}'...")
            update_all_post_metrics(db, page.page_id, page.access_token, page.id)

        window_days = _int_setting(db, "topic_window_days", 7, 1, 90)
        res = optimize_all_pages(db, window_days)
        for entry in res.get("pages", []):
            logger.info(f"[Scheduler] '{entry['page_name']}' top topic: {entry['winning_topic']}")

        # Sweep poster files left behind by failed generations.
        cleanup_orphan_images(db)
    except Exception as e:
        logger.error(f"[Scheduler] Error during metrics sync: {e}")
        db.rollback()
    finally:
        db.close()


def comment_reply_job():
    """
    Every few minutes: answers new comments on every page that has auto-reply on.
    Replies inside one run are spaced out so the page doesn't look automated.
    """
    db = SessionLocal()
    try:
        pages = (db.query(FacebookPage)
                   .filter(FacebookPage.is_active.isnot(False), FacebookPage.auto_reply_enabled.is_(True))
                   .all())
        for page in pages:
            if not page.access_token:
                continue
            res = process_page_comments(db, page, gap=True)
            if res.get("success"):
                if res.get("replied") or res.get("drafted"):
                    logger.info(f"[Scheduler] '{page.name}' comments: {res.get('message')}")
            else:
                logger.warning(f"[Scheduler] '{page.name}' comment reply skipped: {res.get('message')}")
    except Exception as e:
        logger.error(f"[Scheduler] Error during comment reply job: {e}")
        db.rollback()
    finally:
        db.close()


def topic_evolution_job():
    """
    Weekly: every page grows new topics from ITS OWN winners. New topics land in
    the shared catalog, so a discovery on one page can benefit the others.
    """
    db = SessionLocal()
    try:
        if get_setting_val(db, "auto_topic_evolution", "true").lower() != "true":
            logger.info("[Scheduler] Topic evolution disabled. Skipping.")
            return

        text_ai = ai_backend(db, "text")
        if text_ai["missing"]:
            logger.warning(f"[Scheduler] No {text_ai['label']} key. Skipping topic evolution.")
            return

        window_days = _int_setting(db, "topic_window_days", 7, 1, 90)
        max_new = _int_setting(db, "max_new_topics_per_cycle", 2, 1, 5)

        # Metrics were already refreshed by the 03:00 sync job an hour ago, so this
        # job reads them instead of hitting the Graph API for every post again.
        pages = db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all()
        for page in pages:
            res = evolve_topics(
                db=db,
                api_key=text_ai["api_key"],
                model_name=text_ai["model"],
                provider=text_ai["provider"],
                window_days=window_days,
                max_new=max_new,
                language=page.content_language or DEFAULT_CONTENT_LANGUAGE,
                page_id=page.id,
            )
            if res.get("success"):
                titles = ", ".join(t["title"] for t in res.get("created", []))
                logger.info(f"[Scheduler] '{page.name}' grew new topics: {titles}")
            else:
                logger.info(f"[Scheduler] '{page.name}' evolution skipped: {res.get('message')}")

        optimize_all_pages(db, window_days)
    except Exception as e:
        logger.error(f"[Scheduler] Error during topic evolution: {e}")
        db.rollback()
    finally:
        db.close()


# Page edits arrive on several request threads at once; two interleaved rebuilds
# raced on remove_job (JobLookupError) and could leave an outdated schedule.
_RELOAD_LOCK = threading.Lock()


def reload_autopost_schedule():
    """
    Rebuilds one set of cron jobs per Fanspage from each page's own posting times.
    Safe to call at startup and whenever a page is added or edited.
    """
    with _RELOAD_LOCK:
        return _reload_autopost_schedule()


def _reload_autopost_schedule():
    db = SessionLocal()
    try:
        pages = db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all()
        plan = [
            (
                p.id,
                p.name,
                parse_post_times(p.auto_post_times) or parse_post_times(DEFAULT_SETTINGS["auto_post_times"]),
                # A page without a token cannot post: scheduling it would show an
                # "Aktif — berikutnya 10:00" status for a job that always aborts.
                bool(p.autopilot_enabled) and bool(p.access_token),
            )
            for p in pages
        ]
    finally:
        db.close()

    # Drop previous auto-post jobs before re-adding them.
    for job in scheduler.get_jobs():
        if job.id.startswith(AUTOPOST_JOB_PREFIX):
            scheduler.remove_job(job.id)

    scheduled = []
    for page_row_id, page_name, times, enabled in plan:
        if not enabled:
            continue
        for hour, minute in times:
            scheduler.add_job(
                auto_generate_and_post_job,
                CronTrigger(hour=hour, minute=minute, timezone=SCHEDULER_TZ),
                args=[page_row_id],
                id=f"{AUTOPOST_JOB_PREFIX}{page_row_id}_{hour:02d}{minute:02d}",
                replace_existing=True,
                misfire_grace_time=AUTOPOST_GRACE_SECONDS,
                coalesce=True,
                max_instances=1,
            )
        scheduled.append(f"{page_name} ({', '.join(f'{h:02d}:{m:02d}' for h, m in times)})")

    if scheduled:
        logger.info(f"[Scheduler] Auto-post schedule: {' | '.join(scheduled)} ({SCHEDULER_TIMEZONE}).")
    else:
        logger.info("[Scheduler] No Fanspage has autopilot enabled.")
    return scheduled


def schedule_missed_autoposts(now: datetime | None = None) -> list[int]:
    """
    The cron jobs live in memory, so a slot that passed while the app was down
    (restart, redeploy, server reboot) would be skipped until the next one. At
    startup, run each page's most recent slot once if it passed within the grace
    window and the page has produced no post since.
    """
    now = (now or datetime.now(SCHEDULER_TZ)).astimezone(SCHEDULER_TZ)
    db = SessionLocal()
    queued = []
    try:
        pages = db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all()
        for page in pages:
            if not (page.autopilot_enabled and page.access_token):
                continue
            times = parse_post_times(page.auto_post_times) or parse_post_times(DEFAULT_SETTINGS["auto_post_times"])
            missed = None
            for hour, minute in times:
                slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if slot > now:
                    slot -= timedelta(days=1)
                if (now - slot).total_seconds() <= AUTOPOST_GRACE_SECONDS and (missed is None or slot > missed):
                    missed = slot
            if missed is None:
                continue
            # Timestamps are stored as naive UTC.
            missed_utc = missed.astimezone(timezone.utc).replace(tzinfo=None)
            if page.created_at and page.created_at > missed_utc:
                continue   # the page did not exist yet at that slot
            if db.query(Post).filter(Post.page_id == page.id, Post.created_at >= missed_utc).first():
                continue   # the slot did run
            scheduler.add_job(
                auto_generate_and_post_job,
                "date",
                run_date=now + timedelta(seconds=CATCHUP_DELAY_SECONDS),
                args=[page.id],
                id=f"{CATCHUP_JOB_PREFIX}{page.id}",
                replace_existing=True,
                misfire_grace_time=AUTOPOST_GRACE_SECONDS,
            )
            queued.append(page.id)
            logger.warning(f"[Scheduler] '{page.name}' missed its {missed:%H:%M} post while the app was down; "
                           f"posting it in {CATCHUP_DELAY_SECONDS}s.")
    finally:
        db.close()
    return queued


def get_schedule_status(enabled: bool = True) -> dict:
    """
    Reports what the autopilot will do next, per page, so the UI can show exact
    times instead of a vague "tiap interval".
    """
    jobs = [j for j in scheduler.get_jobs() if j.id.startswith(AUTOPOST_JOB_PREFIX)]

    per_page = {}
    for job in jobs:
        page_row_id = job.args[0] if job.args else None
        fields = {f.name: str(f) for f in job.trigger.fields}
        entry = per_page.setdefault(page_row_id, {"page_id": page_row_id, "times": [], "next_run": None})
        entry["times"].append(f"{int(fields['hour']):02d}:{int(fields['minute']):02d}")
        if job.next_run_time and (entry["next_run"] is None or job.next_run_time.isoformat() < entry["next_run"]):
            entry["next_run"] = job.next_run_time.isoformat()

    for entry in per_page.values():
        entry["times"].sort()

    next_runs = sorted(e["next_run"] for e in per_page.values() if e["next_run"])
    evolution_job = scheduler.get_job('topic_evolution_job')
    evolution_next = evolution_job.next_run_time if evolution_job else None
    reply_job = scheduler.get_job('comment_reply_job')
    reply_next = reply_job.next_run_time if reply_job else None

    return {
        "enabled": bool(per_page),
        "timezone": SCHEDULER_TIMEZONE,
        "pages": list(per_page.values()),
        "next_run": next_runs[0] if next_runs else None,
        "next_evolution": evolution_next.isoformat() if evolution_next else None,
        "next_reply_scan": reply_next.isoformat() if reply_next else None,
        "reply_interval_minutes": COMMENT_REPLY_INTERVAL_MINUTES,
    }


def start_scheduler():
    if scheduler.running:
        return

    reload_autopost_schedule()

    # Daily metric sync at 03:00 local time, well after the last post of the day.
    scheduler.add_job(
        metrics_sync_job,
        CronTrigger(hour=3, minute=0, timezone=SCHEDULER_TZ),
        id='feedback_job',
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )

    # Weekly topic evolution: Monday 04:00, right after the nightly metric sync.
    scheduler.add_job(
        topic_evolution_job,
        CronTrigger(day_of_week='mon', hour=4, minute=0, timezone=SCHEDULER_TZ),
        id='topic_evolution_job',
        replace_existing=True,
        misfire_grace_time=7200,
        coalesce=True,
        max_instances=1,
    )
    # Comment auto-reply: checks pages with auto-reply on every few minutes.
    scheduler.add_job(
        comment_reply_job,
        IntervalTrigger(minutes=COMMENT_REPLY_INTERVAL_MINUTES, timezone=SCHEDULER_TZ),
        id='comment_reply_job',
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    schedule_missed_autoposts()
    logger.info("Background scheduler started successfully.")
