"""Posts deleted on Facebook: marked once, then left out of the nightly sync."""
import pytest

import app as app_module
import core.feedback_loop as feedback_module
from core import fb_client
from core.feedback_loop import learning_phase, update_all_post_metrics

GONE = ("(#10) Object does not exist, cannot be loaded due to missing permission or reviewable "
        "feature, or does not support this operation. This endpoint requires the "
        "'pages_read_engagement' permission or the 'Page Public Content Access' feature.")
FRESH = {"reactions": 7, "comments": 1, "shares": 0, "reach": 300, "impressions": 450}


class Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def graph(monkeypatch):
    """Posts listed in `gone` answer "does not exist"; `readable` is what the page's post list says."""
    state = {"gone": set(), "readable": True, "calls": [], "page_checks": 0}

    def fetch(page_id, access_token, fb_post_id):
        state["calls"].append(fb_post_id)
        if fb_post_id in state["gone"]:
            raise fb_client.PostNotFound(f"Facebook Error: {GONE}")
        return dict(FRESH)

    def readable(page_id, access_token):
        state["page_checks"] += 1
        return state["readable"]

    monkeypatch.setattr(feedback_module, "fetch_post_metrics", fetch)
    monkeypatch.setattr(feedback_module, "page_posts_readable", readable)
    return state


def test_error_does_not_exist_dikenali_sebagai_postingan_hilang(monkeypatch):
    monkeypatch.setattr(fb_client.requests, "get",
                        lambda *a, **k: Resp({"error": {"message": GONE, "code": 10}}))

    with pytest.raises(fb_client.PostNotFound):
        fb_client.fetch_post_metrics("p", "t", "p_1")


def test_error_lain_bukan_postingan_hilang(monkeypatch):
    monkeypatch.setattr(fb_client.requests, "get",
                        lambda *a, **k: Resp({"error": {"message": "Session has expired", "code": 190}}))

    with pytest.raises(fb_client.MetricsUnavailable) as err:
        fb_client.fetch_post_metrics("p", "t", "p_1")
    assert not isinstance(err.value, fb_client.PostNotFound)


def test_daftar_postingan_halaman_terbaca(monkeypatch):
    monkeypatch.setattr(fb_client.requests, "get", lambda *a, **k: Resp({"data": [{"id": "p_9"}]}))
    assert fb_client.page_posts_readable("p", "t") is True

    monkeypatch.setattr(fb_client.requests, "get",
                        lambda *a, **k: Resp({"error": {"message": "(#10) requires pages_read_engagement"}}))
    assert fb_client.page_posts_readable("p", "t") is False


def test_postingan_terhapus_ditandai_lalu_tidak_disinkron_lagi(client, make_page, make_post, topics, db, graph):
    page = make_page("111")
    hilang = make_post(page["id"], topics[0], days_ago=3, reach=900)
    ada = make_post(page["id"], topics[1], days_ago=3, reach=100)
    graph["gone"].add(hilang.fb_post_id)

    update_all_post_metrics(db, "111", "token-111", page["id"])

    db.refresh(hilang)
    assert hilang.fb_removed_at is not None
    assert hilang.metrics[0].reach == 900, "angka terakhir tetap jadi riwayat"
    db.refresh(ada)
    assert ada.fb_removed_at is None and ada.metrics[0].reach == 300

    graph["calls"].clear()
    update_all_post_metrics(db, "111", "token-111", page["id"])
    assert graph["calls"] == [ada.fb_post_id]


def test_izin_hilang_tidak_dianggap_postingan_terhapus(client, make_page, make_post, topics, db, graph):
    """Facebook memberi error yang sama bila token kehilangan pages_read_engagement."""
    page = make_page("111")
    post = make_post(page["id"], topics[0], days_ago=3, reach=900)
    graph["gone"].add(post.fb_post_id)
    graph["readable"] = False

    update_all_post_metrics(db, "111", "token-111", page["id"])

    db.refresh(post)
    assert post.fb_removed_at is None
    graph["calls"].clear()
    update_all_post_metrics(db, "111", "token-111", page["id"])
    assert graph["calls"] == [post.fb_post_id], "dicoba lagi pada sinkron berikutnya"


def test_daftar_postingan_halaman_dicek_sekali_per_sinkron(client, make_page, make_post, topics, db, graph):
    page = make_page("111")
    for t in topics[:5]:
        graph["gone"].add(make_post(page["id"], t, days_ago=3).fb_post_id)

    update_all_post_metrics(db, "111", "token-111", page["id"])

    assert graph["page_checks"] == 1


def test_postingan_terhapus_tanpa_metrik_tidak_menahan_variasi(client, make_page, make_post,
                                                               finish_test_phase, topics, db, graph):
    page = make_page("111")
    finish_test_phase(page["id"], reach=1000, skip={topics[2].id})
    post = make_post(page["id"], topics[2], days_ago=1, with_metrics=False)
    assert learning_phase(db, page["id"])["pending"] == 1

    graph["gone"].add(post.fb_post_id)
    update_all_post_metrics(db, "111", "token-111", page["id"])

    fase = learning_phase(db, page["id"])
    assert (fase["pending"], fase["ready_for_variants"]) == (0, True)


def test_perbarui_manual_postingan_terhapus_memberi_pesan_jelas(client, make_page, make_post,
                                                                 topics, monkeypatch):
    page = make_page("111")
    post = make_post(page["id"], topics[0], days_ago=2, reach=10)

    def gone(*a):
        raise fb_client.PostNotFound(f"Facebook Error: {GONE}")
    monkeypatch.setattr(app_module, "fetch_post_metrics", gone)

    res = client.post(f"/api/posts/{post.id}/metrics/refresh").json()

    assert res["success"] is False and "sudah dihapus" in res["message"]


def test_perbarui_manual_yang_berhasil_mengaktifkan_sinkron_lagi(client, make_page, make_post,
                                                                 topics, db, monkeypatch):
    from datetime import datetime
    page = make_page("111")
    post = make_post(page["id"], topics[0], days_ago=2, reach=10)
    post.fb_removed_at = datetime(2026, 10, 22)
    db.commit()
    assert client.get(f"/api/posts/{post.id}").json()["fb_removed_at"]
    monkeypatch.setattr(app_module, "fetch_post_metrics", lambda *a: dict(FRESH))

    res = client.post(f"/api/posts/{post.id}/metrics/refresh").json()

    assert res["success"] is True
    assert client.get(f"/api/posts/{post.id}").json()["fb_removed_at"] is None
