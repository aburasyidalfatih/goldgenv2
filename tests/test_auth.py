"""Login: nothing in the dashboard is reachable without a session."""
from fastapi.testclient import TestClient

import app as app_module
from core import auth
from database.models import User
from tests.conftest import TEST_EMAIL, TEST_PASSWORD


def test_tanpa_login_api_ditolak_dan_halaman_dialihkan(client):
    anon = TestClient(app_module.app)

    assert anon.get("/api/settings").status_code == 401
    assert anon.post("/api/generate", json={}).status_code == 401
    assert anon.get("/storage/generated_images/apa.jpg").status_code == 401
    res = anon.get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200
    assert anon.get("/static/js/app.js").status_code == 200


def test_login_benar_membuka_dashboard(client):
    anon = TestClient(app_module.app)
    res = anon.post("/api/auth/login", json={"email": "  ADMIN@test.local ", "password": TEST_PASSWORD})

    assert res.json()["success"] is True
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/settings").status_code == 200
    assert anon.get("/api/auth/me").json()["email"] == TEST_EMAIL


def test_password_salah_dan_email_asing_pesannya_sama(client):
    anon = TestClient(app_module.app)
    salah = anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": "bukan-ini"})
    asing = anon.post("/api/auth/login", json={"email": "orang@lain.com", "password": "apa-saja"})

    assert salah.status_code == asing.status_code == 401
    assert salah.json()["message"] == asing.json()["message"]
    assert anon.get("/api/settings").status_code == 401


def test_password_disimpan_sebagai_hash(client, db):
    user = db.query(User).filter(User.email == TEST_EMAIL).first()
    assert TEST_PASSWORD not in user.password_hash
    assert user.password_hash.startswith("pbkdf2_sha256$")


def test_terlalu_banyak_gagal_dikunci_sementara(client):
    anon = TestClient(app_module.app)
    for _ in range(auth.MAX_FAILED_ATTEMPTS):
        anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": "tebakan"})

    res = anon.post("/api/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})

    assert res.status_code == 429   # even the right password waits out the lock
    auth.login_throttle._failures.clear()


def test_logout_mengakhiri_sesi(client):
    assert client.post("/api/auth/logout").json()["success"] is True
    assert client.get("/api/settings").status_code == 401


def test_cookie_lama_tidak_berlaku_setelah_logout(client):
    token = client.cookies.get(auth.SESSION_COOKIE)
    client.post("/api/auth/logout")

    pencuri = TestClient(app_module.app, cookies={auth.SESSION_COOKIE: token})
    assert pencuri.get("/api/settings").status_code == 401


def test_ganti_password_mengeluarkan_perangkat_lain(client):
    lain = TestClient(app_module.app)
    lain.post("/api/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})

    salah = client.post("/api/auth/change-password",
                        json={"current_password": "keliru", "new_password": "baru-12345"}).json()
    assert salah["success"] is False
    pendek = client.post("/api/auth/change-password",
                         json={"current_password": TEST_PASSWORD, "new_password": "123"}).json()
    assert pendek["success"] is False

    res = client.post("/api/auth/change-password",
                      json={"current_password": TEST_PASSWORD, "new_password": "baru-12345"}).json()

    assert res["success"] is True
    assert client.get("/api/settings").status_code == 200      # this browser stays in
    assert lain.get("/api/settings").status_code == 401        # the other one is out
    baru = TestClient(app_module.app)
    assert baru.post("/api/auth/login", json={"email": TEST_EMAIL, "password": "baru-12345"}).json()["success"]


def test_ganti_ganti_ip_tetap_terkunci_per_email():
    """Di balik proxy, IP berasal dari X-Forwarded-For yang bisa dipalsukan."""
    throttle = auth.LoginThrottle(max_attempts=5, max_per_email=8)
    for i in range(8):
        throttle.record_failure(f"10.0.0.{i}", TEST_EMAIL)

    assert throttle.seconds_locked("10.9.9.9", TEST_EMAIL) > 0     # IP baru pun ditahan
    assert throttle.seconds_locked("10.9.9.9", "lain@x.com") == 0  # email lain tidak terdampak


def test_healthz_terbuka_tanpa_login(client):
    assert TestClient(app_module.app).get("/healthz").json() == {"status": "ok"}


def test_akun_pertama_dari_environment_hanya_bila_belum_ada_akun(client, db, monkeypatch):
    monkeypatch.setenv("AUTOPOSTER_ADMIN_EMAIL", "pemilik@contoh.com")
    monkeypatch.setenv("AUTOPOSTER_ADMIN_PASSWORD", "rahasia-awal")

    app_module.bootstrap_first_user(db)   # sudah ada akun uji -> tidak menimpa apa pun
    assert db.query(User).filter(User.email == "pemilik@contoh.com").first() is None

    db.query(User).delete(); db.commit()
    app_module.bootstrap_first_user(db)
    assert auth.authenticate(db, "pemilik@contoh.com", "rahasia-awal") is not None
    db.query(User).delete(); db.commit()


def test_pembatas_login_melupakan_percobaan_kedaluwarsa(monkeypatch):
    """Tanpa ini, setiap IP/email yang pernah mencoba login tersimpan di memori selamanya."""
    from core import auth as auth_module

    jam = {"t": 1000.0}
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: jam["t"])
    throttle = auth_module.LoginThrottle(window=60)
    for i in range(500):
        throttle.record_failure(f"10.0.0.{i % 250}", f"bot{i}@x.test")
    assert len(throttle._failures) > 0

    jam["t"] += 61
    throttle.seconds_locked("10.0.0.1", "siapa@x.test")
    for key in list(throttle._failures):
        throttle._recent(key, jam["t"])

    assert throttle._failures == {}
