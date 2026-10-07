"""
First-comment promotion: under each new post of a Fanspage, the page itself
comments one short invitation to a link (e.g. its website).

- one comment per post, from the page, only on posts this app published;
- worded by the text model to fit that post's topic, and checked against the
  page's previous promo comments so the wording never repeats;
- posted a few minutes after the post goes live (a periodic job picks it up),
  so it survives restarts and reads like an admin adding the link afterwards.
"""
import difflib
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from config import DEFAULT_CONTENT_LANGUAGE
from core.ai_provider import ai_backend, complete_json
from core.fb_client import comment_on_post
from core.utils import redact_secrets
from database.models import FacebookPage, Post

logger = logging.getLogger(__name__)

PROMO_MIN_DELAY_MINUTES = 2      # a post must be live this long before its comment
PROMO_WINDOW_HOURS = 24          # posts older than this are left alone
PROMO_MAX_ATTEMPTS = 3
MAX_PROMO_CHARS = 280
RECENT_FOR_VARIETY = 12
TOO_SIMILAR = 0.72               # difflib ratio against a previous comment

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.I)
UNCERTAIN_NOTE = "Status tidak pasti: aplikasi berhenti saat mengirim komentar promosi. Periksa postingannya."

SYSTEM_PROMPT = """You are the admin of the Facebook Page "{page_name}" (gold prospecting and field geology education).
Right after a new post goes live, you add the page's own first comment that points readers to {url}.

Rules:
- 1 or 2 short sentences, at most {max_chars} characters in total.
- {language_rule}
- Tie it to THIS post's topic, so it reads like a natural follow-up, not an ad.
- Include the link exactly once, written exactly as: {url}
- Sound like a real person. No hashtags, no ALL CAPS, at most 1 emoji, no "click here" spam phrasing.
- Do not promise results (no guaranteed gold, earnings or locations) and do not invent facts about the link.
- Start differently from the previous comments listed by the user and do not reuse their sentences.

Reply ONLY with JSON: {{"comment": "..."}}"""

LANGUAGE_RULES = {
    "en": "Write in casual, natural American English.",
    "id": "Tulis dalam Bahasa Indonesia santai dan natural.",
}

