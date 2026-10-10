"""
Threads API client (graph.threads.net). Threads uses its own user access token,
not the Facebook Page token: it comes from a Meta app with the "Access the
Threads API" use case and needs threads_basic + threads_content_publish.

Long-lived tokens last 60 days and are refreshed before they lapse. Like
Instagram, Threads downloads the image itself from a public HTTPS URL.
"""
import logging
import os
import time

import requests

from core.utils import redact_secrets

logger = logging.getLogger(__name__)

THREADS_BASE = "https://graph.threads.net"
THREADS_API_VERSION = os.environ.get("THREADS_API_VERSION", "v1.0")
THREADS_GRAPH_URL = f"{THREADS_BASE}/{THREADS_API_VERSION}"

# Error codes meaning the token is invalid/expired (190) or lacks a permission.
THREADS_TOKEN_ERROR_CODES = {190}
THREADS_PERMISSION_ERROR_CODES = {10, 200, 210, 230}
THREADS_PERMISSION_HINT = (
    " Pastikan token Threads masih berlaku dan punya izin threads_basic serta threads_content_publish."
)
CONTAINER_POLL_SECONDS = 3
CONTAINER_POLL_TRIES = 20


def _error(data: dict) -> str:
    err = data.get("error") or {}
    message = err.get("error_user_msg") or err.get("message") or "Unknown Threads Error"
    code = err.get("code")
    if code in THREADS_PERMISSION_ERROR_CODES | THREADS_TOKEN_ERROR_CODES or "permission" in message.lower():
        message += THREADS_PERMISSION_HINT
    return redact_secrets(message)


def _blocked(data: dict) -> bool:
    """The token itself is the problem: retrying the next post would fail the same way."""
    return (data.get("error") or {}).get("code") in THREADS_PERMISSION_ERROR_CODES | THREADS_TOKEN_ERROR_CODES


def get_threads_profile(access_token: str) -> dict:
    """The Threads account the token belongs to."""
    try:
        data = requests.get(f"{THREADS_GRAPH_URL}/me",
                            params={"fields": "id,username", "access_token": access_token},
                            timeout=15).json()
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Threads gagal: {redact_secrets(e)}"}
    if "error" in data or not data.get("id"):
        return {"success": False, "message": _error(data)}
    return {"success": True, "threads_user_id": str(data["id"]), "username": data.get("username") or ""}


def exchange_long_lived_token(short_token: str, app_secret: str) -> dict:
    """Short-lived (1 hour) token -> long-lived (60 days). Needs the Meta app secret."""
    try:
        data = requests.get(f"{THREADS_BASE}/access_token",
                            params={"grant_type": "th_exchange_token", "client_secret": app_secret,
                                    "access_token": short_token},
                            timeout=15).json()
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Threads gagal: {redact_secrets(e)}"}
    if "error" in data or not data.get("access_token"):
        return {"success": False, "message": _error(data)}
    return {"success": True, "access_token": data["access_token"], "expires_in": data.get("expires_in")}


def refresh_long_lived_token(access_token: str) -> dict:
    """A long-lived token at least 24 hours old, renewed for another 60 days."""
    try:
        data = requests.get(f"{THREADS_BASE}/refresh_access_token",
                            params={"grant_type": "th_refresh_token", "access_token": access_token},
                            timeout=15).json()
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Threads gagal: {redact_secrets(e)}"}
    if "error" in data or not data.get("access_token"):
        return {"success": False, "message": _error(data), "token_error": _blocked(data)}
    return {"success": True, "access_token": data["access_token"], "expires_in": data.get("expires_in")}


def publish_to_threads(threads_user_id: str, access_token: str, text: str, image_url: str | None = None,
                       reply_to_id: str | None = None, sleep=time.sleep) -> dict:
    """
    Two steps, like Instagram: create a container (an image post, or a text reply
    under `reply_to_id`), wait until Threads has processed it, then publish it.
    Only the publish call makes it public, so a failure before it leaves nothing.
    """
    data = {"text": text, "access_token": access_token}
    if image_url:
        data.update(media_type="IMAGE", image_url=image_url)
    else:
        data["media_type"] = "TEXT"
    if reply_to_id:
        data["reply_to_id"] = reply_to_id
    try:
        container = requests.post(f"{THREADS_GRAPH_URL}/{threads_user_id}/threads", data=data, timeout=60).json()
    except Exception as e:
        return {"success": False, "message": f"Koneksi ke Threads gagal: {redact_secrets(e)}"}
    if "error" in container or not container.get("id"):
        return {"success": False, "message": _error(container), "permission_error": _blocked(container)}
    creation_id = container["id"]

    status, detail = "", ""
    for _ in range(CONTAINER_POLL_TRIES):
        try:
            res = requests.get(f"{THREADS_GRAPH_URL}/{creation_id}",
                               params={"fields": "status,error_message", "access_token": access_token},
                               timeout=15).json()
            status, detail = res.get("status") or "", res.get("error_message") or ""
        except Exception:
            status = ""
        if status in ("FINISHED", "ERROR", "EXPIRED", "PUBLISHED"):
            break
        sleep(CONTAINER_POLL_SECONDS)
    if status != "FINISHED":
        note = f" ({detail})" if detail else ""
        return {"success": False,
                "message": f"Threads belum selesai memproses postingan (status: {status or 'tidak diketahui'}){note}."}

    try:
        published = requests.post(f"{THREADS_GRAPH_URL}/{threads_user_id}/threads_publish",
                                  data={"creation_id": creation_id, "access_token": access_token},
                                  timeout=60).json()
    except Exception as e:
        # The post may or may not be live now.
        return {"success": False, "message": f"Koneksi ke Threads gagal: {redact_secrets(e)}", "uncertain": True}
    if "error" in published or not published.get("id"):
        return {"success": False, "message": _error(published), "permission_error": _blocked(published)}

    media_id = str(published["id"])
    permalink = None
    if reply_to_id:
        return {"success": True, "media_id": media_id, "permalink": None}
    try:
        permalink = requests.get(f"{THREADS_GRAPH_URL}/{media_id}",
                                 params={"fields": "permalink", "access_token": access_token},
                                 timeout=15).json().get("permalink")
    except Exception:
        pass
    return {"success": True, "media_id": media_id, "permalink": permalink}
