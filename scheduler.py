import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from apscheduler.events import EVENT_SCHEDULER_SHUTDOWN
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from config import (
    normalize_aspect_ratio,
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
    optimize_topic_weights,
    mark_topic_used,
    learning_phase,
    focus_winners,
)
from core.topic_evolution import evolve_topics
from core.maintenance import remove_generated_image, cleanup_orphan_images
from core.ai_provider import ai_backend
from core.comment_reply import process_page_comments
from core.promo_comment import process_page_promos
from core.instagram import process_page_instagram
from core.gemini_client import generate_post_content
from core.imagen_client import generate_poster_image
from core.fb_client import publish_photo_to_page
from core.pages import verify_page_credentials
from core.notifier import notify
from core.backup import create_backup, latest_backup_age_hours

logger = logging.getLogger(__name__)

try:
    SCHEDULER_TZ = ZoneInfo(SCHEDULER_TIMEZONE)
except Exception:
    logger.warning(f"Unknown timezone '{SCHEDULER_TIMEZONE}', falling back to UTC.")
    SCHEDULER_TZ = timezone.utc

scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)

AUTOPOST_JOB_PREFIX = "autopost_"
# A posting slot may still run this late (also the cron jobs' misfire_grace_time).
AUTOPOST_GRACE_SECONDS = 3600
COMMENT_REPLY_INTERVAL_MINUTES = 10
# One failed comment-reply run is usually a passing Facebook or AI hiccup that the
# next run (10 minutes later) gets past. Only mail when it keeps failing this long.
REPLY_ALERT_AFTER_MINUTES = 30
# page id (or "job") -> when its current streak of failed runs began
_reply_failing_since: dict = {}

def get_setting_val(db, key, default=""):
    s = db.query(AppSetting).filter(AppSetting.key == key).first()
    return s.value if s and s.value else default

