"""
Comment auto-reply: answers audience comments on every post of a Fanspage —
whether this app published it or someone posted it by hand — the way a friendly
human admin would, in at most two sentences.

Photo comments (often a prospector showing a rock they found) are answered too:
the model sees the photo, names a visible feature and suggests a test, and never
says a rock is gold or worth anything.

Safety rails:
- the unique comment id in `comment_replies` means a comment is answered once;
- comments by the page itself, already answered by an admin, with neither text
  nor photo (stickers, GIFs), with links, or older than REPLY_LOOKBACK_HOURS are
  left alone;
- an hourly cap per page and a random pause between replies keep the page from
  looking (and being flagged) like a bot.
"""
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.ai_provider import ai_backend, complete_json
from config import DEFAULT_CONTENT_LANGUAGE
from core.fb_client import fetch_recent_comments, reply_to_comment
from core.utils import iso_utc, redact_secrets
from database.models import CommentReply, FacebookPage

logger = logging.getLogger(__name__)

REPLY_MODES = ("auto", "review")
REPLY_LOOKBACK_HOURS = 48        # older comments are not answered: a late reply reads oddly
POST_SCAN_DAYS = 14              # posts this recent are checked for new comments
MAX_REPLIES_PER_RUN = 10         # AI calls per page per run
DEFAULT_MAX_PER_HOUR = 20
MAX_REPLY_CHARS = 300
REPLY_GAP_SECONDS = (15, 45)     # pause between replies in scheduled runs
RECENT_REPLIES_FOR_VARIETY = 8
MAX_PHOTO_BYTES = 8 * 1024 * 1024

LINK_PATTERN = re.compile(r"https?://|www\.|\b[\w-]+\.(com|net|org|id|ly|me|xyz|link)\b|wa\.me", re.I)
UNCERTAIN_NOTE = ("Status tidak pasti: koneksi terputus saat mengirim balasan. "
                  "Periksa komentar di Facebook sebelum mengirim ulang agar tidak dobel.")


# ------------------------------------------------------------------ text helpers


def _parse_time(value) -> datetime | None:
    """Graph API timestamps look like 2026-10-01T08:15:00+0000."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def limit_sentences(text: str, max_sentences: int = 2) -> str:
    """Keeps the first `max_sentences` sentences; a trailing emoji stays with its sentence."""
    parts = []
    for chunk in re.split(r"(?<=[.!?…])\s+", text.strip()):
        if not chunk:
            continue
        if parts and not re.search(r"\w", chunk):
            parts[-1] += " " + chunk          # emoji-only piece belongs to the previous sentence
        else:
            parts.append(chunk)
    return " ".join(parts[:max_sentences])


def clean_reply(text: str) -> str:
    """Strips what makes a reply look automated or spammy, and enforces the length."""
    text = (text or "").strip().strip('"').strip("“”").strip()
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"(^|\s)#\w+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = limit_sentences(text, 2)
    if len(text) > MAX_REPLY_CHARS:
        cut = text[:MAX_REPLY_CHARS].rsplit(" ", 1)[0]
        text = cut.rstrip(",;:-") + "…"
    return text


# ------------------------------------------------------------------ AI


SYSTEM_PROMPT = """Kamu adalah admin Fanspage Facebook "{page_name}", komunitas edukasi pencarian emas dan geologi lapangan. Kamu membalas komentar pengikut seperti manusia sungguhan: hangat, santai, dan singkat.

Aturan balasan:
- MAKSIMAL 2 kalimat pendek. Sering kali 1 kalimat sudah cukup.
{language_rule}
- Tanggapi isi komentarnya secara spesifik. Kalau bertanya, jawab intinya dengan benar secara geologi; kalau tidak yakin, jangan mengarang.
- Jangan terdengar seperti bot atau customer service: hindari "Terima kasih atas komentarnya", "Halo kak, terima kasih sudah...", "Semoga bermanfaat", dan jangan meniru pola pembuka balasan sebelumnya.
- Tanpa hashtag, tanpa tautan, tanpa promosi, jangan menyebut dirimu AI. Emoji paling banyak 1, hanya bila cocok.
- Menyapa nama depan pengomentar boleh sesekali, tidak wajib.
- Jangan menjanjikan apa pun: lokasi pasti emas, jual-beli, harga, atau ajakan bertemu.

