"""
Instagram cross-posting: each post published on a Fanspage is also published,
same poster and same caption, to the Instagram professional account linked to
that page.

- A periodic job picks up the page's newly published posts, so a restart never
  loses one; only posts published after Instagram was switched on are sent.
- Instagram downloads the image from a public HTTPS URL. The poster folder needs
  a login, so each post gets a random, short-lived link serving only its poster.
- Instagram accepts aspect ratios from 4:5 to 1.91:1; the 3:4 posters are padded
  (never cropped, so no text is cut) to 4:5.
"""
import io
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from PIL import Image, ImageStat
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.fb_client import find_instagram_account, publish_photo_to_instagram
from core.utils import redact_secrets
from database.models import AppSetting, FacebookPage, Post

logger = logging.getLogger(__name__)

IG_WINDOW_HOURS = 24
IG_MAX_ATTEMPTS = 3
IG_LINK_MINUTES = 120
IG_CAPTION_MAX = 2200
IG_WIDTH = 1080
MIN_RATIO, MAX_RATIO = 4 / 5, 1.91
PUBLIC_BASE_URL_KEY = "public_base_url"
UNCERTAIN_NOTE = "Status tidak pasti: koneksi terputus saat mempublikasikan ke Instagram. Periksa akun Instagram."


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ public address


def _is_public(url: str) -> bool:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and bool(host) and host not in ("localhost",)
            and not host.startswith(("127.", "10.", "192.168.", "0.")) and "." in host)


