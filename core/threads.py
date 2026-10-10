"""
Threads cross-posting: each post published on a Fanspage is also published to
the Threads account set on that page, with the same poster.

- Threads allows 500 characters per post, so the caption becomes a thread: the
  poster with the opening of the caption, then the rest as chained replies.
  Captions are split at paragraphs, then lines, sentences and words, never
  mid-word. Threads uses only one tag per post: the first hashtag of the
  caption's closing hashtag line goes on the main post, the others are dropped.
- Progress is saved after every part. A reply that fails is retried later under
  the last part that went live, so the poster is never posted twice.
- Like Instagram: a periodic job picks up newly published posts, the poster is
  served from a random, short-lived public link, and only posts published after
  Threads was switched on are sent.
- Threads has its own user token (60 days). It is renewed every few days.
"""
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.instagram import _is_public, public_base_url
from core.threads_client import (exchange_long_lived_token, get_threads_profile, publish_to_threads,
                                 refresh_long_lived_token)
from core.utils import redact_secrets
from database.models import FacebookPage, Post

logger = logging.getLogger(__name__)

THREADS_WINDOW_HOURS = 24
THREADS_MAX_ATTEMPTS = 3
THREADS_LINK_MINUTES = 120
# Threads counts 500 characters, emoji by their UTF-8 length; counting every
# character by its UTF-8 length always stays inside the limit.
THREADS_TEXT_MAX = 500
THREADS_MAX_PARTS = 6
# Long-lived tokens last 60 days and can be renewed once they are a day old.
THREADS_REFRESH_DAYS = 7
THREADS_REFRESH_MIN_HOURS = 24
UNCERTAIN_NOTE = "Status tidak pasti: koneksi terputus saat mempublikasikan ke Threads. Periksa akun Threads."

