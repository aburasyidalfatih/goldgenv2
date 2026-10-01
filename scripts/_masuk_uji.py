"""Skrip uji memanggil API yang kini butuh login: buat akun uji di database sementara lalu masuk."""
import threading

from core.auth import has_any_user, set_user_password
from database.db_session import SessionLocal

EMAIL, PASSWORD = "uji@skrip.local", "kata-sandi-skrip"
_kunci = threading.Lock()


def masuk(client):
    # Sekali saja: membuat ulang akun mengakhiri sesi thread lain yang sudah masuk.
    with _kunci:
        db = SessionLocal()
        try:
            if not has_any_user(db):
                set_user_password(db, EMAIL, PASSWORD)
        finally:
            db.close()
    res = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert res.status_code == 200, res.text
