"""
Membuat akun login dashboard, atau mengganti password-nya (semua sesi lama ikut berakhir).

    python scripts/atur_login.py email@contoh.com              # password diketik tersembunyi
    echo rahasia | python scripts/atur_login.py email@contoh.com --password-stdin
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db_session import Base, SessionLocal, engine  # noqa: E402
from core.auth import set_user_password  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Atur akun login dashboard AutoPoster.")
    parser.add_argument("email")
    parser.add_argument("--password-stdin", action="store_true", help="baca password dari stdin")
    args = parser.parse_args()

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password baru: ")
        if getpass.getpass("Ulangi password: ") != password:
            sys.exit("Password tidak sama.")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = set_user_password(db, args.email, password)
        print(f"Akun login '{user.email}' tersimpan.")
    except ValueError as e:
        sys.exit(str(e))
    finally:
        db.close()


if __name__ == "__main__":
    main()