_HASHTAG = re.compile(r"#\w+", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ caption -> thread


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _hard_cut(text: str, limit: int) -> list:
    """Last resort for a single word longer than a post (a long URL)."""
    pieces, current = [], ""
    for ch in text:
        if _size(current + ch) > limit:
            pieces.append(current)
            current = ""
        current += ch
    return pieces + ([current] if current else [])


def _pieces(text: str, limit: int) -> list:
    """`text` cut into pieces of at most `limit`, at the coarsest boundary that works."""
    if _size(text) <= limit:
        return [text]
    for pattern, joiner in ((re.compile(r"\n"), "\n"), (_SENTENCE_END, " "), (re.compile(r"\s+"), " ")):
        parts = [p for p in pattern.split(text) if p.strip()]
        if len(parts) > 1:
            return _pack(parts, joiner, limit)
    return _hard_cut(text, limit)


def _pack(units: list, joiner: str, limit: int) -> list:
    """Greedily joins units into pieces of at most `limit`, splitting units that are too big."""
    out, current = [], ""
    for unit in units:
        for piece in _pieces(unit.strip(), limit):
            candidate = f"{current}{joiner}{piece}" if current else piece
            if _size(candidate) <= limit:
                current = candidate
            else:
                if current:
                    out.append(current)
                current = piece
    return out + ([current] if current else [])


def _split_hashtag_line(caption: str) -> tuple:
    """(body, hashtags) where hashtags come from the caption's closing hashtag-only lines."""
    lines = caption.rstrip().split("\n")
    tags = []
    while lines and lines[-1].strip() and not _HASHTAG.sub("", lines[-1]).strip():
        tags = _HASHTAG.findall(lines.pop()) + tags
    return "\n".join(lines).strip(), tags


def split_for_threads(caption: str, limit: int = THREADS_TEXT_MAX, max_parts: int = THREADS_MAX_PARTS) -> list:
    """
    The caption as a thread: a list of texts of at most `limit`, the first one for
    the poster post. Never empty (an empty caption is one empty part).
    """
    body, tags = _split_hashtag_line((caption or "").replace("\r\n", "\n"))
    tag = f"\n\n{tags[0]}" if tags else ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    parts = _pack(paragraphs, "\n\n", limit) or [""]

    # The tag goes on the main post: re-pack if the first part has no room for it.
    if tag and _size(parts[0] + tag) > limit:
        first = _pack([parts[0]], "\n\n", limit - _size(tag))
        parts = first[:1] + _pack(first[1:] + parts[1:], "\n\n", limit)
    parts[0] = (parts[0] + tag).strip()

    if len(parts) > max_parts:
        ellipsis = "…"
        last = parts[max_parts - 1]
        while last and _size(last + ellipsis) > limit:
            last = last[:-1]
        parts = parts[:max_parts - 1] + [last.rstrip() + ellipsis]
    return parts


# ------------------------------------------------------------------ token & account


def connect_threads(db: Session, page: FacebookPage, token: str, now: datetime | None = None) -> dict:
    """
    Stores a Threads token on the page after checking which account it belongs to.
    With THREADS_APP_SECRET set, a short-lived token is first exchanged for a
    long-lived one (60 days).
    """
    token = (token or "").strip()
    if not token:
        return {"success": False, "message": "Tempel Access Token Threads terlebih dahulu."}
    now = now or _now()
    expires = None
    secret = (os.environ.get("THREADS_APP_SECRET") or "").strip()
    if secret:
        res = exchange_long_lived_token(token, secret)
        if res.get("success"):          # otherwise it is most likely long-lived already
            token = res["access_token"]
            if res.get("expires_in"):
                expires = now + timedelta(seconds=int(res["expires_in"]))

    profile = get_threads_profile(token)
    if not profile.get("success"):
        page.threads_last_error = profile.get("message")
        return profile
    page.threads_access_token = token
    page.threads_user_id = profile["threads_user_id"]
    page.threads_username = profile["username"]
    page.threads_token_expires = expires
    page.threads_token_refreshed_at = now
    page.threads_last_error = None
    return profile


def refresh_due(page: FacebookPage, now: datetime | None = None) -> bool:
    if not page.threads_access_token:
        return False
    now = now or _now()
    last = page.threads_token_refreshed_at
    if last is None:
        return True
    age = now - last
    if age < timedelta(hours=THREADS_REFRESH_MIN_HOURS):
        return False
    # Unknown lifetime (pasted token): renew as soon as allowed, which also tells us the expiry.
    return page.threads_token_expires is None or age >= timedelta(days=THREADS_REFRESH_DAYS)


def refresh_threads_token(db: Session, page: FacebookPage, now: datetime | None = None) -> dict:
    """Renews the page's Threads token for another 60 days. Never raises."""
    now = now or _now()
    res = refresh_long_lived_token(page.threads_access_token)
    if res.get("success"):
        page.threads_access_token = res["access_token"]
        page.threads_token_refreshed_at = now
        if res.get("expires_in"):
            page.threads_token_expires = now + timedelta(seconds=int(res["expires_in"]))
        logger.info(f"[Threads] '{page.name}' token renewed until {page.threads_token_expires}.")
    else:
        message = ("Token Threads tidak bisa diperpanjang: " + (res.get("message") or "")
                   + " Tempel token Threads baru di tab Fanspage.")
        page.threads_last_error = message
        res["message"] = message
        logger.warning(f"[Threads] '{page.name}': {message}")
    db.commit()
    return res


# ------------------------------------------------------------------ public poster link


def post_for_media_token(db: Session, token: str) -> Post | None:
    """The post whose short-lived public poster link this is, if still valid."""
    if not token or len(token) < 20:
        return None
    post = db.query(Post).filter(Post.threads_media_token == token).first()
    if not post or not post.threads_media_expires or post.threads_media_expires < _now():
        return None
    return post


# ------------------------------------------------------------------ publishing

RETRYABLE = ("failed", "incomplete")


def due_posts(db: Session, page: FacebookPage, now: datetime | None = None) -> list:
    now = now or _now()
    since = max(now - timedelta(hours=THREADS_WINDOW_HOURS), page.threads_enabled_at or now)
    return (db.query(Post)
              .filter(Post.page_id == page.id,
                      Post.status == "published",
                      Post.published_at >= since,
                      Post.threads_status.is_(None) | Post.threads_status.in_(RETRYABLE),
                      Post.threads_attempts.is_(None) | (Post.threads_attempts < THREADS_MAX_ATTEMPTS))
              .order_by(Post.published_at.asc())
              .all())


def _claim(db: Session, post_id: int, now: datetime) -> str | None:
    """Atomically marks the post 'publishing' and issues its public poster link."""
    token = secrets.token_urlsafe(24)
    claimed = (db.query(Post)
                 .filter(Post.id == post_id,
                         Post.threads_status.is_(None) | Post.threads_status.in_(RETRYABLE))
                 .update({Post.threads_status: "publishing",
                          Post.threads_attempts: func.coalesce(Post.threads_attempts, 0) + 1,
                          Post.threads_media_token: token,
                          Post.threads_media_expires: now + timedelta(minutes=THREADS_LINK_MINUTES)},
                         synchronize_session=False))
    db.commit()
    return token if claimed == 1 else None


def _publish_thread(db: Session, page: FacebookPage, post: Post, image_url: str) -> dict:
    """Posts the parts not yet live, saving progress after each one."""
    parts = split_for_threads(post.caption)
    done = post.threads_parts_done or 0
    for i in range(done, len(parts)):
        if i == 0:
            res = publish_to_threads(page.threads_user_id, page.threads_access_token, parts[0], image_url=image_url)
        else:
            res = publish_to_threads(page.threads_user_id, page.threads_access_token, parts[i],
                                     reply_to_id=post.threads_last_id)
        if not res.get("success"):
            res["part"] = i
            return res
        if i == 0:
            post.threads_media_id, post.threads_permalink = res.get("media_id"), res.get("permalink")
        post.threads_last_id, post.threads_parts_done = res.get("media_id"), i + 1
        db.commit()
    return {"success": True, "parts": len(parts)}


def process_page_threads(db: Session, page: FacebookPage, now: datetime | None = None) -> dict:
    """Publishes the page's due posts to its Threads account. Never raises."""
    summary = {"published": 0, "failed": 0, "message": ""}
    if not (page.threads_enabled and page.threads_access_token and page.threads_user_id):
        return summary
    if refresh_due(page, now):
        res = refresh_threads_token(db, page, now)
        if not res.get("success") and res.get("token_error"):
            summary["message"] = res["message"]
            return summary
    posts = due_posts(db, page, now)
    if not posts:
        return summary

    base = public_base_url(db)
    if not _is_public(base):
        page.threads_last_error = ("Alamat publik aplikasi belum diketahui. Buka dashboard sekali lewat domain HTTPS-nya "
                                   "(bukan localhost), atau isi PUBLIC_BASE_URL di Environment Dokploy.")
        db.commit()
        summary["message"] = page.threads_last_error
        return summary

    for post in posts:
        token = _claim(db, post.id, now or _now())
        if not token:
            continue
        db.refresh(post)
        res = _publish_thread(db, page, post, f"{base}/media/threads/{token}.jpg")
        if res.get("success"):
            post.threads_status, post.threads_error = "published", None
            page.threads_last_error = None
            summary["published"] += 1
            logger.info(f"[Threads] '{page.name}' post {post.id} published in {res['parts']} part(s): "
                        f"{post.threads_permalink}")
        else:
            message = redact_secrets(res.get("message") or "Gagal mempublikasikan ke Threads.")
            if res.get("part"):
                message = f"Bagian {res['part'] + 1} dari utas belum terkirim: {message}"
            if res.get("uncertain"):
                post.threads_status, post.threads_error = "uncertain", UNCERTAIN_NOTE
            else:
                post.threads_status = "incomplete" if post.threads_parts_done else "failed"
                post.threads_error = message
            page.threads_last_error = message
            summary["failed"] += 1
            summary["message"] = message
            logger.warning(f"[Threads] '{page.name}' post {post.id}: {message}")
        post.threads_media_expires = _now()          # the public link is no longer needed
        db.commit()
        if res.get("permission_error"):
            break
    return summary


def recover_interrupted_threads(db: Session) -> int:
    """At startup: a post left in 'publishing' may or may not be (fully) on Threads."""
    stuck = db.query(Post).filter(Post.threads_status == "publishing").all()
    for post in stuck:
        post.threads_status, post.threads_error = "uncertain", UNCERTAIN_NOTE
    return len(stuck)