def public_base_url(db: Session) -> str:
    """PUBLIC_BASE_URL, else the HTTPS address the dashboard was last opened at."""
    explicit = (os.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    row = db.query(AppSetting).filter(AppSetting.key == PUBLIC_BASE_URL_KEY).first()
    return (row.value or "").rstrip("/") if row else ""


def remember_public_base_url(db: Session, url: str) -> None:
    """Called when the dashboard is opened: its HTTPS address is where Instagram fetches posters."""
    url = (url or "").rstrip("/")
    if not _is_public(url):
        return
    row = db.query(AppSetting).filter(AppSetting.key == PUBLIC_BASE_URL_KEY).first()
    if row and row.value == url:
        return
    row = row or AppSetting(key=PUBLIC_BASE_URL_KEY)
    row.value = url
    db.add(row)
    db.commit()


# ------------------------------------------------------------------ image


def instagram_image_bytes(image_path: str) -> bytes:
    """The poster as an Instagram-ready JPEG: padded into 4:5..1.91:1, 1080 px wide."""
    with Image.open(image_path) as src:
        img = src.convert("RGB")
    w, h = img.size
    ratio = w / h
    if ratio < MIN_RATIO or ratio > MAX_RATIO:
        # Pad with the poster's own edge colour so the frame looks intended.
        edge = img.crop((0, 0, w, max(1, h // 50)))
        fill = tuple(int(v) for v in ImageStat.Stat(edge).median)
        if ratio < MIN_RATIO:
            canvas = Image.new("RGB", (round(h * MIN_RATIO), h), fill)
        else:
            canvas = Image.new("RGB", (w, round(w / MAX_RATIO)), fill)
        canvas.paste(img, ((canvas.width - w) // 2, (canvas.height - h) // 2))
        img = canvas
    if img.width > IG_WIDTH:
        img = img.resize((IG_WIDTH, round(img.height * IG_WIDTH / img.width)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def post_for_media_token(db: Session, token: str) -> Post | None:
    """The post whose short-lived public poster link this is, if still valid."""
    if not token or len(token) < 20:
        return None
    post = db.query(Post).filter(Post.ig_media_token == token).first()
    if not post or not post.ig_media_expires or post.ig_media_expires < _now():
        return None
    return post


# ------------------------------------------------------------------ linking


def link_instagram(db: Session, page: FacebookPage) -> dict:
    """Looks up and stores the Instagram account linked to the page."""
    res = find_instagram_account(page.page_id, page.access_token)
    if res.get("success"):
        page.ig_user_id, page.ig_username, page.ig_last_error = res["ig_user_id"], res["username"], None
    else:
        page.ig_last_error = res.get("message")
    return res


# ------------------------------------------------------------------ publishing


def due_posts(db: Session, page: FacebookPage, now: datetime | None = None) -> list:
    now = now or _now()
    since = max(now - timedelta(hours=IG_WINDOW_HOURS), page.ig_enabled_at or now)
    return (db.query(Post)
              .filter(Post.page_id == page.id,
                      Post.status == "published",
                      Post.published_at >= since,
                      Post.ig_status.is_(None) | (Post.ig_status == "failed"),
                      Post.ig_attempts.is_(None) | (Post.ig_attempts < IG_MAX_ATTEMPTS))
              .order_by(Post.published_at.asc())
              .all())


def _claim(db: Session, post_id: int, now: datetime) -> str | None:
    """Atomically marks the post 'publishing' and issues its public poster link."""
    token = secrets.token_urlsafe(24)
    claimed = (db.query(Post)
                 .filter(Post.id == post_id, Post.ig_status.is_(None) | (Post.ig_status == "failed"))
                 .update({Post.ig_status: "publishing",
                          Post.ig_attempts: func.coalesce(Post.ig_attempts, 0) + 1,
                          Post.ig_media_token: token,
                          Post.ig_media_expires: now + timedelta(minutes=IG_LINK_MINUTES)},
                         synchronize_session=False))
    db.commit()
    return token if claimed == 1 else None


def process_page_instagram(db: Session, page: FacebookPage, now: datetime | None = None) -> dict:
    """Publishes the page's due posts to its Instagram account. Never raises."""
    summary = {"published": 0, "failed": 0, "message": ""}
    if not (page.ig_enabled and page.access_token):
        return summary
    posts = due_posts(db, page, now)
    if not posts:
        return summary

    base = public_base_url(db)
    if not _is_public(base):
        page.ig_last_error = ("Alamat publik aplikasi belum diketahui. Buka dashboard sekali lewat domain HTTPS-nya "
                              "(bukan localhost), atau isi PUBLIC_BASE_URL di Environment Dokploy.")
        db.commit()
        summary["message"] = page.ig_last_error
        return summary
    if not page.ig_user_id:
        res = link_instagram(db, page)
        db.commit()
        if not res.get("success"):
            summary["message"] = page.ig_last_error
            return summary

    for post in posts:
        token = _claim(db, post.id, now or _now())
        if not token:
            continue
        db.refresh(post)
        res = publish_photo_to_instagram(page.ig_user_id, page.access_token,
                                         f"{base}/media/ig/{token}.jpg", (post.caption or "")[:IG_CAPTION_MAX])
        if res.get("success"):
            post.ig_status, post.ig_error = "published", None
            post.ig_media_id, post.ig_permalink = res.get("media_id"), res.get("permalink")
            page.ig_last_error = None
            summary["published"] += 1
            logger.info(f"[Instagram] '{page.name}' post {post.id} published: {post.ig_permalink}")
        else:
            message = redact_secrets(res.get("message") or "Gagal mempublikasikan ke Instagram.")
            post.ig_status = "uncertain" if res.get("uncertain") else "failed"
            post.ig_error = UNCERTAIN_NOTE if res.get("uncertain") else message
            page.ig_last_error = message
            summary["failed"] += 1
            summary["message"] = message
            logger.warning(f"[Instagram] '{page.name}' post {post.id}: {message}")
        post.ig_media_expires = _now()          # the public link is no longer needed
        db.commit()
        if res.get("permission_error"):
            break
    return summary


def recover_interrupted_instagram(db: Session) -> int:
    """At startup: a post left in 'publishing' may or may not be on Instagram."""
    stuck = db.query(Post).filter(Post.ig_status == "publishing").all()
    for post in stuck:
        post.ig_status, post.ig_error = "uncertain", UNCERTAIN_NOTE
    return len(stuck)