FALLBACK_TEMPLATES = {
    "en": [
        "If this got you curious, there's more on getting started over at {url}",
        "Want to go a step further with this? We put together more at {url}",
        "For anyone planning a trip after reading this, {url} has the next steps.",
        "More practical tips like this are waiting at {url}",
        "Going out to try this yourself? Have a look at {url} first.",
        "This is just one piece of it. The full picture is at {url}",
    ],
    "id": [
        "Kalau penasaran lanjutannya, cek {url}",
        "Mau belajar lebih dalam soal ini? Mampir ke {url}",
        "Buat yang mau langsung praktik, panduannya ada di {url}",
        "Tips lain seperti ini kami kumpulkan di {url}",
        "Sebelum turun ke lapangan, intip dulu {url}",
        "Ini baru sebagian, lengkapnya ada di {url}",
    ],
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_url(raw: str) -> str:
    url = (raw or "").strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def clean_promo(text: str, url: str) -> str:
    """Keeps the link exactly once (and no other link), no hashtags, bounded length."""
    text = (text or "").strip().strip('"').strip("“”").strip()
    text = URL_PATTERN.sub(" ", text)                     # every link out, ours goes back below
    text = re.sub(r"(^|\s)#\w+", " ", text)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[\s:,(\-–—]+$", "", text)            # dangling "at", ":" left by the link
    budget = MAX_PROMO_CHARS - len(url) - 1
    if len(text) > budget:
        text = text[:budget].rsplit(" ", 1)[0].rstrip(",;:-") + "…"
    return f"{text} {url}".strip()


def has_foreign_link(text: str, url: str) -> bool:
    """True when the text links anywhere other than the promoted URL."""
    ours = url.rstrip("/").lower()
    return any(link.rstrip("/.,!?)").rstrip("/").lower() != ours for link in URL_PATTERN.findall(text or ""))


def too_similar(candidate: str, previous: list) -> bool:
    plain = URL_PATTERN.sub("", candidate).lower().strip()
    for old in previous:
        ratio = difflib.SequenceMatcher(None, plain, URL_PATTERN.sub("", old or "").lower().strip()).ratio()
        if ratio >= TOO_SIMILAR:
            return True
    return False


def recent_promos(db: Session, page_id: int) -> list:
    rows = (db.query(Post.promo_comment)
              .filter(Post.page_id == page_id, Post.promo_status == "posted")
              .order_by(Post.published_at.desc())
              .limit(RECENT_FOR_VARIETY).all())
    return [r[0] for r in rows if r[0]]


def _fallback(language: str, url: str, previous: list) -> str:
    options = [t.format(url=url) for t in FALLBACK_TEMPLATES["id" if language == "id" else "en"]]
    fresh = [o for o in options if not too_similar(o, previous)] or options
    return random.choice(fresh)


def write_promo_comment(text_ai: dict, page: FacebookPage, post: Post, previous: list) -> str:
    """A promo comment for `post` that does not repeat `previous`. Never raises."""
    url = normalize_url(page.promo_url)
    language = page.content_language or DEFAULT_CONTENT_LANGUAGE
    system = SYSTEM_PROMPT.format(page_name=page.name or "this page", url=url, max_chars=MAX_PROMO_CHARS,
                                  language_rule=LANGUAGE_RULES["id" if language == "id" else "en"])
    about = (page.promo_note or "").strip()
    listed = "\n".join(f"- {c}" for c in previous) or "- (none yet)"
    user = (
        f"The post that just went live:\n\"\"\"{post.visual_title}\n{(post.caption or '')[:700]}\"\"\"\n\n"
        + (f"What {url} offers (use only this, do not invent): {about[:400]}\n\n" if about else "")
        + f"Previous comments (do not resemble them):\n{listed}\n\nWrite the comment."
    )
    if not text_ai.get("missing"):
        for temperature in (0.9, 1.1):
            try:
                raw = complete_json(text_ai["provider"], text_ai["api_key"], text_ai["model"],
                                    system, user, temperature, reasoning=text_ai.get("reasoning"))
                data = json.loads(raw or "{}")
                comment = str(data.get("comment") or "") if isinstance(data, dict) else ""
                if len(URL_PATTERN.sub("", comment).strip()) >= 15 and not has_foreign_link(comment, url):
                    comment = clean_promo(comment, url)
                    if not too_similar(comment, previous):
                        return comment
            except Exception as e:
                logger.warning(f"[Promo] AI could not write a comment for '{page.name}': {redact_secrets(e)}")
                break
    return _fallback(language, url, previous)


def due_posts(db: Session, page: FacebookPage, now: datetime | None = None) -> list:
    """This page's posts that should get their promo comment now."""
    now = now or _now()
    return (db.query(Post)
              .filter(Post.page_id == page.id,
                      Post.status == "published",
                      Post.fb_post_id.isnot(None),
                      Post.promo_status.is_(None) | (Post.promo_status == "failed"),
                      (Post.promo_attempts.is_(None)) | (Post.promo_attempts < PROMO_MAX_ATTEMPTS),
                      Post.published_at <= now - timedelta(minutes=PROMO_MIN_DELAY_MINUTES),
                      Post.published_at >= now - timedelta(hours=PROMO_WINDOW_HOURS))
              .order_by(Post.published_at.asc())
              .all())


def _claim(db: Session, post_id: int) -> bool:
    """Atomically marks the post 'posting' so two runs never comment twice."""
    claimed = (db.query(Post)
                 .filter(Post.id == post_id,
                         Post.promo_status.is_(None) | (Post.promo_status == "failed"))
                 .update({Post.promo_status: "posting",
                          Post.promo_attempts: func.coalesce(Post.promo_attempts, 0) + 1},
                         synchronize_session=False))
    db.commit()
    return claimed == 1


def process_page_promos(db: Session, page: FacebookPage, now: datetime | None = None) -> dict:
    """Posts the promo comment under each due post of `page`. Never raises."""
    summary = {"posted": 0, "failed": 0}
    url = normalize_url(page.promo_url)
    if not (page.promo_enabled and url and page.access_token):
        return summary
    posts = due_posts(db, page, now)
    if not posts:
        return summary

    text_ai = ai_backend(db, "text")
    previous = recent_promos(db, page.id)
    for post in posts:
        if not _claim(db, post.id):
            continue
        db.refresh(post)
        comment = write_promo_comment(text_ai, page, post, previous)
        res = comment_on_post(post.fb_post_id, page.access_token, comment)
        post.promo_comment = comment
        if res.get("success"):
            post.promo_status = "posted"
            post.promo_comment_fb_id = res.get("reply_id")
            page.promo_last_error = None
            previous.insert(0, comment)
            summary["posted"] += 1
            logger.info(f"[Promo] '{page.name}' commented on post {post.id}.")
        else:
            # A lost connection may still have posted it: never retry blindly.
            post.promo_status = "uncertain" if res.get("uncertain") else "failed"
            page.promo_last_error = redact_secrets(res.get("message") or "Gagal mengirim komentar promosi.")
            summary["failed"] += 1
            logger.warning(f"[Promo] '{page.name}' post {post.id}: {page.promo_last_error}")
        db.commit()
        if res.get("permission_error"):
            break
    return summary


def recover_interrupted_promos(db: Session) -> int:
    """At startup: a promo left in 'posting' may or may not have reached Facebook."""
    stuck = db.query(Post).filter(Post.promo_status == "posting").all()
    for post in stuck:
        post.promo_status = "uncertain"
    return len(stuck)
