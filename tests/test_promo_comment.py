"""Komentar promosi pertama: satu komentar Fanspage berisi link di tiap postingan baru."""
import json

import pytest

import scheduler as sched
from core import promo_comment as pc
from database.models import FacebookPage, Post

URL = "https://firstflake.com/"
MENIT = 1 / 1440


@pytest.fixture
def fb(monkeypatch):
    state = {"sent": [], "result": None}

    def comment(post_id, token, message):
        if state["result"]:
            return state["result"]
        state["sent"].append({"post": post_id, "token": token, "message": message})
        return {"success": True, "reply_id": f"cm{len(state['sent'])}"}

    monkeypatch.setattr(pc, "comment_on_post", comment)
    return state


@pytest.fixture
def ai(monkeypatch):
    state = {"answers": [], "calls": []}

    def complete(provider, api_key, model, system, user, temperature, reasoning=None):
        state["calls"].append({"system": system, "user": user})
        answer = state["answers"].pop(0) if state["answers"] else f"Variasi nomor {len(state['calls'])} soal topik ini, cek {URL}"
        return json.dumps({"comment": answer})

    monkeypatch.setattr(pc, "complete_json", complete)
    return state


@pytest.fixture
def page(client, make_page, with_gemini_key):
    p = make_page("111")
    res = client.patch(f"/api/pages/{p['id']}", json={"promo_enabled": True, "promo_url": "firstflake.com/",
                                                      "promo_note": "panduan pemula"}).json()
    assert res["success"], res
    return res["page"]


def _jalankan():
    sched.promo_comment_job()


# ------------------------------------------------------------ pengaturan


def test_alamat_dirapikan_dan_wajib_sebelum_aktif(client, make_page):
    p = make_page("111")
    res = client.patch(f"/api/pages/{p['id']}", json={"promo_enabled": True}).json()
    assert res["success"] is False and "alamat web" in res["message"].lower()

    res = client.patch(f"/api/pages/{p['id']}", json={"promo_url": "bukan alamat"}).json()
    assert res["success"] is False

    res = client.patch(f"/api/pages/{p['id']}", json={"promo_url": "firstflake.com/", "promo_enabled": True}).json()
    assert res["success"] and res["page"]["promo_url"] == URL and res["page"]["promo_enabled"] is True


# ------------------------------------------------------------ pengiriman


def test_satu_komentar_per_postingan_berisi_link(page, make_post, topics, fb, ai, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)

    _jalankan()
    _jalankan()                                    # putaran berikutnya tidak mengomentari lagi

    assert len(fb["sent"]) == 1
    sent = fb["sent"][0]
    assert sent["post"] == post.fb_post_id and sent["token"] == "token-111"
    assert sent["message"].count(URL) == 1
    db.refresh(post)
    assert post.promo_status == "posted" and post.promo_comment_fb_id == "cm1"
    assert "panduan pemula" in ai["calls"][0]["user"]          # isi web dikirim ke AI


def test_postingan_baru_menunggu_dan_yang_lama_dilewati(page, make_post, topics, fb, ai):
    make_post(page["id"], topics[0], days_ago=0.5 * MENIT)    # baru 30 detik tayang
    make_post(page["id"], topics[1], days_ago=2)              # sudah 2 hari
    make_post(page["id"], topics[2], days_ago=1, status="ready")

    _jalankan()

    assert fb["sent"] == []


def test_halaman_tanpa_promosi_tidak_dikomentari(client, make_page, make_post, topics, fb, ai):
    p = make_page("222")
    make_post(p["id"], topics[0], days_ago=5 * MENIT)

    _jalankan()

    assert fb["sent"] == []


def test_kalimat_tidak_mengulang_komentar_sebelumnya(page, make_post, topics, fb, ai):
    ulang = f"Mau belajar lebih jauh soal emas di sungai? Mampir ke {URL}"
    make_post(page["id"], topics[0], days_ago=10 * MENIT)
    ai["answers"] = [ulang]
    _jalankan()

    make_post(page["id"], topics[1], days_ago=5 * MENIT)
    ai["answers"] = [ulang, ulang.replace("Mau", "Ingin")]     # AI dua kali mengulang
    _jalankan()

    pertama, kedua = (s["message"] for s in fb["sent"])
    assert pertama == ulang
    assert not pc.too_similar(kedua, [pertama]) and kedua.count(URL) == 1
    assert pertama in ai["calls"][-1]["user"]                  # komentar lama ditunjukkan ke AI


def test_link_lain_dari_ai_tidak_ikut_terkirim(page, make_post, topics, fb, ai):
    make_post(page["id"], topics[0], days_ago=5 * MENIT)
    ai["answers"] = ["Cek juga https://situs-lain.com ya", "Lihat https://bit.ly/abc sekarang"]

    _jalankan()

    message = fb["sent"][0]["message"]
    assert "situs-lain" not in message and "bit.ly" not in message and URL in message


def test_ai_gagal_tetap_mengirim_kalimat_cadangan(page, make_post, topics, fb, monkeypatch):
    def rusak(*a, **k):
        raise RuntimeError("kuota habis")

    monkeypatch.setattr(pc, "complete_json", rusak)
    make_post(page["id"], topics[0], days_ago=5 * MENIT)

    _jalankan()

    assert len(fb["sent"]) == 1 and URL in fb["sent"][0]["message"]


def test_gagal_dicoba_lagi_maksimal_tiga_kali(page, make_post, topics, fb, ai, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    fb["result"] = {"success": False, "message": "(#368) temporarily blocked"}

    for _ in range(5):
        _jalankan()

    db.refresh(post)
    assert post.promo_status == "failed" and post.promo_attempts == pc.PROMO_MAX_ATTEMPTS
    halaman = db.query(FacebookPage).filter(FacebookPage.id == page["id"]).one()
    db.refresh(halaman)
    assert "temporarily blocked" in halaman.promo_last_error


def test_koneksi_putus_tidak_dicoba_ulang(page, make_post, topics, fb, ai, db):
    """Komentar mungkin sudah tayang: mengirim ulang bisa membuat komentar ganda."""
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    fb["result"] = {"success": False, "message": "Koneksi ke Facebook gagal", "uncertain": True}
    _jalankan()
    fb["result"] = None
    _jalankan()

    db.refresh(post)
    assert post.promo_status == "uncertain" and fb["sent"] == []


def test_terhenti_saat_mengirim_ditandai_saat_start(page, make_post, topics, db):
    post = make_post(page["id"], topics[0], days_ago=5 * MENIT)
    post.promo_status = "posting"
    db.commit()

    assert pc.recover_interrupted_promos(db) == 1
    db.commit()
    db.refresh(post)
    assert post.promo_status == "uncertain"


def test_pembersih_kalimat():
    assert pc.clean_promo('"Lengkapnya di #emas www.lain.com"', URL) == f"Lengkapnya di {URL}"
    assert len(pc.clean_promo("kata " * 200, URL)) <= pc.MAX_PROMO_CHARS
    assert pc.has_foreign_link("cek https://firstflake.com", URL) is False
    assert pc.has_foreign_link("cek https://bit.ly/x", URL) is True
