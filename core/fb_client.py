import os
import requests
import logging
from datetime import datetime, timedelta, timezone
from config import FB_GRAPH_API_VERSION

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = FB_GRAPH_API_VERSION
BASE_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

def test_facebook_credentials(page_id: str, access_token: str) -> dict:
    """
    Validates Facebook Page ID and Access Token.
    Also automatically checks if the provided token is a User Token that has access to pages,
    and retrieves the permanent Page Access Token.
    """
    if not page_id or not access_token:
        return {
            "success": False,
            "message": "Page ID atau Access Token belum diisi."
        }

    try:
        # First, try fetching the page directly
        url = f"{BASE_GRAPH_URL}/{page_id}"
        params = {
            "fields": "id,name,picture{url},fan_count,link",
            "access_token": access_token
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        if "error" in data:
            error_msg = data["error"].get("message", "Unknown Facebook Error")
            # If failed, try checking /me/accounts in case it's a User Token
            accounts_url = f"{BASE_GRAPH_URL}/me/accounts"
            acc_resp = requests.get(accounts_url, params={"access_token": access_token}, timeout=10)
            acc_data = acc_resp.json()
            
            if "data" in acc_data:
                for item in acc_data["data"]:
                    if str(item.get("id")) == str(page_id):
                        return {
                            "success": True,
                            "page_id": item.get("id"),
                            "page_name": item.get("name"),
                            "suggested_page_token": item.get("access_token"),
                            "message": f"Token Pengguna terdeteksi! Ditemukan Page Token permanen untuk: {item.get('name')}"
                        }

            return {
                "success": False,
                "message": f"Facebook Error: {error_msg}"
            }

        page_name = data.get("name", "Unknown Page")
        picture_url = ""
        if "picture" in data and "data" in data["picture"]:
            picture_url = data["picture"]["data"].get("url", "")

        return {
            "success": True,
            "page_id": data.get("id"),
            "page_name": page_name,
            "picture_url": picture_url,
            "link": data.get("link", ""),
            "fan_count": data.get("fan_count", 0),
            "message": f"Terhubung dengan Fanspage: {page_name}"
        }

    except Exception as e:
        logger.error(f"Error testing Facebook credentials: {e}")
        return {
            "success": False,
            "message": f"Koneksi ke Facebook API gagal: {str(e)}"
        }

def publish_photo_to_page(page_id: str, access_token: str, image_path: str, caption: str) -> dict:
    """
    Publishes an image with caption to the Facebook Page using Graph API.
    """
    if not os.path.exists(image_path):
        return {"success": False, "message": f"File gambar tidak ditemukan di path: {image_path}"}

    url = f"{BASE_GRAPH_URL}/{page_id}/photos"
    data = {
        "caption": caption,
        "published": "true",
        "access_token": access_token
    }

    try:
        with open(image_path, "rb") as img_file:
            files = {"source": img_file}
            resp = requests.post(url, data=data, files=files, timeout=60)

        try:
            res_json = resp.json()
        except ValueError:
            logger.error(f"Facebook returned non-JSON response ({resp.status_code}).")
            return {
                "success": False,
                "message": f"Respons Facebook tidak valid (HTTP {resp.status_code})."
            }

        if "error" in res_json:
            err = res_json["error"].get("message", "Unknown Facebook Error")
            logger.error(f"Facebook publish error: {err}")
            return {"success": False, "message": err}

        photo_id = res_json.get("id")
        post_id = res_json.get("post_id", photo_id)
        
        # Build Facebook post URL
        post_url = f"https://www.facebook.com/{post_id}"

        return {
            "success": True,
            "post_id": post_id,
            "photo_id": photo_id,
            "post_url": post_url,
            "message": "Berhasil dipublikasikan ke Facebook!"
        }

    except Exception as e:
        logger.error(f"Failed to publish photo to Facebook: {e}")
        return {"success": False, "message": str(e)}

# (metrics requested, metric used as reach, metric used as total views), newest first.
INSIGHT_METRIC_SETS = (
    ("post_media_view,post_total_media_view_unique", "post_total_media_view_unique", "post_media_view"),
    ("post_impressions,post_impressions_unique", "post_impressions_unique", "post_impressions"),
)


def fetch_post_metrics(page_id: str, access_token: str, fb_post_id: str) -> dict:
    """
    Fetches engagement and reach metrics using dual-layer architecture:
    Layer 1: Instant public reactions, comments, shares
    Layer 2: Insights views and unique viewers (reach), if the token has read_insights
    """
    metrics = {
        "reactions": 0,
        "comments": 0,
        "shares": 0,
        "reach": 0,
        "impressions": 0
    }

    if not fb_post_id:
        return metrics

    # Layer 1: Instant Post Summary
    try:
        url = f"{BASE_GRAPH_URL}/{fb_post_id}"
        params = {
            "fields": "reactions.summary(true),comments.summary(true),shares",
            "access_token": access_token
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if "reactions" in data and "summary" in data["reactions"]:
            metrics["reactions"] = data["reactions"]["summary"].get("total_count", 0)
        if "comments" in data and "summary" in data["comments"]:
            metrics["comments"] = data["comments"]["summary"].get("total_count", 0)
        if "shares" in data:
            metrics["shares"] = data["shares"].get("count", 0)
    except Exception as e:
        logger.warning(f"Error fetching Layer 1 metrics: {e}")

    # Layer 2: Insights. Meta retired post_impressions / post_impressions_unique
    # (they now fail with "must be a valid insights metric") in favour of "views":
    # post_media_view = total views, post_total_media_view_unique = people who saw
    # it (our reach). The old names stay as a fallback for older API versions.
    for metric_names, reach_metric, views_metric in INSIGHT_METRIC_SETS:
        try:
            r = requests.get(f"{BASE_GRAPH_URL}/{fb_post_id}/insights",
                             params={"metric": metric_names, "access_token": access_token},
                             timeout=10)
            insights_data = r.json()
        except Exception as e:
            logger.warning(f"Error fetching Layer 2 insights: {e}")
            break
        if "error" in insights_data:
            logger.warning(f"Insights '{metric_names}' unavailable for {fb_post_id}: "
                           f"{insights_data['error'].get('message')}")
            continue
        for item in insights_data.get("data", []):
            # A metric can come back once per period (lifetime, day, ...): use lifetime.
            if item.get("period") not in (None, "lifetime"):
                continue
            val = (item.get("values") or [{}])[0].get("value", 0) or 0
            if item.get("name") == views_metric:
                metrics["impressions"] = val
            elif item.get("name") == reach_metric:
                metrics["reach"] = val
        break

    # No synthetic reach: if Insights is unreachable (missing read_insights
    # permission), reach stays 0 rather than reporting a fabricated number.
    return metrics


# Graph API error codes meaning the token lacks a permission for this call.
PERMISSION_ERROR_CODES = {10, 200, 210, 230}
COMMENT_PERMISSION_HINT = (
    " Pastikan Page Access Token punya izin pages_read_engagement, "
    "pages_read_user_content, dan pages_manage_engagement."
)


def _graph_error(data: dict) -> str:
    err = data.get("error") or {}
    message = err.get("message", "Unknown Facebook Error")
    if err.get("code") in PERMISSION_ERROR_CODES or "permission" in message.lower():
        message += COMMENT_PERMISSION_HINT
    return message


def fetch_recent_comments(page_id: str, access_token: str, since_days: int = 14,
                          max_posts: int = 25) -> dict:
    """
    Recent posts of the page — published by this app or manually — each with its
    newest top-level comments and who already answered them, in one request.
    """
    since = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
    fields = (
        "id,message,permalink_url,created_time,"
        "comments.order(reverse_chronological).limit(25)"
        "{id,message,created_time,from{id,name},attachment{type},comments.limit(25){from{id}}}"
    )
    try:
        resp = requests.get(
            f"{BASE_GRAPH_URL}/{page_id}/posts",
            params={"fields": fields, "since": since, "limit": max_posts, "access_token": access_token},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch comments for page {page_id}: {e}")
        return {"success": False, "message": f"Koneksi ke Facebook gagal: {e}"}

    if "error" in data:
        return {"success": False, "message": _graph_error(data)}
    return {"success": True, "posts": data.get("data", [])}


def reply_to_comment(comment_id: str, access_token: str, message: str) -> dict:
    """Posts `message` as the page's reply under a comment."""
    try:
        resp = requests.post(
            f"{BASE_GRAPH_URL}/{comment_id}/comments",
            data={"message": message, "access_token": access_token},
            timeout=20,
        )
        data = resp.json()
    except Exception as e:
        logger.error(f"Failed to reply to comment {comment_id}: {e}")
        return {"success": False, "message": f"Koneksi ke Facebook gagal: {e}", "uncertain": True}

    if "error" in data:
        err = data["error"]
        return {
            "success": False,
            "message": _graph_error(data),
            "permission_error": err.get("code") in PERMISSION_ERROR_CODES,
        }
    return {"success": True, "reply_id": data.get("id")}
