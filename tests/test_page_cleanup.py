"""Alat sementara: hapus semua postingan Fanspage di Facebook memakai token yang tersimpan."""
import time

import pytest

from core import page_cleanup as pc


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def fb(monkeypatch):
    state = {"posts": ["111_1", "111_2", "111_3"], "deleted": [], "errors": {}}

    def get(url, params=None, timeout=None):
        if "after=" in url:
            return _R({"data": [{"id": i} for i in state["posts"][2:]]})
        assert url.endswith("/111/posts") and params["access_token"] == "token-111"
        return _R({"data": [{"id": i} for i in state["posts"][:2]],
                   "paging": {"next": "https://graph.facebook.com/111/posts?after=2"}})

    def delete(url, params=None, timeout=None):
        post_id = url.rsplit("/", 1)[-1]
        if post_id in state["errors"]:
            return _R({"error": state["errors"][post_id]})
        state["deleted"].append(post_id)
        return _R({"success": True})

    monkeypatch.setattr(pc.requests, "get", get)
    monkeypatch.setattr(pc.requests, "delete", delete)
    monkeypatch.setattr(pc, "DELETE_PAUSE_SECONDS", 0)
    return state


def _tunggu(client, page_id):
    for _ in range(100):
        st = client.get(f"/api/pages/{page_id}/fb-posts/delete-status").json()
        if not st.get("running"):
            return st
        time.sleep(0.05)
    raise AssertionError("penghapusan tidak selesai")


def test_menghitung_semua_postingan_termasuk_halaman_berikutnya(client, make_page, fb):
    p = make_page("111")
    assert client.get(f"/api/pages/{p['id']}/fb-posts/count").json() == {"success": True, "count": 3}
    assert fb["deleted"] == []


def test_nama_salah_tidak_menghapus_apa_pun(client, make_page, fb):
    p = make_page("111")
    res = client.post(f"/api/pages/{p['id']}/fb-posts/delete-all", json={"confirm_name": "salah"}).json()
    assert res["success"] is False and fb["deleted"] == []


def test_menghapus_semua_postingan_dan_melaporkan_kemajuan(client, make_page, fb):
    p = make_page("111")
    fb["errors"]["111_2"] = {"message": "Permissions error", "code": 200}

    res = client.post(f"/api/pages/{p['id']}/fb-posts/delete-all", json={"confirm_name": p["name"]}).json()
    assert res["success"], res
    st = _tunggu(client, p["id"])

    assert fb["deleted"] == ["111_1", "111_3"]
    assert (st["total"], st["deleted"], st["failed"]) == (3, 2, 1)
    assert "pages_manage_posts" in st["message"]


def test_batas_laju_menghentikan_penghapusan(fb):
    fb["errors"]["111_1"] = {"message": "Application request limit reached", "code": 4}
    progress = pc.delete_all_posts("111", "token-111", [{"id": i} for i in fb["posts"]], sleep=lambda s: None)
    assert fb["deleted"] == [] and progress["failed"] == 1 and "Batas laju" in progress["message"]
