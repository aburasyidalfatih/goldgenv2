"""
Deleting all of a Fanspage's own posts on Facebook with its stored token
(temporary tool, used from the Fanspage tab and scripts/hapus_postingan_fanspage.py).

- Only the page's own posts (/posts), made by this app or manually; visitor
  posts are left alone. Needs pages_manage_posts. Cannot be undone.
- Instagram/Threads copies and the app's own history (posts, reach, learning)
  are not touched.
- Runs in a background thread, one run per page at a time; progress lives in
  memory, so a restart simply stops it (running it again continues).
"""
import logging
import threading
import time
from datetime import datetime, timezone

import requests

from core.fb_client import BASE_GRAPH_URL
from core.utils import iso_utc, redact_secrets

logger = logging.getLogger(__name__)

DELETE_PAUSE_SECONDS = 1.0
# Facebook rate limits: stop, the user runs it again later.
RATE_LIMIT_CODES = {4, 17, 32, 613}

_runs: dict = {}            # page row id -> progress
_lock = threading.Lock()


def list_page_posts(page_id: str, access_token: str) -> dict:
    """Every post of the page itself, newest first."""
    url = f"{BASE_GRAPH_URL}/{page_id}/posts"
    params = {"fields": "id,created_time,message", "limit": 100, "access_token": access_token}
    posts = []
    try:
        while url:
            data = requests.get(url, params=params, timeout=30).json()
            if "error" in data:
                return {"success": False,
                        "message": f"Gagal mengambil daftar postingan: {redact_secrets(data['error'].get('message'))}"}
            posts.extend(data.get("data", []))
            url, params = (data.get("paging") or {}).get("next"), None   # 'next' carries its own parameters
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Facebook gagal: {redact_secrets(e)}"}
    return {"success": True, "posts": posts}


def delete_post(post_id: str, access_token: str) -> dict:
    try:
        data = requests.delete(f"{BASE_GRAPH_URL}/{post_id}",
                               params={"access_token": access_token}, timeout=30).json()
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Facebook gagal: {redact_secrets(e)}"}
    if data.get("success"):
        return {"success": True}
    err = data.get("error") or {}
    return {"success": False, "message": redact_secrets(err.get("message") or data),
            "rate_limited": err.get("code") in RATE_LIMIT_CODES}


def delete_all_posts(page_id: str, access_token: str, posts: list, progress: dict | None = None,
                     sleep=time.sleep, on_item=None) -> dict:
    """Deletes `posts` one by one, updating `progress` as it goes."""
    progress = progress if progress is not None else {}
    progress.update(total=len(posts), deleted=0, failed=0, message="")
    for i, post in enumerate(posts, 1):
        res = delete_post(post["id"], access_token)
        if res.get("success"):
            progress["deleted"] += 1
        else:
            progress["failed"] += 1
            progress["message"] = res.get("message") or ""
            if res.get("rate_limited"):
                progress["message"] = "Batas laju Facebook tercapai. Tunggu sebentar lalu jalankan lagi."
                if on_item:
                    on_item(i, post, res)
                break
        if on_item:
            on_item(i, post, res)
        sleep(DELETE_PAUSE_SECONDS)
    if progress["failed"] and "permission" in (progress["message"] or "").lower():
        progress["message"] = progress["message"].rstrip(". ") + ". Token kemungkinan belum punya izin pages_manage_posts."
    return progress


def status(page_row_id: int) -> dict:
    with _lock:
        return dict(_runs.get(page_row_id) or {"running": False})


def start(page_row_id: int, page_id: str, access_token: str, name: str) -> dict:
    """Starts deleting in the background. Refuses while a run for the page is going."""
    with _lock:
        if (_runs.get(page_row_id) or {}).get("running"):
            return {"success": False, "message": "Penghapusan untuk Fanspage ini sedang berjalan."}
        _runs[page_row_id] = {"running": True, "total": None, "deleted": 0, "failed": 0, "message": "",
                              "started_at": iso_utc(datetime.now(timezone.utc))}

    def run():
        progress = _runs[page_row_id]
        try:
            listed = list_page_posts(page_id, access_token)
            if not listed.get("success"):
                progress["message"] = listed["message"]
                return
            delete_all_posts(page_id, access_token, listed["posts"], progress)
            logger.info(f"[Cleanup] '{name}': {progress['deleted']} post(s) deleted, {progress['failed']} failed.")
        except Exception as e:
            progress["message"] = redact_secrets(e)
            logger.error(f"[Cleanup] '{name}': {progress['message']}")
        finally:
            progress["running"] = False
            progress["finished_at"] = iso_utc(datetime.now(timezone.utc))

    threading.Thread(target=run, name=f"cleanup-{page_row_id}", daemon=True).start()
    return {"success": True, "message": f"Penghapusan semua postingan '{name}' dimulai."}
