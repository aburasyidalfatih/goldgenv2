"""Facebook post metrics: Meta retired post_impressions*, reach must come from the views metrics."""
from core import fb_client


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _fake_graph(insights_by_metric, summary=None):
    calls = []

    def get(url, params, timeout):
        calls.append(params.get("metric") or params.get("fields"))
        if url.endswith("/insights"):
            return Resp(insights_by_metric.get(params["metric"],
                                               {"error": {"message": "(#100) The value must be a valid insights metric"}}))
        return Resp(summary or {"reactions": {"summary": {"total_count": 12}},
                                "comments": {"summary": {"total_count": 3}},
                                "shares": {"count": 2}})
    return get, calls


NEW = "post_media_view,post_total_media_view_unique"
OLD = "post_impressions,post_impressions_unique"


def test_reach_dari_metrik_views_baru_memakai_periode_lifetime(monkeypatch):
    get, calls = _fake_graph({NEW: {"data": [
        {"name": "post_media_view", "period": "lifetime", "values": [{"value": 115}]},
        {"name": "post_total_media_view_unique", "period": "lifetime", "values": [{"value": 75}]},
        {"name": "post_total_media_view_unique", "period": "day", "values": [{"value": 0}]},
    ]}})
    monkeypatch.setattr(fb_client.requests, "get", get)

    m = fb_client.fetch_post_metrics("p", "t", "p_1")

    assert m == {"reactions": 12, "comments": 3, "shares": 2, "reach": 75, "impressions": 115}
    assert OLD not in calls, "metrik lama tidak perlu dicoba bila yang baru berhasil"


def test_mundur_ke_metrik_lama_bila_versi_api_belum_kenal_yang_baru(monkeypatch):
    get, calls = _fake_graph({OLD: {"data": [
        {"name": "post_impressions", "period": "lifetime", "values": [{"value": 900}]},
        {"name": "post_impressions_unique", "period": "lifetime", "values": [{"value": 640}]},
    ]}})
    monkeypatch.setattr(fb_client.requests, "get", get)

    m = fb_client.fetch_post_metrics("p", "t", "p_1")

    assert (m["reach"], m["impressions"]) == (640, 900)
    assert calls.index(NEW) < calls.index(OLD)


def test_tanpa_izin_insights_reach_tetap_nol_bukan_karangan(monkeypatch):
    get, _ = _fake_graph({})
    monkeypatch.setattr(fb_client.requests, "get", get)

    m = fb_client.fetch_post_metrics("p", "t", "p_1")

    assert (m["reach"], m["impressions"]) == (0, 0)
    assert m["reactions"] == 12


def test_perbarui_metrik_satu_postingan_dari_facebook(client, make_page, make_post, topics, monkeypatch):
    import app as app_module
    page = make_page("111")
    post = make_post(page["id"], topics[0], days_ago=2, reach=10)
    monkeypatch.setattr(app_module, "fetch_post_metrics", lambda page_id, token, fb_id:
                        {"reactions": 12, "comments": 3, "shares": 2, "reach": 75, "impressions": 115})

    res = client.post(f"/api/posts/{post.id}/metrics/refresh").json()

    assert res["success"] is True
    assert {k: res["metrics"][k] for k in ("reactions", "comments", "shares", "reach", "impressions")} == \
        {"reactions": 12, "comments": 3, "shares": 2, "reach": 75, "impressions": 115}
    assert res["metrics"]["last_checked_at"]
    detail = client.get(f"/api/posts/{post.id}").json()["metrics"]
    assert (detail["reach"], detail["impressions"]) == (75, 115)


def test_draft_belum_tayang_tidak_bisa_diperbarui(client, make_page, make_post, topics):
    page = make_page("111")
    draft = make_post(page["id"], topics[0], status="ready")
    res = client.post(f"/api/posts/{draft.id}/metrics/refresh").json()
    assert res["success"] is False and "belum tayang" in res["message"]