Bila komentar menyertakan foto (terlampir):
- Lihat fotonya. Bila memperlihatkan batu, mineral, spesimen, atau hasil dulang, tanggapi seperti sesama penambang yang antusias melihat temuan orang lain: sebut 1 ciri yang benar-benar terlihat di foto (mis. urat kuarsa, noda karat, kristal mengilap, pasir hitam) dan sarankan 1 uji sederhana yang cocok untuk batu itu (mis. uji magnet, uji kekerasan, streak test, dihancurkan lalu didulang, atau assay).
- Boleh menyebut kemungkinan jenisnya dengan kata "kelihatannya" (mis. "kelihatannya amethyst"), tetapi JANGAN PERNAH memastikan itu emas atau meteorit, dan jangan menaksir kadar, nilai, atau harga dari foto.
- Bila fotonya tidak berkaitan (selfie, meme, iklan, gambar tidak jelas), skip=true.

Lewati (skip=true) bila komentar berisi spam, promosi, judi, penipuan, ujaran kebencian atau kata kasar, hanya menandai teman tanpa kata lain, atau memang tidak bisa dibalas dengan wajar.

Balas HANYA dengan JSON: {{"skip": false, "reason": "", "reply": "..."}}"""

LANGUAGE_RULES = {
    "id": "- Ikuti bahasa dan gaya si pengomentar (Indonesia santai, sedikit bahasa daerah, atau Inggris) "
          "serta tingkat formalitasnya. Bila tidak jelas, pakai Bahasa Indonesia santai.",
    "en": "- Audiens halaman ini orang Amerika: SELALU tulis \"reply\" dalam American English yang santai dan natural "
          "(ejaan Amerika, satuan feet/inches/ounces), seperti sesama prospector di AS, apa pun bahasa komentarnya. "
          "Ikuti tingkat formalitas si pengomentar. Hindari juga pembuka kaku ala bot seperti \"Thanks for your comment!\", "
          "\"Great question!\", atau \"Hope this helps\".",
}


def generate_comment_reply(text_ai: dict, page_name: str, language: str, post_message: str,
                           commenter_name: str, comment_message: str,
                           recent_replies: list, photo: tuple | None = None) -> dict:
    """
    Returns {"skip": bool, "reason": str, "reply": str} from the chosen text model.
    `photo` is the comment's attached image as (bytes, mime type).
    """
    lang_rule = LANGUAGE_RULES["id" if language == "id" else "en"]
    recent = "\n".join(f"- {r}" for r in recent_replies) or "- (belum ada)"
    user_prompt = (
        f"Postingan Fanspage:\n\"\"\"{(post_message or '(postingan tanpa teks)')[:600]}\"\"\"\n\n"
        f"Komentar dari {commenter_name or 'seorang pengikut'}:\n\"\"\"{comment_message[:800] or '(tanpa teks)'}\"\"\"\n\n"
        + ("Komentar ini menyertakan foto (terlampir).\n\n" if photo else "")
        + f"Balasan Fanspage sebelumnya (jangan mirip):\n{recent}\n\n"
        "Tulis balasannya."
    )
    raw = complete_json(text_ai["provider"], text_ai["api_key"], text_ai["model"],
                        SYSTEM_PROMPT.format(page_name=page_name or "ini", language_rule=lang_rule),
                        user_prompt, 0.9, reasoning=text_ai.get("reasoning"),
                        images=[photo] if photo else None)
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise RuntimeError("Format balasan dari model AI tidak sesuai.")

    reply = clean_reply(str(data.get("reply") or ""))
    # A model may answer "skip": "false" as a string; bool("false") is True and
    # would silently skip every comment for good.
    skip_raw = data.get("skip")
    skip = (skip_raw.strip().lower() in ("true", "yes", "1") if isinstance(skip_raw, str)
            else bool(skip_raw)) or not reply
    return {"skip": skip, "reason": str(data.get("reason") or "").strip()[:200], "reply": "" if skip else reply}


def _recent_replies(db: Session, page_row_id: int) -> list:
    rows = (db.query(CommentReply.reply_message)
              .filter(CommentReply.page_id == page_row_id, CommentReply.status == "replied")
              .order_by(CommentReply.replied_at.desc())
              .limit(RECENT_REPLIES_FOR_VARIETY).all())
    return [r[0] for r in rows if r[0]]


def replies_sent_last_hour(db: Session, page_row_id: int) -> int:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    return (db.query(CommentReply)
              .filter(CommentReply.page_id == page_row_id,
                      CommentReply.status.in_(("replied", "replying")),
                      CommentReply.replied_at >= since)
              .count())


# ------------------------------------------------------------------ candidates


def comment_photo_url(comment: dict) -> str | None:
    """The image of a photo attached to a comment; stickers and GIFs don't count."""
    attachment = comment.get("attachment") or {}
    if attachment.get("type") != "photo":
        return None
    return ((attachment.get("media") or {}).get("image") or {}).get("src") or None


