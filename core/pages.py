"""
Fanspage management: add, verify, update and remove the pages this app posts to.

Each page carries its own credentials, schedule, content preferences and learning
state, so pages never share tokens or audiences.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from config import SECRET_MASK, DEFAULT_CONTENT_LANGUAGE, POSTER_RATIOS, normalize_aspect_ratio
from core.utils import iso_utc
from core.poster_style import THEMES, normalize_theme
from database.models import ContentTopic, FacebookPage, Post, PageTopicWeight, CommentReply
from core.fb_client import test_facebook_credentials
from core.promo_comment import normalize_url
from core.instagram import link_instagram

logger = logging.getLogger(__name__)


def serialize_page(page: FacebookPage, db: Session | None = None) -> dict:
    """Page data for the UI. The access token is never exposed in cleartext."""
    published = failed = pending_replies = 0
    last_promo = None
    if db is not None:
        row = (db.query(Post.promo_comment)
                 .filter(Post.page_id == page.id, Post.promo_status == "posted")
                 .order_by(Post.published_at.desc()).first())
        last_promo = row[0] if row else None
        published = db.query(Post).filter(Post.page_id == page.id, Post.status == "published").count()
        failed = db.query(Post).filter(Post.page_id == page.id, Post.status == "failed").count()
        pending_replies = (db.query(CommentReply)
                             .filter(CommentReply.page_id == page.id, CommentReply.status == "pending")
                             .count())

    return {
        "id": page.id,
        "page_id": page.page_id,
        "name": page.name,
        "picture_url": page.picture_url,
        "link": page.link,
        "fan_count": page.fan_count or 0,
        "has_token": bool(page.access_token),
        "access_token": SECRET_MASK if page.access_token else "",
        "content_language": page.content_language or DEFAULT_CONTENT_LANGUAGE,
        "aspect_ratio": normalize_aspect_ratio(page.aspect_ratio),
        "color_theme": normalize_theme(page.color_theme),
        "auto_post_times": page.auto_post_times or "10:00,19:00",
        "autopilot_enabled": bool(page.autopilot_enabled),
        "is_active": page.is_active is not False,
        "token_status": page.token_status,
        "last_verified_at": iso_utc(page.last_verified_at),
        "published_count": published,
        "failed_count": failed,
        "auto_reply_enabled": bool(page.auto_reply_enabled),
        "auto_reply_mode": page.auto_reply_mode or "auto",
        "reply_max_per_hour": page.reply_max_per_hour or 20,
        "reply_last_run": iso_utc(page.reply_last_run),
        "reply_last_error": page.reply_last_error,
        "pending_replies": pending_replies,
        "promo_enabled": bool(page.promo_enabled),
        "promo_url": page.promo_url or "",
        "promo_note": page.promo_note or "",
        "promo_last_error": page.promo_last_error,
        "promo_last_comment": last_promo,
        "ig_enabled": bool(page.ig_enabled),
        "ig_username": page.ig_username or "",
        "ig_last_error": page.ig_last_error,
    }


def list_pages(db: Session, include_inactive: bool = True) -> list:
    query = db.query(FacebookPage)
    if not include_inactive:
        query = query.filter(FacebookPage.is_active.isnot(False))
    pages = query.order_by(FacebookPage.created_at.asc()).all()
    return [serialize_page(p, db) for p in pages]


def get_page(db: Session, page_row_id: int) -> FacebookPage | None:
    return db.query(FacebookPage).filter(FacebookPage.id == page_row_id).first()


def active_pages(db: Session) -> list:
    """Pages eligible for autopilot: active, with credentials and autopilot on."""
    return [
        p for p in db.query(FacebookPage).filter(FacebookPage.is_active.isnot(False)).all()
        if p.access_token and p.autopilot_enabled
    ]


def default_page(db: Session) -> FacebookPage | None:
    """The page the UI falls back to when none is selected."""
    return (
        db.query(FacebookPage)
        .filter(FacebookPage.is_active.isnot(False))
        .order_by(FacebookPage.created_at.asc())
        .first()
    )


def verify_page_credentials(page_id: str, access_token: str) -> dict:
    """Checks credentials against the Graph API without storing anything."""
    return test_facebook_credentials(page_id, access_token)


def add_page(db: Session, page_id: str, access_token: str, verify: bool = True) -> dict:
    page_id = (page_id or "").strip()
    access_token = (access_token or "").strip()
    if not page_id or not access_token:
        return {"success": False, "message": "Page ID dan Access Token wajib diisi."}

    existing = db.query(FacebookPage).filter(FacebookPage.page_id == page_id).first()
    if existing:
        return {
            "success": False,
            "message": f"Fanspage dengan ID {page_id} sudah terdaftar sebagai '{existing.name}'.",
        }

    name, picture, link, fans = f"Fanspage {page_id}", None, None, 0
    status = "Belum diverifikasi"

    if verify:
        result = verify_page_credentials(page_id, access_token)
        if not result.get("success"):
            return {"success": False, "message": result.get("message", "Verifikasi gagal.")}
        # A user token can carry the real page token; prefer it.
        if result.get("suggested_page_token"):
            access_token = result["suggested_page_token"]
        # Keep Facebook's numeric id even when a username was typed: comment
        # authors are matched against it to recognise the page's own replies.
        canonical = str(result.get("page_id") or page_id)
        if canonical != page_id:
            existing = db.query(FacebookPage).filter(FacebookPage.page_id == canonical).first()
            if existing:
                return {
                    "success": False,
                    "message": f"Fanspage dengan ID {canonical} sudah terdaftar sebagai '{existing.name}'.",
                }
            page_id = canonical
        name = result.get("page_name") or name
        picture = result.get("picture_url")
        link = result.get("link")
        fans = result.get("fan_count", 0) or 0
        status = "Terverifikasi"

    page = FacebookPage(
        page_id=page_id,
        name=name,
        access_token=access_token,
        picture_url=picture,
        link=link,
        fan_count=fans,
        token_status=status,
        last_verified_at=datetime.now(timezone.utc) if verify else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(page)
    db.commit()
    db.refresh(page)
    logger.info(f"[Pages] Added Fanspage '{page.name}' ({page.page_id}).")
    return {"success": True, "message": f"Fanspage '{page.name}' berhasil ditambahkan.", "page": serialize_page(page, db)}


def update_page(db: Session, page_row_id: int, payload: dict) -> dict:
    page = get_page(db, page_row_id)
    if not page:
        return {"success": False, "message": "Fanspage tidak ditemukan."}

    editable = {
        "name": str,
        "content_language": str,
        "aspect_ratio": str,
        "auto_post_times": str,
        "autopilot_enabled": bool,
        "is_active": bool,
    }
    if payload.get("aspect_ratio") is not None and payload["aspect_ratio"] not in POSTER_RATIOS:
        return {"success": False, "message": "Rasio poster harus 4:5 atau 1:1."}
    for key, caster in editable.items():
        if key in payload and payload[key] is not None:
            setattr(page, key, caster(payload[key]) if caster is not bool else bool(payload[key]))

    if payload.get("color_theme") is not None:
        if payload["color_theme"] not in THEMES:
            return {"success": False, "message": "Tema warna tidak dikenal."}
        page.color_theme = payload["color_theme"]

    # Comment auto-reply preferences
    if payload.get("auto_reply_enabled") is not None:
        page.auto_reply_enabled = bool(payload["auto_reply_enabled"])
    if payload.get("auto_reply_mode") is not None:
        if payload["auto_reply_mode"] not in ("auto", "review"):
            return {"success": False, "message": "Mode balasan harus 'auto' atau 'review'."}
        page.auto_reply_mode = payload["auto_reply_mode"]
    if payload.get("reply_max_per_hour") is not None:
        try:
            page.reply_max_per_hour = max(1, min(60, int(payload["reply_max_per_hour"])))
        except (TypeError, ValueError):
            return {"success": False, "message": "Batas balasan per jam harus berupa angka 1–60."}

    # First-comment promotion
    if payload.get("promo_url") is not None:
        url = normalize_url(str(payload["promo_url"]))
        if url and (" " in url or "." not in url):
            return {"success": False, "message": "Alamat web promosi tidak valid. Contoh: https://firstflake.com/"}
        page.promo_url = url
    if payload.get("promo_note") is not None:
        page.promo_note = str(payload["promo_note"]).strip()[:500]
    if payload.get("promo_enabled") is not None:
        enabled = bool(payload["promo_enabled"])
        if enabled and not page.promo_url:
            return {"success": False, "message": "Isi alamat web yang dipromosikan sebelum menyalakan komentar promosi."}
        page.promo_enabled = enabled
        if enabled:
            page.promo_last_error = None

    # Instagram cross-posting
    if payload.get("ig_enabled") is not None:
        enabled = bool(payload["ig_enabled"])
        if enabled and not page.ig_enabled:
            if not page.access_token:
                return {"success": False, "message": "Fanspage ini belum punya Access Token."}
            res = link_instagram(db, page)
            if not res.get("success"):
                db.commit()
                return {"success": False, "message": res.get("message")}
            page.ig_enabled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        page.ig_enabled = enabled

    # A new token is only stored when it is not the mask placeholder.
    new_token = payload.get("access_token")
    if new_token and new_token != SECRET_MASK:
        page.access_token = new_token.strip()
        page.token_status = "Belum diverifikasi"

    db.commit()
    db.refresh(page)
    return {"success": True, "message": "Pengaturan Fanspage disimpan.", "page": serialize_page(page, db)}


def reverify_page(db: Session, page_row_id: int) -> dict:
    page = get_page(db, page_row_id)
    if not page:
        return {"success": False, "message": "Fanspage tidak ditemukan."}
    if not page.access_token:
        return {"success": False, "message": "Fanspage ini belum punya Access Token."}

    result = verify_page_credentials(page.page_id, page.access_token)
    if not result.get("success"):
        page.token_status = "Token bermasalah"
        db.commit()
        return {"success": False, "message": result.get("message", "Verifikasi gagal."), "page": serialize_page(page, db)}

    if result.get("suggested_page_token"):
        page.access_token = result["suggested_page_token"]
    # Pages added by username before ids were normalised: switch to the numeric id.
    canonical = str(result.get("page_id") or page.page_id)
    if canonical != page.page_id and not (
        db.query(FacebookPage).filter(FacebookPage.page_id == canonical, FacebookPage.id != page.id).first()
    ):
        page.page_id = canonical
    page.name = result.get("page_name") or page.name
    page.picture_url = result.get("picture_url") or page.picture_url
    page.link = result.get("link") or page.link
    page.fan_count = result.get("fan_count", page.fan_count) or 0
    page.token_status = "Terverifikasi"
    page.last_verified_at = datetime.now(timezone.utc)
    if page.ig_enabled:
        link_instagram(db, page)        # account may have been relinked
    db.commit()
    db.refresh(page)
    return {"success": True, "message": f"Terhubung: {page.name}", "page": serialize_page(page, db)}


def delete_page(db: Session, page_row_id: int) -> dict:
    """
    Removes a page. Its published posts are kept as history but detached, so the
    reach numbers you already earned are never silently deleted.
    """
    page = get_page(db, page_row_id)
    if not page:
        return {"success": False, "message": "Fanspage tidak ditemukan."}

    published = db.query(Post).filter(Post.page_id == page.id, Post.status == "published").count()
    name = page.name

    db.query(PageTopicWeight).filter(PageTopicWeight.page_id == page.id).delete()
    db.query(CommentReply).filter(CommentReply.page_id == page.id).delete()
    db.query(Post).filter(Post.page_id == page.id).update({Post.page_id: None})
    # Its variants become shared instead of belonging to no page at all.
    db.query(ContentTopic).filter(ContentTopic.origin_page_id == page.id).update(
        {ContentTopic.origin_page_id: None})
    db.delete(page)
    db.commit()

    note = f" {published} postingan yang sudah tayang tetap tersimpan sebagai riwayat." if published else ""
    logger.info(f"[Pages] Deleted Fanspage '{name}'.")
    return {"success": True, "message": f"Fanspage '{name}' dihapus.{note}"}