def _set_setting(db, key, value: str):
    row = db.query(AppSetting).filter(AppSetting.key == key).first() or AppSetting(key=key)
    row.value = value
    db.add(row)
    db.commit()

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
    page = None
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
            notify(db, f"no-token:{page.id}", f"Autopilot '{page.name}' berhenti: token kosong",
                   f"Jadwal posting Fanspage '{page.name}' dilewati karena Access Token-nya kosong.\n"
                   "Isi ulang token di tab Fanspage, lalu klik Verifikasi.")
            return

        text_ai, image_ai = ai_backend(db, "text"), ai_backend(db, "image")
        for backend in (text_ai, image_ai):
            if backend["missing"]:
                logger.warning(f"[Scheduler] {backend['label']} API key missing. Skipping auto-post.")
                notify(db, f"no-ai-key:{backend['label']}", f"Autopilot berhenti: API key {backend['label']} kosong",
                       f"Jadwal posting '{page.name}' dilewati: {backend['missing']}\n"
                       "Isi API key di tab Pengaturan, lalu klik Uji Koneksi.")
                return

        lang = page.content_language or DEFAULT_CONTENT_LANGUAGE
        aspect_ratio = normalize_aspect_ratio(page.aspect_ratio)

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
                                        model_name=text_ai["model"], provider=text_ai["provider"],
                                        reasoning=text_ai["reasoning"])

        # 3. Render the poster
        filename, abspath = generate_poster_image(
            api_key=image_ai["api_key"],
            prompt=content["imagen_prompt"],
            aspect_ratio=aspect_ratio,
            model_name=image_ai["model"],
            provider=image_ai["provider"],
            quality=image_ai["quality"],
            theme=page.color_theme if page else None,
            watermark=page.name if page else None,
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
            notify(db, f"publish:{page.id}", f"Gagal posting ke '{page.name}'",
                   f"Konten '{post.visual_title}' sudah dibuat, tetapi Facebook menolaknya.\n\n"
                   f"Pesan Facebook: {fb_res.get('message')}\n\n"
                   "Draft tersimpan dengan status Gagal; buka di Studio untuk mencoba publish lagi. "
                   "Jika pesannya soal token atau izin, perbarui token di tab Fanspage.")

    except Exception as e:
        logger.error(f"[Scheduler] Error during auto-post job for page {page_row_id}: {e}")
        db.rollback()
        # A poster was rendered but never saved: remove it instead of leaking a file.
        if poster_sementara:
            remove_generated_image(poster_sementara)
        page_name = page.name if page else f"#{page_row_id}"
        notify(db, f"autopost:{page_row_id}", f"Autopilot gagal membuat konten untuk '{page_name}'",
               f"Jadwal posting '{page_name}' gagal sebelum sampai ke Facebook.\n\n"
               f"Error: {e}\n\n"
               "Penyebab umum: saldo/kuota API AI habis, API key dicabut, atau layanan AI sedang gangguan. "
               "Cek tab Pengaturan > Uji Koneksi.")
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
            # Doubles as the daily token health check: a revoked or expired token
            # would otherwise only surface when the next post fails.
            check = verify_page_credentials(page.page_id, page.access_token)
            if not check.get("success"):
                logger.warning(f"[Scheduler] '{page.name}' token check failed: {check.get('message')}")
                notify(db, f"token:{page.id}", f"Token Facebook '{page.name}' bermasalah",
                       f"Pemeriksaan harian token Fanspage '{page.name}' gagal.\n\n"
                       f"Pesan Facebook: {check.get('message')}\n\n"
                       "Selama token ini bermasalah, posting otomatis ke Fanspage ini akan gagal. "
                       "Buat token baru lalu tempel di tab Fanspage dan klik Verifikasi. "
                       "(Token sering tidak berlaku setelah password Facebook diganti.)",
                       cooldown_hours=20)
                continue
            logger.info(f"[Scheduler] Syncing metrics for '{page.name}'...")
            update_all_post_metrics(db, page.page_id, page.access_token, page.id)

        window_days = _int_setting(db, "topic_window_days", 7, 1, 90)
        res = optimize_all_pages(db, window_days)
        for entry in res.get("pages", []):
            logger.info(f"[Scheduler] '{entry['page_name']}' top topic: {entry['winning_topic']}")

        stock_focus_variants(db)
        _set_setting(db, LAST_METRICS_SYNC_KEY, datetime.now(timezone.utc).isoformat())

        # Sweep poster files left behind by failed generations.
        cleanup_orphan_images(db)
    except Exception as e:
        logger.error(f"[Scheduler] Error during metrics sync: {e}")
        db.rollback()
        notify(db, "metrics-sync", "Sinkron metrik malam gagal",
               f"Sinkron metrik & pembelajaran topik malam ini gagal.\n\nError: {e}", cooldown_hours=20)
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
                _reply_failing_since.pop(page.id, None)
                if res.get("replied") or res.get("drafted"):
                    logger.info(f"[Scheduler] '{page.name}' comments: {res.get('message')}")
            else:
                logger.warning(f"[Scheduler] '{page.name}' comment reply skipped: {res.get('message')}")
                minutes = _reply_failure_minutes(page.id)
                if minutes is not None:
                    notify(db, f"reply:{page.id}", f"Balas komentar otomatis '{page.name}' terhenti",
                           f"Balasan komentar otomatis untuk '{page.name}' gagal terus selama "
                           f"{minutes} menit terakhir.\n\nPesan: {res.get('message')}", cooldown_hours=12)
        _reply_failing_since.pop("job", None)
    except Exception as e:
        logger.error(f"[Scheduler] Error during comment reply job: {e}")
        db.rollback()
        minutes = _reply_failure_minutes("job")
        if minutes is not None:
            notify(db, "reply-job", "Balas komentar otomatis error",
                   f"Pemeriksaan komentar error terus selama {minutes} menit terakhir.\n\nError: {e}",
                   cooldown_hours=12)
    finally:
        db.close()


PROMO_INTERVAL_MINUTES = 5


def promo_comment_job():
    """
    Every few minutes: posts the page's promo first comment under its posts that
    went live a couple of minutes ago (see core/promo_comment.py).
    """
    db = SessionLocal()
    try:
        pages = (db.query(FacebookPage)
                   .filter(FacebookPage.is_active.isnot(False), FacebookPage.promo_enabled.is_(True))
                   .all())
        for page in pages:
            process_page_promos(db, page)
    except Exception as e:
        logger.error(f"[Scheduler] Error during promo comment job: {e}")
        db.rollback()
    finally:
        db.close()


INSTAGRAM_INTERVAL_MINUTES = 5


def instagram_job():
    """
    Every few minutes: publishes each Instagram-enabled page's newly published
    posts to its linked Instagram account (see core/instagram.py).
    """
    db = SessionLocal()
    try:
        pages = (db.query(FacebookPage)
                   .filter(FacebookPage.is_active.isnot(False), FacebookPage.ig_enabled.is_(True))
                   .all())
        for page in pages:
            res = process_page_instagram(db, page)
            if res.get("message") and not res.get("published"):
                notify(db, f"instagram:{page.id}", f"Posting Instagram '{page.name}' gagal",
                       f"Postingan Fanspage '{page.name}' tidak bisa dipublikasikan ke Instagram.\n\n"
                       f"Pesan: {res['message']}", cooldown_hours=12)
    except Exception as e:
        logger.error(f"[Scheduler] Error during Instagram job: {e}")
        db.rollback()
    finally:
        db.close()


def _reply_failure_minutes(key, now: datetime | None = None) -> int | None:
    """
    Records a failed run for `key`. Returns how long it has been failing once that
    reaches REPLY_ALERT_AFTER_MINUTES (time to alert), else None.
    """
    now = now or datetime.now(timezone.utc)
    since = _reply_failing_since.setdefault(key, now)
    minutes = int((now - since).total_seconds() // 60)
    return minutes if minutes >= REPLY_ALERT_AFTER_MINUTES else None


def stock_focus_variants(db) -> dict:
    """
    Keeps every winner in each page's focus rotation supplied with fresh variants.

    For a page that has tested and measured every base topic, any of its top
    winners with no unposted variant left gets new close variants grown from it,
    so the rotation #1 -> #2 -> #3 always has "something like the winner" to post.
    The first run after a page finishes its test phase is what grows its very
    first variants. Returns {page_id: [titles created]}.
    """
    if get_setting_val(db, "auto_topic_evolution", "true").lower() != "true":
        logger.info("[Scheduler] Topic evolution disabled. Skipping variant stocking.")
        return {}
    text_ai = ai_backend(db, "text")
    if text_ai["missing"]:
        logger.warning(f"[Scheduler] No {text_ai['label']} key. Skipping variant stocking.")
        return {}
    window_days = _int_setting(db, "topic_window_days", 7, 1, 90)
    max_new = _int_setting(db, "max_new_topics_per_cycle", 2, 1, 5)

    created = {}
    for page in db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all():
        phase = learning_phase(db, page.id)
        if not phase["ready_for_variants"]:
            logger.info(f"[Scheduler] '{page.name}' not ready for variants: {phase['phase']} phase, "
                        f"{phase['tested']}/{phase['total']} tested, {phase['pending']} awaiting metrics.")
            continue
        winners = focus_winners(db, page.id)
        leaders = [w["leader"].id for w in winners]
        for rank, winner in enumerate(winners, start=1):
            if winner["fresh_variants"]:
                continue
            others = [i for i in leaders if i != winner["leader"].id]
            res = evolve_topics(
                db=db,
                api_key=text_ai["api_key"],
                model_name=text_ai["model"],
                provider=text_ai["provider"],
                reasoning=text_ai["reasoning"],
                window_days=window_days,
                max_new=max_new,
                language=page.content_language or DEFAULT_CONTENT_LANGUAGE,
                page_id=page.id,
                winner_ids=[winner["leader"].id] + others,
            )
            if res.get("success"):
                titles = [t["title"] for t in res.get("created", [])]
                created.setdefault(page.id, []).extend(titles)
                logger.info(f"[Scheduler] '{page.name}' winner #{rank} '{winner['leader'].title}' "
                            f"got variants: {', '.join(titles)}")
            else:
                logger.info(f"[Scheduler] '{page.name}' winner #{rank} variants skipped: {res.get('message')}")
        if page.id in created:
            optimize_topic_weights(db, window_days, page.id)   # variants take their winner's weight
    return created


def topic_evolution_job():
    """
    Weekly safety net for the nightly stocking: makes sure every page in its focus
    phase has fresh variants of each of its top winners, then refreshes weights.
    """
    db = SessionLocal()
    try:
        stock_focus_variants(db)
        optimize_all_pages(db, _int_setting(db, "topic_window_days", 7, 1, 90))
    except Exception as e:
        logger.error(f"[Scheduler] Error during topic evolution: {e}")
        db.rollback()
        notify(db, "topic-evolution", "Evolusi topik mingguan gagal", f"Error: {e}", cooldown_hours=24)
    finally:
        db.close()


def backup_job():
    """Nightly database snapshot (see core/backup.py); mails an alert if it fails."""
    try:
        create_backup()
    except Exception as e:
        logger.error(f"[Scheduler] Database backup failed: {e}")
        db = SessionLocal()
        try:
            notify(db, "backup", "Backup database gagal",
                   f"Backup database malam ini gagal.\n\nError: {e}\n\n"
                   "Data aplikasi masih utuh, tetapi belum ada salinan cadangan terbaru.",
                   cooldown_hours=20)
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


# Jobs live in memory and are rebuilt from "now" at startup, so whatever fell due
# while the app was down (a VPS reboot, a redeploy) would silently never happen.
# Shortly after startup, catch_up_job runs what was missed.
CATCH_UP_DELAY_SECONDS = 60
CATCH_UP_POST_HOURS = 3          # a post slot missed longer ago than this is skipped
METRICS_STALE_HOURS = 26         # the nightly sync is overdue past this
BACKUP_STALE_HOURS = 26          # likewise for the nightly backup
LAST_METRICS_SYNC_KEY = "last_metrics_sync_at"
STARTED_AT: datetime | None = None


def _latest_slot_before(moment: datetime, times: list) -> datetime | None:
    """The most recent posting slot at or before `moment` (today or yesterday)."""
    slots = []
    for hour, minute in times:
        for days_back in (0, 1):
            slot = (moment - timedelta(days=days_back)).replace(hour=hour, minute=minute,
                                                                 second=0, microsecond=0)
            if slot <= moment:
                slots.append(slot)
    return max(slots) if slots else None


def missed_post_pages(db, started_at: datetime, now: datetime | None = None) -> list:
    """
    Autopilot pages whose latest slot fell while the app was down: before this
    process started, within CATCH_UP_POST_HOURS, and with no post created for the
    page since. Only the latest slot is caught up, so a long outage never fires a
    burst of posts; slots after startup belong to the live scheduler.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(SCHEDULER_TZ)
    started = started_at.astimezone(SCHEDULER_TZ)
    due = []
    pages = db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False),
                                          FacebookPage.autopilot_enabled.is_(True)).all()
    for page in pages:
        if not page.access_token:
            continue
        slot = _latest_slot_before(started, parse_post_times(page.auto_post_times))
        if slot is None or now - slot > timedelta(hours=CATCH_UP_POST_HOURS):
            continue
        slot_utc = slot.astimezone(timezone.utc).replace(tzinfo=None)
        produced = db.query(Post.id).filter(Post.page_id == page.id, Post.created_at >= slot_utc).first()
        if not produced:
            due.append(page.id)
    return due


def metrics_sync_overdue(db, now: datetime | None = None) -> bool:
    raw = get_setting_val(db, LAST_METRICS_SYNC_KEY)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last > timedelta(hours=METRICS_STALE_HOURS)


def catch_up_job():
    """Runs, once after startup, the metric sync and post slots missed during downtime."""
    db = SessionLocal()
    try:
        overdue = metrics_sync_overdue(db)
        pages_due = missed_post_pages(db, STARTED_AT or datetime.now(timezone.utc))
    finally:
        db.close()

    backup_age = latest_backup_age_hours()
    if backup_age is None or backup_age > BACKUP_STALE_HOURS:
        logger.info("[Scheduler] Catch-up: no recent database backup. Making one now.")
        backup_job()
    if overdue:
        logger.info("[Scheduler] Catch-up: nightly metric sync was missed. Running it now.")
        metrics_sync_job()
    for page_row_id in pages_due:
        logger.info(f"[Scheduler] Catch-up: page {page_row_id} missed its posting slot while offline.")
        auto_generate_and_post_job(page_row_id)


def start_scheduler():
    global STARTED_AT
    if scheduler.running:
        return

    STARTED_AT = datetime.now(timezone.utc)
    reload_autopost_schedule()

    scheduler.add_job(
        catch_up_job,
        DateTrigger(run_date=STARTED_AT + timedelta(seconds=CATCH_UP_DELAY_SECONDS)),
        id='catch_up_job',
        replace_existing=True,
        misfire_grace_time=600,
    )

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

    # Nightly database backup at 03:30, after the metric sync has written its results.
    scheduler.add_job(
        backup_job,
        CronTrigger(hour=3, minute=30, timezone=SCHEDULER_TZ),
        id='backup_job',
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
    # First-comment promotion under freshly published posts.
    scheduler.add_job(
        promo_comment_job,
        IntervalTrigger(minutes=PROMO_INTERVAL_MINUTES, timezone=SCHEDULER_TZ),
        id='promo_comment_job',
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    # Instagram cross-posting of freshly published posts.
    scheduler.add_job(
        instagram_job,
        IntervalTrigger(minutes=INSTAGRAM_INTERVAL_MINUTES, timezone=SCHEDULER_TZ),
        id='instagram_job',
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _start_watchdog()
    logger.info("Background scheduler started successfully.")


# ------------------------------------------------------------------ watchdog
# If the scheduler thread dies, the dashboard keeps answering while autopilot,
# comment replies, metrics and backups have silently stopped. Docker only
# restarts a container whose process exits, so after a few failed checks the
# watchdog exits the process and the restart policy brings everything back.
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_STRIKES = 3
# The 10-minute comment-reply job is the heartbeat: its next run is never this
# far in the past while the scheduler thread is alive.
HEARTBEAT_STALE_MINUTES = 30
_watchdog_stop = threading.Event()


def scheduler_health(now: datetime | None = None) -> tuple[bool, str]:
    """(healthy, reason) for /healthz and the watchdog."""
    if not scheduler.running:
        return False, "scheduler not running"
    job = scheduler.get_job("comment_reply_job")
    if job is None or job.next_run_time is None:
        return True, "ok"
    now = now or datetime.now(timezone.utc)
    if now - job.next_run_time > timedelta(minutes=HEARTBEAT_STALE_MINUTES):
        return False, f"scheduler stalled since {job.next_run_time.isoformat()}"
    return True, "ok"


def _watchdog_loop():
    strikes = 0
    while not _watchdog_stop.wait(WATCHDOG_INTERVAL_SECONDS):
        healthy, reason = scheduler_health()
        strikes = 0 if healthy else strikes + 1
        if strikes:
            logger.error(f"[Watchdog] {reason} (check {strikes}/{WATCHDOG_STRIKES}).")
        if strikes >= WATCHDOG_STRIKES and not _watchdog_stop.is_set():
            logger.critical("[Watchdog] Background jobs stopped; exiting so Docker restarts the app.")
            for handler in logging.getLogger().handlers:
                handler.flush()
            time.sleep(1)
            os._exit(1)


def _start_watchdog():
    _watchdog_stop.clear()
    # A deliberate shutdown (app stopping, test teardown) is not a failure.
    scheduler.add_listener(lambda event: _watchdog_stop.set(), EVENT_SCHEDULER_SHUTDOWN)
    threading.Thread(target=_watchdog_loop, name="scheduler-watchdog", daemon=True).start()