def download_photo(url: str) -> tuple | None:
    """(bytes, mime type) of a comment photo, or None when it can't be fetched."""
    try:
        with requests.get(url, timeout=20, stream=True) as resp:
            mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if resp.status_code != 200 or not mime.startswith("image/"):
                return None
            data = resp.raw.read(MAX_PHOTO_BYTES + 1, decode_content=True)
    except Exception as e:
        logger.warning(f"[Replies] Comment photo could not be downloaded: {redact_secrets(e)}")
        return None
    if not data or len(data) > MAX_PHOTO_BYTES:
        return None
    return data, mime


def find_new_comments(db: Session, page: FacebookPage, posts: list, now: datetime) -> list:
    """Comments worth answering, oldest first."""
    cutoff = now - timedelta(hours=REPLY_LOOKBACK_HOURS)
    found = []
    for post in posts:
        for c in (post.get("comments") or {}).get("data", []):
            author = c.get("from") or {}
            if str(author.get("id")) == str(page.page_id):
                continue                                   # the page's own comment
            if any(str((r.get("from") or {}).get("id")) == str(page.page_id)
                   for r in (c.get("comments") or {}).get("data", [])):
                continue                                   # an admin already answered it
            message = (c.get("message") or "").strip()
            image_url = comment_photo_url(c)
            if not message and not image_url:
                continue                                   # sticker / GIF only
            created = _parse_time(c.get("created_time"))
            if created and created < cutoff:
                continue
            found.append({
                "fb_comment_id": c["id"],
                "comment_message": message,
                "comment_image_url": image_url,
                "commenter_name": author.get("name"),
                "commenter_id": author.get("id"),
                "comment_created_at": created,
                "fb_post_id": post.get("id"),
                "post_message": post.get("message") or "",
                "post_url": post.get("permalink_url"),
            })

    if not found:
        return []
    handled = {row[0] for row in db.query(CommentReply.fb_comment_id)
               .filter(CommentReply.fb_comment_id.in_([f["fb_comment_id"] for f in found])).all()}
    fresh = [f for f in found if f["fb_comment_id"] not in handled]
    fresh.sort(key=lambda f: f["comment_created_at"] or now)
    return fresh


# ------------------------------------------------------------------ sending


def send_reply(db: Session, page: FacebookPage, row: CommentReply, message: str) -> dict:
    """Posts `row`'s reply. The caller must already have claimed it as 'replying'."""
    res = reply_to_comment(row.fb_comment_id, page.access_token, message)
    if res.get("success"):
        row.status = "replied"
        row.reply_message = message
        row.reply_fb_id = res.get("reply_id")
        row.replied_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.note = None
    else:
        row.status = "failed"
        row.reply_message = message
        row.note = UNCERTAIN_NOTE if res.get("uncertain") else res.get("message")
    db.commit()
    return res


def claim_for_sending(db: Session, reply_id: int, from_statuses: tuple) -> bool:
    """Atomically moves a reply into 'replying' so two clicks can't send it twice."""
    claimed = (db.query(CommentReply)
                 .filter(CommentReply.id == reply_id, CommentReply.status.in_(from_statuses))
                 .update({CommentReply.status: "replying",
                          CommentReply.replied_at: datetime.now(timezone.utc).replace(tzinfo=None)},
                         synchronize_session=False))
    db.commit()
    return claimed == 1


# ------------------------------------------------------------------ main loop


