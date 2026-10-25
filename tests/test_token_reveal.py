"""Lihat Access Token Fanspage: hanya setelah password login dimasukkan ulang."""
from core import auth
from tests.conftest import TEST_PASSWORD


def test_token_hanya_tampil_dengan_password_login(client, make_page):
    p = make_page("111")
    assert client.get("/api/pages").json()["pages"][0]["access_token"] != "token-111"   # tetap disamarkan

    salah = client.post(f"/api/pages/{p['id']}/token/reveal", json={"password": "bukan"}).json()
    assert salah["success"] is False and "token" not in salah

    res = client.post(f"/api/pages/{p['id']}/token/reveal", json={"password": TEST_PASSWORD})
    assert res.json() == {"success": True, "token": "token-111"}
    assert res.headers["cache-control"] == "no-store"


def test_terlalu_banyak_password_salah_dikunci(client, make_page):
    p = make_page("111")
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        client.post(f"/api/pages/{p['id']}/token/reveal", json={"password": "bukan"})

    res = client.post(f"/api/pages/{p['id']}/token/reveal", json={"password": TEST_PASSWORD})
    assert res.status_code == 429 and "token" not in res.json()
    auth.login_throttle._failures.clear()


def test_tanpa_login_ditolak(client, make_page):
    from fastapi.testclient import TestClient
    import app as app_module

    p = make_page("111")
    res = TestClient(app_module.app).post(f"/api/pages/{p['id']}/token/reveal", json={"password": TEST_PASSWORD})
    assert res.status_code == 401
