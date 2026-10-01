import hashlib
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional, Dict, Any
from sqlalchemy import or_
from sqlalchemy.orm import Session
from datetime import datetime, timezone

from config import (
    BASE_DIR,
    STORAGE_DIR,
    IMAGES_DIR,
    DEFAULT_SETTINGS,
    DEFAULT_TEXT_MODEL,
    DEFAULT_OPENAI_TEXT_MODEL,
    DEFAULT_OPENAI_IMAGE_MODEL,
    SENSITIVE_SETTING_KEYS,
    SECRET_MASK,
    DEFAULT_CONTENT_LANGUAGE,
)
from database.db_session import engine, Base, get_db, SessionLocal
from database.migrations import run_migrations, migrate_single_page_to_multi
from database.models import AppSetting, ContentTopic, Post, FacebookPage, PageTopicWeight, CommentReply
from core.taxonomy import seed_base_curriculum
from core.utils import iso_utc
from core.maintenance import remove_generated_image, cleanup_orphan_images
from core.gemini_client import test_gemini_key, generate_post_content, template_content
from core.openai_client import test_openai_key
from core.ai_provider import ai_backend, PROVIDER_LABELS
from core.imagen_client import generate_poster_image
from core.fb_client import publish_photo_to_page
from core.feedback_loop import (
    get_next_recommended_topic,
    update_all_post_metrics,
    optimize_topic_weights,
    optimize_all_pages,
    window_performance,
    page_topic_weights,
    mark_topic_used,
    topics_for_page,
    learning_phase,
    focus_winners,
)
from core.topic_evolution import evolve_topics, retire_topic, reactivate_topic
from core import comment_reply
from core import auth
from core.pages import (
    list_pages,
    get_page,
    default_page,
    add_page,
    update_page,
    delete_page,
    reverify_page,
    verify_page_credentials,
)
from scheduler import (
    start_scheduler,
    reload_autopost_schedule,
    parse_post_times,
    get_schedule_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AutoPosterApp")

def bootstrap_first_user(db: Session):
    """
    First deploy (e.g. Dokploy) without shell access: AUTOPOSTER_ADMIN_EMAIL and
    AUTOPOSTER_ADMIN_PASSWORD create the account, but only while no account exists,
    so a password later changed in the dashboard is never overwritten.
    """
    if auth.has_any_user(db):
        return
    email = os.environ.get("AUTOPOSTER_ADMIN_EMAIL", "").strip()
    password = os.environ.get("AUTOPOSTER_ADMIN_PASSWORD", "")
    if not (email and password):
        logger.warning("Belum ada akun login. Isi AUTOPOSTER_ADMIN_EMAIL dan AUTOPOSTER_ADMIN_PASSWORD, "
                       "atau jalankan: python scripts/atur_login.py <email>")
        return
    try:
        auth.set_user_password(db, email, password)
        logger.info(f"Akun login pertama '{auth.normalize_email(email)}' dibuat dari environment.")
    except ValueError as e:
        logger.error(f"AUTOPOSTER_ADMIN_* tidak valid: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Database Initialization
    logger.info("Initializing database schema...")
    Base.metadata.create_all(bind=engine)
    applied = run_migrations(engine)
    if applied:
        logger.info(f"Applied schema migrations: {', '.join(applied)}")
    moved = migrate_single_page_to_multi(engine)
    if moved:
        logger.info(f"Existing Fanspage '{moved}' moved into multi-page management.")
    db = SessionLocal()
    try:
        # Seed default settings if missing
        for k, v in DEFAULT_SETTINGS.items():
            existing = db.query(AppSetting).filter(AppSetting.key == k).first()
            if not existing:
                db.add(AppSetting(key=k, value=str(v)))
        # Base curriculum: adds any base topic this database does not have yet
        added = seed_base_curriculum(db)
        if added:
            logger.info(f"Added {len(added)} base topic(s) to the curriculum.")
        # A post left in "publishing" means the app stopped mid-upload. Whether
        # Facebook received it is unknown, so flag it instead of retrying blindly.
        stuck = db.query(Post).filter(Post.status == "publishing").all()
        for post in stuck:
            post.status = "failed"
            post.error_message = (
                "Status tidak pasti: aplikasi berhenti saat mempublikasikan. "
                "Periksa Fanspage terlebih dahulu sebelum publish ulang agar tidak tayang ganda."
            )
        if stuck:
            logger.warning(f"{len(stuck)} post(s) were interrupted mid-publish and marked as failed.")
        interrupted = comment_reply.recover_interrupted_replies(db)
        if interrupted:
            logger.warning(f"{interrupted} comment reply(ies) were interrupted mid-send and marked as failed.")
        db.commit()
        bootstrap_first_user(db)
    finally:
        db.close()

    # Start background autonomous scheduler
    start_scheduler()
    yield

app = FastAPI(title="AutoPoster Gold AI", version="2.0", lifespan=lifespan)

# Mount static and storage folders
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/storage", StaticFiles(directory=str(STORAGE_DIR)), name="storage")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ==========================================
# AUTHENTICATION
# ==========================================
# Everything else (pages, API, generated posters) requires a signed-in session.
PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.ico", "/healthz"}

def _session_user(token: str):
    db = SessionLocal()
    try:
        user = auth.user_for_token(db, token)
        return user.email if user else None
    finally:
        db.close()

@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    email = await run_in_threadpool(_session_user, request.cookies.get(auth.SESSION_COOKIE, ""))
    if not email:
        if path.startswith("/api/") or path.startswith("/storage/") or request.method != "GET":
            return JSONResponse({"detail": "Sesi login berakhir. Silakan login kembali."}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    request.state.user_email = email
    return await call_next(request)

def _set_session_cookie(response, request: Request, token: str):
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )

class LoginRequest(BaseModel):
    email: str
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

@app.get("/healthz")
def healthz():
    """Container health check; public and reveals nothing."""
    return {"status": "ok"}

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    if auth.user_for_token(db, request.cookies.get(auth.SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"has_user": auth.has_any_user(db)})

@app.post("/api/auth/login")
def login_endpoint(req: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    locked = auth.login_throttle.seconds_locked(ip, req.email)
    if locked:
        minutes = (locked + 59) // 60
        return JSONResponse({"success": False,
                             "message": f"Terlalu banyak percobaan gagal. Coba lagi dalam {minutes} menit."},
                            status_code=429)
    user = auth.authenticate(db, req.email, req.password)
    if not user:
        auth.login_throttle.record_failure(ip, req.email)
        logger.warning(f"Login gagal untuk '{auth.normalize_email(req.email)}' dari {ip}")
        return JSONResponse({"success": False, "message": "Email atau password salah."}, status_code=401)
    auth.login_throttle.reset(ip, req.email)
    response = JSONResponse({"success": True, "email": user.email})
    _set_session_cookie(response, request, auth.create_session(db, user))
    return response

@app.post("/api/auth/logout")
def logout_endpoint(request: Request, db: Session = Depends(get_db)):
    auth.end_session(db, request.cookies.get(auth.SESSION_COOKIE, ""))
    response = JSONResponse({"success": True})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response

@app.get("/api/auth/me")
def me_endpoint(request: Request):
    return {"email": request.state.user_email}

@app.post("/api/auth/change-password")
def change_password_endpoint(req: ChangePasswordRequest, request: Request, db: Session = Depends(get_db)):
    user = auth.authenticate(db, request.state.user_email, req.current_password)
    if not user:
        return {"success": False, "message": "Password saat ini salah."}
    if req.new_password == req.current_password:
        return {"success": False, "message": "Password baru harus berbeda dari password lama."}
    try:
        auth.set_user_password(db, user.email, req.new_password)   # ends every session
    except ValueError as e:
        return {"success": False, "message": str(e)}
    # Keep this browser signed in; every other device must log in again.
    response = JSONResponse({"success": True, "message": "Password diganti. Perangkat lain telah dikeluarkan."})
    _set_session_cookie(response, request, auth.create_session(db, user))
    return response

# Helper function to get setting
def get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.query(AppSetting).filter(AppSetting.key == key).first()
    return s.value if s and s.value else default

# Statuses from which a post may start publishing. "publishing" is the in-flight
# claim that prevents two requests from posting the same draft twice.
PUBLISHABLE_STATUSES = ("ready", "draft", "failed")
LOCKED_STATUSES = ("published", "publishing")

def list_page_rows(db: Session) -> list:
    """All managed Fanspage rows (ORM objects, not serialized)."""
    return db.query(FacebookPage).order_by(FacebookPage.created_at.asc()).all()

def resolve_secret(db: Session, key: str, submitted: str) -> str:
    """
    The UI only ever sees SECRET_MASK for stored secrets. When it sends the mask
    back (e.g. when testing a saved credential), read the real value from the DB.
    """
    if submitted == SECRET_MASK:
        return get_setting(db, key)
    return submitted

# ==========================================
# WEB UI ROUTES
# ==========================================
ASSET_FILES = [BASE_DIR / "static" / "css" / "custom.css", BASE_DIR / "static" / "js" / "app.js"]

def asset_version() -> str:
    """
    Derived from the files' modification time, so editing css/js takes effect on
    the next reload without restarting the server or clearing the browser cache.
    """
    try:
        return str(int(max(f.stat().st_mtime for f in ASSET_FILES if f.exists())))
    except ValueError:
        return "1"

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"asset_v": asset_version()}
    )

# ==========================================
# REST API: SETTINGS & CREDENTIALS
# ==========================================
# A successful "Uji Koneksi" is remembered as a fingerprint of the exact key and
# model(s) it tested, so the status survives a page reload and lapses by itself
# as soon as the stored key or model differs from what was tested.
VERIFIED_FP_KEY = {"gemini": "gemini_verified_fp", "openai": "openai_verified_fp"}
_VERIFIED_FIELDS = {
    "gemini": ("gemini_api_key", "gemini_text_model"),
    "openai": ("openai_api_key", "openai_text_model", "openai_image_model"),
}
# Computed or internal: never written from the settings form.
READ_ONLY_SETTING_KEYS = set(VERIFIED_FP_KEY.values()) | {f"{p}_status" for p in VERIFIED_FP_KEY}

def _verification_fingerprint(*values: str) -> str:
    return hashlib.sha256("".join(values).encode("utf-8")).hexdigest()

def remember_verification(db: Session, provider: str, success: bool, *values: str):
    key = VERIFIED_FP_KEY[provider]
    row = db.query(AppSetting).filter(AppSetting.key == key).first() or AppSetting(key=key)
    row.value = _verification_fingerprint(*values) if success else ""
    db.add(row)
    db.commit()

def provider_status(db: Session, provider: str) -> str:
    stored = get_setting(db, VERIFIED_FP_KEY[provider])
    current = _verification_fingerprint(*(get_setting(db, k) for k in _VERIFIED_FIELDS[provider]))
    return "Terhubung" if stored and get_setting(db, _VERIFIED_FIELDS[provider][0]) and stored == current         else "Belum diverifikasi"

@app.get("/api/settings")
def get_all_settings(db: Session = Depends(get_db)):
    """
    Secrets are never returned in cleartext; the UI receives a mask placeholder
    and sends it back unchanged when the user does not edit the field.
    """
    settings_records = db.query(AppSetting).all()
    result = {}
    for s in settings_records:
        if s.key in READ_ONLY_SETTING_KEYS:
            continue
        if s.key in SENSITIVE_SETTING_KEYS and s.value:
            result[s.key] = SECRET_MASK
        else:
            result[s.key] = s.value
    for provider in VERIFIED_FP_KEY:
        result[f"{provider}_status"] = provider_status(db, provider)
    return result

@app.post("/api/settings")
def save_settings(payload: Dict[str, Any], db: Session = Depends(get_db)):
    for role_key in ("text_provider", "image_provider"):
        if role_key in payload and str(payload[role_key]) not in PROVIDER_LABELS:
            raise HTTPException(status_code=422, detail=f"Penyedia AI '{payload[role_key]}' tidak dikenal.")
    try:
        for k, v in payload.items():
            if k in READ_ONLY_SETTING_KEYS:
                continue
            # Masked secret came back untouched -> keep the stored value.
            if k in SENSITIVE_SETTING_KEYS and str(v) == SECRET_MASK:
                continue
            setting = db.query(AppSetting).filter(AppSetting.key == k).first()
            if setting:
                setting.value = str(v)
            else:
                db.add(AppSetting(key=k, value=str(v)))
        db.commit()
        # Posting schedules are per Fanspage now; nothing here touches the cron jobs.
        return {
            "success": True,
            "message": "Pengaturan berhasil disimpan.",
            "scheduler": get_schedule_status()
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

class TestGeminiRequest(BaseModel):
    api_key: str
    model_name: Optional[str] = DEFAULT_TEXT_MODEL

@app.post("/api/settings/test-gemini")
def test_gemini_endpoint(req: TestGeminiRequest, db: Session = Depends(get_db)):
    api_key = resolve_secret(db, "gemini_api_key", req.api_key)
    if not api_key:
        return {"success": False, "message": "Gemini API Key belum diisi."}
    model = req.model_name or DEFAULT_TEXT_MODEL
    result = test_gemini_key(api_key, model)
    remember_verification(db, "gemini", result.get("success", False), api_key, model)
    return result

class TestOpenAIRequest(BaseModel):
    api_key: str
    text_model: Optional[str] = DEFAULT_OPENAI_TEXT_MODEL
    image_model: Optional[str] = DEFAULT_OPENAI_IMAGE_MODEL

@app.post("/api/settings/test-openai")
def test_openai_endpoint(req: TestOpenAIRequest, db: Session = Depends(get_db)):
    api_key = resolve_secret(db, "openai_api_key", req.api_key)
    if not api_key:
        return {"success": False, "message": "OpenAI API Key belum diisi."}
    text_model = req.text_model or DEFAULT_OPENAI_TEXT_MODEL
    image_model = req.image_model or DEFAULT_OPENAI_IMAGE_MODEL
    result = test_openai_key(api_key, text_model, image_model)
    remember_verification(db, "openai", result.get("success", False), api_key, text_model, image_model)
    return result

# ==========================================
# REST API: FANSPAGE MANAGEMENT
# ==========================================
@app.get("/api/pages")
def get_pages(db: Session = Depends(get_db)):
    return {"pages": list_pages(db)}

class AddPageRequest(BaseModel):
    page_id: str
    access_token: str
    verify: Optional[bool] = True

@app.post("/api/pages")
def add_page_endpoint(req: AddPageRequest, db: Session = Depends(get_db)):
    result = add_page(db, req.page_id, req.access_token, bool(req.verify))
    if result.get("success"):
        _refresh_schedule()
    return result

class VerifyPageRequest(BaseModel):
    page_id: str
    access_token: str

@app.post("/api/pages/verify")
def verify_page_endpoint(req: VerifyPageRequest):
    """Checks credentials before adding a page, without storing anything."""
    return verify_page_credentials(req.page_id, req.access_token)

@app.patch("/api/pages/{page_row_id}")
def update_page_endpoint(page_row_id: int, payload: Dict[str, Any], db: Session = Depends(get_db)):
    if "auto_post_times" in payload:
        if not parse_post_times(str(payload["auto_post_times"])):
            return {
                "success": False,
                "message": "Format jam posting tidak valid. Gunakan format 24 jam, contoh: 10:00,19:00"
            }
    result = update_page(db, page_row_id, payload)
    if result.get("success"):
        _refresh_schedule()
    return result

@app.post("/api/pages/{page_row_id}/verify")
def reverify_page_endpoint(page_row_id: int, db: Session = Depends(get_db)):
    return reverify_page(db, page_row_id)

@app.delete("/api/pages/{page_row_id}")
def delete_page_endpoint(page_row_id: int, db: Session = Depends(get_db)):
    result = delete_page(db, page_row_id)
    if result.get("success"):
        _refresh_schedule()
    return result

def _refresh_schedule():
    """Page schedules changed -> rebuild the cron jobs."""
    try:
        reload_autopost_schedule()
    except Exception as e:
        logger.error(f"Failed to reload autopost schedule: {e}")

# ==========================================
# REST API: TOPICS & ANALYTICS
# ==========================================
def get_int_setting(db: Session, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(get_setting(db, key, str(default)))))
    except (TypeError, ValueError):
        return default

@app.get("/api/topics")
def list_topics(include_inactive: bool = False, page: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Topic catalog. With `page`, weights/usage come from that Fanspage's own
    learning state instead of the global aggregate.
    """
    query = db.query(ContentTopic)
    if not include_inactive:
        query = query.filter(ContentTopic.is_active.isnot(False))
    topics = query.order_by(ContentTopic.weight.desc()).all()

    page_rows = {}
    if page:
        page_rows = {
            r.topic_id: r for r in
            db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page).all()
        }

    titles = {t.id: t.title for t in db.query(ContentTopic).all()}
    variant_counts = {}
    for t in db.query(ContentTopic).filter(ContentTopic.source == "ai").all():
        if t.base_topic_id:
            variant_counts[t.base_topic_id] = variant_counts.get(t.base_topic_id, 0) + 1

    out = []
    for t in topics:
        is_base = (t.source or "seed") == "seed"
        row = page_rows.get(t.id)
        out.append({
            "id": t.id,
            "category": t.category,
            "topic_key": t.topic_key,
            "title": t.title,
            "core_concept": t.core_concept,
            "weight": (row.weight if row and row.weight else t.weight),
            "global_weight": t.weight,
            "posts_count": (row.posts_count if row else 0) if page else t.posts_count,
            "avg_reach": (row.avg_reach if row else 0.0) if page else t.avg_reach,
            "scoped_to_page": bool(page),
            "source": t.source or "seed",
            "is_base": is_base,
            "is_active": t.is_active is not False,
            "base_topic_id": t.base_topic_id,
            "base_topic_title": titles.get(t.base_topic_id) if not is_base else None,
            "variant_count": variant_counts.get(t.id, 0),
            "origin_note": t.origin_note,
            "last_used_at": iso_utc(t.last_used_at),
        })
    out.sort(key=lambda t: t["weight"] or 0, reverse=True)
    return out

@app.get("/api/topics/curriculum")
def topic_curriculum(db: Session = Depends(get_db)):
    """
    The base curriculum with the variants each base topic has produced —
    the learning tree the system builds on.
    """
    bases = (
        db.query(ContentTopic)
        .filter(ContentTopic.source == "seed")
        .order_by(ContentTopic.weight.desc())
        .all()
    )
    variants = db.query(ContentTopic).filter(ContentTopic.source == "ai").all()

    tree = []
    for base in bases:
        children = [v for v in variants if v.base_topic_id == base.id]
        tree.append({
            "id": base.id,
            "title": base.title,
            "category": base.category,
            "weight": base.weight,
            "posts_count": base.posts_count,
            "avg_reach": base.avg_reach,
            "variants": [{
                "id": v.id,
                "title": v.title,
                "weight": v.weight,
                "posts_count": v.posts_count,
                "is_active": v.is_active is not False,
                "created_at": iso_utc(v.created_at),
            } for v in sorted(children, key=lambda x: x.weight or 0, reverse=True)],
        })

    orphans = [v for v in variants if not v.base_topic_id]
    return {
        "base_count": len(bases),
        "variant_count": len(variants),
        "curriculum": tree,
        "unlinked_variants": [{"id": v.id, "title": v.title} for v in orphans],
    }

@app.get("/api/analytics/window")
def analytics_window(days: Optional[int] = None, page: Optional[int] = None, db: Session = Depends(get_db)):
    """Topic leaderboard for posts published in the last N days (default: 7)."""
    window_days = days or get_int_setting(db, "topic_window_days", 7, 1, 90)
    return window_performance(db, window_days, page)

@app.post("/api/topics/evolve")
def evolve_topics_endpoint(page: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Creates new topics derived from the best performing topics of the window.
    Scoped to one Fanspage's audience when `page` is given.
    """
    text_ai = ai_backend(db, "text")
    if text_ai["missing"]:
        return {"success": False, "message": text_ai["missing"]}

    target = get_page(db, page) if page else default_page(db)
    return evolve_topics(
        db=db,
        api_key=text_ai["api_key"],
        model_name=text_ai["model"],
        provider=text_ai["provider"],
        window_days=get_int_setting(db, "topic_window_days", 7, 1, 90),
        max_new=get_int_setting(db, "max_new_topics_per_cycle", 2, 1, 5),
        language=(target.content_language if target else None) or DEFAULT_CONTENT_LANGUAGE,
        page_id=target.id if target else None,
    )

@app.post("/api/topics/{topic_id}/retire")
def retire_topic_endpoint(topic_id: int, db: Session = Depends(get_db)):
    return retire_topic(db, topic_id)

@app.post("/api/topics/{topic_id}/reactivate")
def reactivate_topic_endpoint(topic_id: int, db: Session = Depends(get_db)):
    return reactivate_topic(db, topic_id)

@app.get("/api/analytics/summary")
def analytics_summary(page: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Post).filter(Post.status == "published")
    if page:
        query = query.filter(Post.page_id == page)
    posts = query.all()

    total_posts = len(posts)
    total_reach = 0
    total_engagement = 0

    for p in posts:
        if p.metrics and len(p.metrics) > 0:
            m = p.metrics[0]
            total_reach += m.reach
            total_engagement += (m.reactions + m.comments + m.shares)

    # The card names the focus topic, so it must name the topic the
    # learning loop actually favours — not a lifetime sum of scores, which rewards
    # topics that were merely posted often and can disagree with what gets produced.
    measured = [p for p in posts if p.metrics]
    winning = "Belum cukup data"
    if measured:
        if page:
            # Same ranking the focus rotation serves first (raw average reach).
            winners = focus_winners(db, page)
            top_topic = winners[0]["leader"] if winners else None
        else:
            topics = topics_for_page(db, None)
            top_topic = max(topics, key=lambda t: t.weight or 0, default=None)
        if top_topic:
            winning = top_topic.title

    learning = None
    if page:
        phase = learning_phase(db, page)
        learning = {
            "phase": phase["phase"],
            "tested": phase["tested"],
            "measured": phase["measured"],
            "total": phase["total"],
        }
        if phase["phase"] == "focus":
            page_row = get_page(db, page)
            winners = focus_winners(db, page)
            restarted = page_row and page_row.focus_leader_key != (winners[0]["key"] if winners else None)
            learning["next_rank"] = 1 if restarted or not winners else (page_row.focus_cursor or 0) % len(winners) + 1
            learning["rotation"] = [
                {
                    "rank": i,
                    "title": w["leader"].title,
                    "avg_reach": round(w["avg_reach"]),
                    "fresh_variants": len(w["fresh_variants"]),
                }
                for i, w in enumerate(winners, start=1)
            ]

    return {
        "page_id": page,
        "total_posts": total_posts,
        "total_reach": total_reach,
        "total_engagement": total_engagement,
        "winning_topic": winning,
        "learning": learning,
        "has_metrics": bool(measured),
        "last_updated": iso_utc(max((p.metrics[0].last_checked_at for p in measured), default=None))
    }

@app.post("/api/analytics/sync")
def sync_metrics_endpoint(page: Optional[int] = None, db: Session = Depends(get_db)):
    """Refreshes metrics for one page, or for every managed page."""
    targets = [get_page(db, page)] if page else list_page_rows(db)
    targets = [p for p in targets if p and p.access_token]
    if not targets:
        return {"success": False, "message": "Belum ada Fanspage dengan Access Token. Tambahkan di tab Fanspage."}

    updated, per_page = [], []
    for row in targets:
        page_updates = update_all_post_metrics(db, row.page_id, row.access_token, row.id)
        updated.extend(page_updates)
        per_page.append({"page": row.name, "updated": len(page_updates)})

    return {"success": True, "updated_count": len(updated), "pages": per_page, "details": updated}

@app.post("/api/feedback/optimize")
def optimize_feedback_endpoint(page: Optional[int] = None, db: Session = Depends(get_db)):
    window_days = get_int_setting(db, "topic_window_days", 7, 1, 90)
    if page:
        return optimize_topic_weights(db, window_days, page)
    result = optimize_all_pages(db, window_days)
    # Keep the shape the UI expects for a single run.
    return {**result["global"], "pages": result["pages"]}

# ==========================================
# REST API: CONTENT GENERATION & PUBLISHING
# ==========================================
class GenerateRequest(BaseModel):
    topic_id: Optional[str] = "auto"
    language: Optional[str] = None
    aspect_ratio: Optional[str] = None
    page_id: Optional[int] = None   # which Fanspage this content is for

@app.post("/api/generate")
def generate_content_endpoint(req: GenerateRequest, db: Session = Depends(get_db)):
    text_ai, image_ai = ai_backend(db, "text"), ai_backend(db, "image")
    for backend in (text_ai, image_ai):
        if backend["missing"]:
            return {"success": False, "message": backend["missing"]}

    # Content is produced per page: language and ratio follow the page's own settings.
    page = get_page(db, req.page_id) if req.page_id else default_page(db)
    language = req.language or (page.content_language if page else None) or DEFAULT_CONTENT_LANGUAGE
    aspect_ratio = req.aspect_ratio or (page.aspect_ratio if page else None) or "3:4"

    # 1. Select Topic (Adaptive or Manual) using this page's learned weights
    topic = None
    if req.topic_id and req.topic_id != "auto":
        try:
            topic = db.query(ContentTopic).filter(ContentTopic.id == int(req.topic_id)).first()
        except (TypeError, ValueError):
            logger.warning(f"Invalid topic_id '{req.topic_id}', falling back to auto-select.")
    if not topic:
        topic = get_next_recommended_topic(db, page.id if page else None)

    topic_dict = {
        "title": topic.title,
        "category": topic.category,
        "core_concept": topic.core_concept,
        "visual_blueprint": topic.visual_blueprint
    }

    img_path_sementara = None
    try:
        # 2. Generate Concept and Copy
        content_plan = generate_post_content(
            api_key=text_ai["api_key"],
            topic_dict=topic_dict,
            language=language,
            model_name=text_ai["model"],
            provider=text_ai["provider"],
        )

        # 3. Generate Poster Image
        img_filename, img_path = generate_poster_image(
            api_key=image_ai["api_key"],
            prompt=content_plan["imagen_prompt"],
            aspect_ratio=aspect_ratio,
            model_name=image_ai["model"],
            provider=image_ai["provider"],
        )
        img_path_sementara = img_path

        # 4. Save to Database as Draft/Ready
        new_post = Post(
            page_id=page.id if page else None,
            topic_id=topic.id,
            topic_title=topic.title,
            language=language,
            visual_title=content_plan.get("visual_title", topic.title),
            prompt_used=content_plan["imagen_prompt"],
            image_filename=img_filename,
            image_path=img_path,
            caption=content_plan["caption"],
            status="ready",
            created_at=datetime.now(timezone.utc)
        )
        db.add(new_post)
        mark_topic_used(db, topic, page.id if page else None)
        db.commit()
        db.refresh(new_post)

        return {
            "success": True,
            "post": {
                "id": new_post.id,
                "page_id": new_post.page_id,
                "page_name": page.name if page else None,
                "visual_title": new_post.visual_title,
                "topic_title": new_post.topic_title,
                "caption": new_post.caption,
                "image_url": f"/storage/generated_images/{new_post.image_filename}",
                "prompt_used": new_post.prompt_used,
                "status": new_post.status,
                "fb_post_url": None,
                "created_at": iso_utc(new_post.created_at),
                "published_at": None,
                "metrics": None
            }
        }
    except Exception as e:
        db.rollback()
        # The poster was rendered but no row references it: don't leave it behind.
        if img_path_sementara:
            remove_generated_image(img_path_sementara)
        logger.exception("Generate error")
        return {"success": False, "message": f"Gagal generate konten: {str(e)}"}

@app.get("/api/scheduler/status")
def scheduler_status(db: Session = Depends(get_db)):
    status = get_schedule_status()
    names = {p.id: p.name for p in list_page_rows(db)}
    for entry in status.get("pages", []):
        entry["page_name"] = names.get(entry["page_id"], "—")
    return status

class PublishRequest(BaseModel):
    # Optional: when omitted the stored caption is published as-is. This prevents
    # publishing one post with another post's caption.
    caption: Optional[str] = None
    visual_title: Optional[str] = None
    page_id: Optional[int] = None   # override target page (defaults to the post's own)

@app.post("/api/posts/{post_id}/publish")
def publish_post_endpoint(post_id: int, req: PublishRequest, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")

    if post.status == "published":
        return {
            "success": False,
            "message": "Postingan ini sudah pernah ditayangkan ke Facebook.",
            "post_url": post.fb_post_url
        }
    if post.status == "publishing":
        return {
            "success": False,
            "message": "Postingan ini sedang dipublikasikan. Tunggu sebentar lalu muat ulang.",
        }

    # Publish to the post's OWN page. An orphaned draft (its page was deleted) is
    # never silently redirected to some other Fanspage: either the caller names the
    # target explicitly, or there is exactly one page and the choice is unambiguous.
    if req.page_id:
        target = get_page(db, req.page_id)
    elif post.page_id:
        target = get_page(db, post.page_id)
    else:
        pages = list_page_rows(db)
        if len(pages) == 1:
            target = pages[0]
        elif not pages:
            return {"success": False, "message": "Belum ada Fanspage terdaftar. Tambahkan di tab Fanspage."}
        else:
            return {
                "success": False,
                "needs_page": True,
                "message": (
                    "Draft ini tidak terikat ke Fanspage manapun (halaman asalnya sudah dihapus). "
                    "Pilih Fanspage tujuan terlebih dahulu."
                ),
            }

    if not target:
        return {"success": False, "message": "Fanspage tujuan tidak ditemukan."}
    if not target.access_token:
        return {"success": False, "message": f"Fanspage '{target.name}' belum punya Access Token."}

    # Claim the post atomically BEFORE talking to Facebook. Without this, two
    # requests arriving together (double click, two tabs, a retry after a slow
    # upload) both passed the "not published yet" check above and each created a
    # real, public Facebook post — while the database only remembered one.
    claimed = (
        db.query(Post)
        .filter(Post.id == post_id, Post.status.in_(PUBLISHABLE_STATUSES))
        .update({Post.status: "publishing"}, synchronize_session=False)
    )
    if claimed != 1:
        db.rollback()
        return {
            "success": False,
            "message": "Postingan ini sedang dipublikasikan atau sudah tayang. Muat ulang untuk melihat statusnya.",
        }

    # Only overwrite stored copy when the client actually sent an edited version.
    if req.caption is not None:
        post.caption = req.caption
    if req.visual_title:
        post.visual_title = req.visual_title
    post.page_id = target.id
    db.commit()   # persist the claim so concurrent requests see "publishing"

    # Publish to Facebook
    try:
        res = publish_photo_to_page(
            page_id=target.page_id,
            access_token=target.access_token,
            image_path=post.image_path,
            caption=post.caption
        )
    except Exception as e:
        # Never leave the post stuck in "publishing".
        logger.exception("Publish error")
        res = {"success": False, "message": f"Koneksi ke Facebook gagal: {e}"}

    if res.get("success"):
        post.status = "published"
        post.fb_post_id = res.get("post_id")
        post.fb_post_url = res.get("post_url")
        post.published_at = datetime.now(timezone.utc)
        post.error_message = None
        db.commit()
        return {
            "success": True,
            "post_url": res.get("post_url"),
            "message": f"Berhasil dipublikasikan ke '{target.name}'!"
        }
    else:
        post.status = "failed"
        post.error_message = res.get("message")
        db.commit()
        return {
            "success": False,
            "message": f"Gagal publish: {res.get('message')}"
        }

@app.post("/api/posts/{post_id}/regenerate-image")
def regenerate_image_endpoint(post_id: int, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")

    if post.status in LOCKED_STATUSES:
        # "publishing": the file is being uploaded right now — replacing it would
        # delete the poster from under the Facebook upload.
        raise HTTPException(status_code=409, detail="Poster yang sudah tayang atau sedang dipublikasikan tidak dapat diganti.")

    image_ai = ai_backend(db, "image")
    if image_ai["missing"]:
        return {"success": False, "message": image_ai["missing"]}

    # The ratio belongs to the Fanspage this post was written for, not to a global setting.
    page = get_page(db, post.page_id) if post.page_id else None
    aspect_ratio = (page.aspect_ratio if page else None) or "3:4"

    try:
        old_image_path = post.image_path
        new_filename, new_path = generate_poster_image(
            api_key=image_ai["api_key"],
            prompt=post.prompt_used,
            aspect_ratio=aspect_ratio,
            model_name=image_ai["model"],
            provider=image_ai["provider"],
        )
        post.image_filename = new_filename
        post.image_path = new_path
        db.commit()
        # The replaced poster is no longer referenced anywhere: don't leave it behind.
        if old_image_path and old_image_path != new_path:
            remove_generated_image(old_image_path)
        return {
            "success": True,
            "image_url": f"/storage/generated_images/{new_filename}",
            "prompt_used": post.prompt_used
        }
    except Exception as e:
        db.rollback()
        logger.exception("Regenerate image error")
        return {"success": False, "message": str(e)}

@app.post("/api/posts/{post_id}/regenerate-caption")
def regenerate_caption_endpoint(post_id: int, db: Session = Depends(get_db)):
    """
    Rewrites only the caption, in the Fanspage's current language, and keeps the
    poster. The stored image prompt is given as the visual reference so the new
    text describes the poster that will actually be posted.
    """
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")
    if post.status not in ("ready", "draft", "failed"):
        raise HTTPException(status_code=409, detail="Caption postingan yang sudah tayang atau sedang dipublikasikan tidak dapat diganti.")

    text_ai = ai_backend(db, "text")
    if text_ai["missing"]:
        return {"success": False, "message": text_ai["missing"]}

    page = get_page(db, post.page_id) if post.page_id else None
    language = (page.content_language if page else None) or post.language or DEFAULT_CONTENT_LANGUAGE
    topic = db.query(ContentTopic).filter(ContentTopic.id == post.topic_id).first() if post.topic_id else None
    topic_dict = {
        "title": topic.title if topic else post.topic_title,
        "category": topic.category if topic else "",
        "core_concept": topic.core_concept if topic else "",
        "visual_blueprint": post.prompt_used or (topic.visual_blueprint if topic else ""),
    }

    try:
        content = generate_post_content(
            api_key=text_ai["api_key"],
            topic_dict=topic_dict,
            language=language,
            model_name=text_ai["model"],
            provider=text_ai["provider"],
        )
    except Exception as e:
        logger.exception("Regenerate caption error")
        return {"success": False, "message": f"Gagal membuat ulang caption: {e}"}

    caption = (content.get("caption") or "").strip()
    # Unusable model output falls back to the canned template; don't overwrite the draft with it.
    if not caption or caption == template_content(topic_dict, language)["caption"]:
        return {"success": False, "message": "Jawaban AI tidak bisa dipakai. Silakan coba lagi."}

    post.caption = caption
    post.language = language
    db.commit()
    return {"success": True, "caption": caption, "language": language}

@app.delete("/api/posts/{post_id}")
def delete_post_endpoint(post_id: int, db: Session = Depends(get_db)):
    """
    Removes a draft/failed post and its generated poster file.
    Published posts are kept so their Facebook metrics history stays intact.
    """
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")

    if post.status == "publishing":
        return {
            "success": False,
            "message": "Postingan ini sedang dipublikasikan. Tunggu hingga selesai sebelum menghapus."
        }
    if post.status == "published":
        return {
            "success": False,
            "message": "Postingan yang sudah tayang tidak bisa dihapus dari sini (riwayat metrik akan hilang)."
        }

    remove_generated_image(post.image_path)

    title = post.visual_title
    db.delete(post)
    db.commit()
    return {"success": True, "message": f"Draft '{title}' telah dihapus."}

CAPTION_PREVIEW_CHARS = 160

def serialize_post(p: Post, page_names: dict, full: bool = False) -> dict:
    """
    List view stays light: the full caption and image prompt are only sent for a
    single post. With a year of history those two fields alone were hundreds of
    kilobytes per request.
    """
    m = p.metrics[0] if p.metrics else None
    data = {
        "id": p.id,
        "page_id": p.page_id,
        "page_name": page_names.get(p.page_id),
        "is_orphan": p.page_id is None,
        "visual_title": p.visual_title,
        "topic_title": p.topic_title,
        "caption_preview": (p.caption or "")[:CAPTION_PREVIEW_CHARS],
        "image_url": f"/storage/generated_images/{p.image_filename}",
        "status": p.status,
        "fb_post_id": p.fb_post_id,
        "fb_post_url": p.fb_post_url,
        "error_message": p.error_message,
        "created_at": iso_utc(p.created_at),
        "published_at": iso_utc(p.published_at),
        "metrics": {
            "reactions": m.reactions if m else 0,
            "comments": m.comments if m else 0,
            "shares": m.shares if m else 0,
            "reach": m.reach if m else 0,
        } if m else None
    }
    if full:
        data["caption"] = p.caption
        data["prompt_used"] = p.prompt_used
    return data

class DraftUpdate(BaseModel):
    caption: str
    visual_title: str


@app.patch("/api/posts/{post_id}")
def save_draft(post_id: int, payload: DraftUpdate, db: Session = Depends(get_db)):
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")
    if post.status not in ("ready", "draft", "failed"):
        raise HTTPException(status_code=409, detail="Hanya draft yang dapat diedit.")
    if not payload.visual_title.strip():
        raise HTTPException(status_code=422, detail="Judul catatan wajib diisi.")
    post.caption = payload.caption
    post.visual_title = payload.visual_title.strip()
    db.commit()
    return {"success": True}


@app.get("/api/posts")
def list_posts(
    page: Optional[int] = None,
    limit: int = 30,
    offset: int = 0,
    search: str = "",
    status: str = "",
    db: Session = Depends(get_db),
):
    query = db.query(Post)
    if page:
        # Orphaned posts (their Fanspage was deleted) belong to no page, so they are
        # always included — otherwise they would be invisible and impossible to clean up.
        query = query.filter(or_(Post.page_id == page, Post.page_id.is_(None)))

    if search.strip():
        query = query.filter(Post.visual_title.contains(search.strip(), autoescape=True))
    if status:
        query = query.filter(Post.status == status)
    total = query.count()
    limit = max(1, min(limit, 200))
    posts = query.order_by(Post.created_at.desc()).offset(max(0, offset)).limit(limit).all()

    page_names = {p.id: p.name for p in list_page_rows(db)}
    items = [serialize_post(p, page_names) for p in posts]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(items) < total,
    }

@app.get("/api/posts/{post_id}")
def get_post(post_id: int, db: Session = Depends(get_db)):
    """Full record, including the caption and image prompt the Studio needs."""
    post = db.query(Post).filter(Post.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Postingan tidak ditemukan.")
    page_names = {p.id: p.name for p in list_page_rows(db)}
    return serialize_post(post, page_names, full=True)

# ==========================================
# REST API: COMMENT AUTO-REPLY
# ==========================================
REPLY_STATUSES = ("pending", "replying", "replied", "skipped", "failed", "dismissed")

class ReplySendRequest(BaseModel):
    message: Optional[str] = None

def _reply_or_404(db: Session, reply_id: int) -> CommentReply:
    row = db.query(CommentReply).filter(CommentReply.id == reply_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Komentar tidak ditemukan.")
    return row

@app.get("/api/replies")
def list_replies(page: Optional[int] = None, status: str = "", limit: int = 30, offset: int = 0,
                 db: Session = Depends(get_db)):
    target = get_page(db, page) if page else default_page(db)
    if not target:
        return {"items": [], "total": 0, "has_more": False, "counts": {}}
    base = db.query(CommentReply).filter(CommentReply.page_id == target.id)
    counts = {s: base.filter(CommentReply.status == s).count() for s in REPLY_STATUSES}
    query = base.filter(CommentReply.status == status) if status in REPLY_STATUSES else base
    total = query.count()
    limit = max(1, min(limit, 100))
    rows = (query.order_by(CommentReply.created_at.desc(), CommentReply.id.desc())
                 .offset(max(0, offset)).limit(limit).all())
    return {
        "items": [comment_reply.serialize_reply(r) for r in rows],
        "total": total,
        "has_more": offset + len(rows) < total,
        "counts": counts,
    }

@app.post("/api/replies/scan")
def scan_replies(page: Optional[int] = None, db: Session = Depends(get_db)):
    """Checks the page for new comments right now, using the page's reply mode."""
    target = get_page(db, page) if page else default_page(db)
    if not target:
        return {"success": False, "message": "Pilih Fanspage terlebih dahulu."}
    return comment_reply.process_page_comments(db, target)

@app.post("/api/replies/{reply_id}/send")
def send_reply_endpoint(reply_id: int, req: ReplySendRequest, db: Session = Depends(get_db)):
    row = _reply_or_404(db, reply_id)
    message = (req.message if req.message is not None else row.reply_message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="Balasan tidak boleh kosong.")
    if len(message) > 1000:
        raise HTTPException(status_code=422, detail="Balasan terlalu panjang (maksimal 1000 karakter).")
    page = get_page(db, row.page_id)
    if not page or not page.access_token:
        return {"success": False, "message": "Fanspage komentar ini tidak punya Access Token."}

    if not comment_reply.claim_for_sending(db, reply_id, ("pending", "failed", "skipped")):
        return {"success": False, "message": "Komentar ini sedang dikirim atau sudah dibalas. Muat ulang daftar."}
    db.refresh(row)
    res = comment_reply.send_reply(db, page, row, message)
    return {
        "success": bool(res.get("success")),
        "message": "Balasan terkirim." if res.get("success") else f"Gagal mengirim: {res.get('message')}",
        "reply": comment_reply.serialize_reply(row),
    }

@app.post("/api/replies/{reply_id}/regenerate")
def regenerate_reply_endpoint(reply_id: int, db: Session = Depends(get_db)):
    row = _reply_or_404(db, reply_id)
    if row.status not in ("pending", "failed", "skipped", "dismissed"):
        raise HTTPException(status_code=409, detail="Komentar yang sudah dibalas tidak bisa dibuat ulang.")
    text_ai = ai_backend(db, "text")
    if text_ai["missing"]:
        return {"success": False, "message": text_ai["missing"]}
    page = get_page(db, row.page_id)
    try:
        decision = comment_reply.generate_comment_reply(
            text_ai, page.name if page else "", (page.content_language if page else None) or DEFAULT_CONTENT_LANGUAGE,
            row.post_message, row.commenter_name, row.comment_message,
            comment_reply._recent_replies(db, row.page_id))
    except Exception as e:
        logger.exception("Reply regenerate error")
        return {"success": False, "message": f"Gagal menyusun balasan: {e}"}
    if decision["skip"]:
        return {"success": False, "message": f"AI menyarankan tidak membalas: {decision['reason'] or 'tidak perlu dibalas'}. "
                                             "Anda tetap bisa menulis balasan sendiri."}
    row.reply_message = decision["reply"]
    row.status = "pending"
    row.note = None
    db.commit()
    return {"success": True, "message": "Draft balasan baru siap.", "reply": comment_reply.serialize_reply(row)}

@app.post("/api/replies/{reply_id}/dismiss")
def dismiss_reply_endpoint(reply_id: int, db: Session = Depends(get_db)):
    row = _reply_or_404(db, reply_id)
    if row.status not in ("pending", "failed", "skipped"):
        raise HTTPException(status_code=409, detail="Komentar ini tidak bisa diabaikan.")
    row.status = "dismissed"
    db.commit()
    return {"success": True, "message": "Komentar diabaikan.", "reply": comment_reply.serialize_reply(row)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