def process_page_comments(db: Session, page: FacebookPage, mode: str | None = None,
                          gap: bool = False) -> dict:
    """
    One pass over a page: fetch new comments, write replies, and send them ('auto')
    or keep them as drafts for approval ('review').
    """
    mode = mode if mode in REPLY_MODES else (page.auto_reply_mode or "auto")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    summary = {"success": True, "replied": 0, "drafted": 0, "skipped": 0, "failed": 0, "message": ""}

    def finish(error: str | None = None) -> dict:
        error = redact_secrets(error) if error else error
        page.reply_last_run = now
        page.reply_last_error = error
        db.commit()
        if error:
            summary["success"] = False
            summary["message"] = error
        elif not summary["message"]:
            parts = [f"{summary['replied']} dibalas" if summary["replied"] else "",
                     f"{summary['drafted']} draft menunggu persetujuan" if summary["drafted"] else "",
                     f"{summary['skipped']} dilewati" if summary["skipped"] else "",
                     f"{summary['failed']} gagal" if summary["failed"] else ""]
            summary["message"] = ", ".join(p for p in parts if p) or "Tidak ada komentar baru untuk dibalas."
        return summary

    if not page.access_token:
        return finish("Fanspage ini belum punya Access Token.")
    text_ai = ai_backend(db, "text")
    if text_ai["missing"]:
        return finish(text_ai["missing"])

    fetched = fetch_recent_comments(page.page_id, page.access_token, POST_SCAN_DAYS)
    if not fetched.get("success"):
        return finish(fetched.get("message"))

    candidates = find_new_comments(db, page, fetched["posts"], now)
    if not candidates:
        return finish()

    hourly_cap = max(1, page.reply_max_per_hour or DEFAULT_MAX_PER_HOUR)
    recent = _recent_replies(db, page.id)
    sent_this_run = 0

    for cand in candidates[:MAX_REPLIES_PER_RUN]:
        if mode == "auto" and replies_sent_last_hour(db, page.id) >= hourly_cap:
            summary["message"] = (f"Batas {hourly_cap} balasan per jam tercapai; "
                                  "sisa komentar dibalas pada putaran berikutnya.")
            break

        photo = download_photo(cand["comment_image_url"]) if cand.get("comment_image_url") else None
        if LINK_PATTERN.search(cand["comment_message"]):
            decision = {"skip": True, "reason": "Berisi tautan (kemungkinan spam).", "reply": ""}
        elif not cand["comment_message"] and not photo:
            decision = {"skip": True, "reason": "Foto di komentar tidak bisa dimuat.", "reply": ""}
        else:
            try:
                decision = generate_comment_reply(
                    text_ai, page.name, page.content_language or DEFAULT_CONTENT_LANGUAGE, cand["post_message"],
                    cand["commenter_name"], cand["comment_message"], recent, photo=photo)
            except Exception as e:
                # Usually quota or a bad key: stop here and retry these comments next run.
                logger.warning(f"[Replies] AI failed for '{page.name}': {e}")
                return finish(f"Gagal menyusun balasan: {e}")

        row = CommentReply(page_id=page.id, created_at=now, **cand)
        if decision["skip"]:
            row.status, row.note = "skipped", decision["reason"] or "Tidak perlu dibalas."
        else:
            row.reply_message = decision["reply"]
            row.status = "replying" if mode == "auto" else "pending"
            if mode == "auto":
                row.replied_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(row)
        try:
            db.commit()                     # the unique comment id is the reply-once lock
        except IntegrityError:
            db.rollback()
            continue                        # another run got to this comment first

        if row.status == "skipped":
            summary["skipped"] += 1
            continue
        if row.status == "pending":
            summary["drafted"] += 1
            continue

        if gap and sent_this_run:
            time.sleep(random.uniform(*REPLY_GAP_SECONDS))
        res = send_reply(db, page, row, decision["reply"])
        if res.get("success"):
            summary["replied"] += 1
            sent_this_run += 1
            recent.insert(0, decision["reply"])
            recent = recent[:RECENT_REPLIES_FOR_VARIETY]
        else:
            summary["failed"] += 1
            if res.get("permission_error"):
                return finish(res.get("message"))

    return finish()


def recover_interrupted_replies(db: Session) -> int:
    """At startup: a reply left in 'replying' may or may not have reached Facebook."""
    stuck = db.query(CommentReply).filter(CommentReply.status == "replying").all()
    for row in stuck:
        row.status = "failed"
        row.note = UNCERTAIN_NOTE
    return len(stuck)


def serialize_reply(row: CommentReply) -> dict:
    return {
        "id": row.id,
        "page_id": row.page_id,
        "fb_post_id": row.fb_post_id,
        "post_message": (row.post_message or "")[:300],
        "post_url": row.post_url,
        "fb_comment_id": row.fb_comment_id,
        "comment_message": row.comment_message,
        "comment_image_url": row.comment_image_url,
        "commenter_name": row.commenter_name or "Pengikut",
        "comment_created_at": iso_utc(row.comment_created_at),
        "reply_message": row.reply_message or "",
        "status": row.status,
        "note": row.note,
        "created_at": iso_utc(row.created_at),
        "replied_at": iso_utc(row.replied_at),
    }
