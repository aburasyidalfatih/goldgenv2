"""
Balas komentar otomatis: komentar mana yang dibalas, mode otomatis vs persetujuan,
pengaman (sekali balas, batas per jam, izin token), dan kealamian teks balasan.
Facebook dan model AI ditiru.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import scheduler as sched
from core import comment_reply as cr
from database.db_session import SessionLocal
from database.models import CommentReply, FacebookPage


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _comment(cid, message, name="Budi Santoso", uid="u1", hours_ago=1, replied_by=None):
    return {
        "id": cid, "message": message, "created_time": _ts(hours_ago),
        "from": {"id": uid, "name": name},
        "comments": {"data": [{"from": {"id": replied_by}}] if replied_by else []},
    }


def _post(pid, comments, message="Kenapa emas mengendap di tikungan dalam sungai?"):
    return {"id": pid, "message": message, "permalink_url": f"https://fb/{pid}",
            "created_time": _ts(30), "comments": {"data": comments}}


@pytest.fixture
def fb(monkeypatch):
    """Halaman Facebook tiruan: isi `state['posts']`, balasan terkirim tercatat di `state['sent']`."""
    state = {"posts": [], "sent": [], "reply_result": None, "fetch_error": None}
    lock = threading.Lock()

    def fetch(page_id, access_token, since_days=14, max_posts=25):
        if state["fetch_error"]:
            return {"success": False, "message": state["fetch_error"]}
        return {"success": True, "posts": state["posts"]}

    def reply(comment_id, access_token, message):
        if state["reply_result"]:
            return state["reply_result"]
        with lock:
            state["sent"].append({"comment_id": comment_id, "token": access_token, "message": message})
            n = len(state["sent"])
        return {"success": True, "reply_id": f"r{n}"}

    monkeypatch.setattr(cr, "fetch_recent_comments", fetch)
    monkeypatch.setattr(cr, "reply_to_comment", reply)
    monkeypatch.setattr(cr, "REPLY_GAP_SECONDS", (0, 0))
    return state


@pytest.fixture
def ai(monkeypatch):
    state = {"calls": [], "error": None, "skip": set()}

    def generate(text_ai, page_name, language, post_message, commenter_name, comment_message, recent):
        if state["error"]:
            raise RuntimeError(state["error"])
        state["calls"].append({"comment": comment_message, "recent": list(recent), "post": post_message})
        if comment_message in state["skip"]:
            return {"skip": True, "reason": "spam", "reply": ""}
        return {"skip": False, "reason": "", "reply": f"Betul {commenter_name.split()[0]}, arusnya melambat di situ."}

    monkeypatch.setattr(cr, "generate_comment_reply", generate)
    return state


@pytest.fixture
def page(make_page, with_gemini_key, client):
    p = make_page("111")
    client.patch(f"/api/pages/{p['id']}", json={"auto_reply_enabled": True, "auto_reply_mode": "auto"})
    return p


def _scan(client, page):
    return client.post(f"/api/replies/scan?page={page['id']}").json()


# ------------------------------------------------------------ komentar mana yang dibalas


def test_membalas_komentar_postingan_manual_maupun_dari_aplikasi(client, page, fb, ai, db):
    fb["posts"] = [
        _post("111_manual", [_comment("c1", "Di sungai saya juga begitu")]),
        _post("111_app", [_comment("c2", "Kalau di hulu bagaimana?", name="Siti", uid="u2")]),
    ]
    res = _scan(client, page)

    assert res["replied"] == 2, res
    assert {s["comment_id"] for s in fb["sent"]} == {"c1", "c2"}
    assert all(s["token"] == "token-111" for s in fb["sent"])


def test_yang_tidak_perlu_dibalas_dilewati(client, page, fb, ai, db):
    fb["posts"] = [_post("p1", [
        _comment("own", "Komentar admin", uid="111"),                       # komentar Fanspage sendiri
        _comment("done", "Sudah dijawab", replied_by="111"),                # admin sudah membalas
        _comment("old", "Komentar lama", hours_ago=72),                     # terlalu lama
        _comment("sticker", ""),                                            # stiker / foto saja
        _comment("ok", "Mantap ilmunya"),
    ])]
    _scan(client, page)

    assert [s["comment_id"] for s in fb["sent"]] == ["ok"]


def test_komentar_hanya_dibalas_sekali(client, page, fb, ai):
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    _scan(client, page)
    _scan(client, page)

    assert len(fb["sent"]) == 1
    assert len(ai["calls"]) == 1      # tidak membuang kuota AI untuk komentar yang sudah ditangani


def test_komentar_bertautan_dilewati_tanpa_memanggil_ai(client, page, fb, ai, db):
    fb["posts"] = [_post("p1", [_comment("spam", "Cek emas murah di bit.ly/xyz")])]
    _scan(client, page)

    row = db.query(CommentReply).filter_by(fb_comment_id="spam").one()
    assert row.status == "skipped" and "tautan" in row.note
    assert ai["calls"] == [] and fb["sent"] == []


def test_ai_memutuskan_tidak_membalas(client, page, fb, ai, db):
    ai["skip"].add("JUDI ONLINE GACOR")
    fb["posts"] = [_post("p1", [_comment("c1", "JUDI ONLINE GACOR")])]
    _scan(client, page)

    assert db.query(CommentReply).filter_by(fb_comment_id="c1").one().status == "skipped"
    assert fb["sent"] == []


def test_balasan_sebelumnya_diberikan_ke_ai_agar_tidak_mirip(client, page, fb, ai):
    fb["posts"] = [_post("p1", [_comment("c1", "Satu", hours_ago=3), _comment("c2", "Dua", hours_ago=2)])]
    _scan(client, page)

    assert ai["calls"][0]["recent"] == []
    assert ai["calls"][1]["recent"] == [fb["sent"][0]["message"]]


# ------------------------------------------------------------ mode persetujuan


def test_mode_persetujuan_hanya_membuat_draft(client, page, fb, ai, db):
    client.patch(f"/api/pages/{page['id']}", json={"auto_reply_mode": "review"})
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    res = _scan(client, page)

    assert res["drafted"] == 1 and fb["sent"] == []
    lst = client.get(f"/api/replies?page={page['id']}&status=pending").json()
    assert lst["counts"]["pending"] == 1
    assert client.get("/api/pages").json()["pages"][0]["pending_replies"] == 1

    item = lst["items"][0]
    sent = client.post(f"/api/replies/{item['id']}/send", json={"message": "Hasil editan saya."}).json()
    assert sent["success"] is True
    assert fb["sent"] == [{"comment_id": "c1", "token": "token-111", "message": "Hasil editan saya."}]

    again = client.post(f"/api/replies/{item['id']}/send", json={}).json()
    assert again["success"] is False and len(fb["sent"]) == 1


def test_kirim_bersamaan_hanya_terkirim_sekali(client, page, fb, ai, db, monkeypatch):
    client.patch(f"/api/pages/{page['id']}", json={"auto_reply_mode": "review"})
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    _scan(client, page)
    reply_id = db.query(CommentReply).one().id

    asli = cr.reply_to_comment

    def lambat(*a, **k):
        time.sleep(0.4)
        return asli(*a, **k)

    monkeypatch.setattr(cr, "reply_to_comment", lambat)

    def klik():
        sesi = SessionLocal()
        try:
            app_module.send_reply_endpoint(reply_id, app_module.ReplySendRequest(), sesi)
        finally:
            sesi.close()

    utas = [threading.Thread(target=klik) for _ in range(3)]
    [u.start() for u in utas]
    [u.join() for u in utas]
    assert len(fb["sent"]) == 1


def test_buat_ulang_dan_abaikan(client, page, fb, ai, db):
    client.patch(f"/api/pages/{page['id']}", json={"auto_reply_mode": "review"})
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    _scan(client, page)
    rid = db.query(CommentReply).one().id

    assert client.post(f"/api/replies/{rid}/regenerate").json()["success"] is True
    assert client.post(f"/api/replies/{rid}/dismiss").json()["reply"]["status"] == "dismissed"
    assert client.post(f"/api/replies/{rid}/send", json={}).json()["success"] is False
    assert fb["sent"] == []


# ------------------------------------------------------------ pengaman


def test_batas_balasan_per_jam(client, page, fb, ai, db):
    client.patch(f"/api/pages/{page['id']}", json={"reply_max_per_hour": 2})
    fb["posts"] = [_post("p1", [_comment(f"c{i}", f"Komentar {i}", hours_ago=5 - i) for i in range(5)])]
    res = _scan(client, page)

    assert len(fb["sent"]) == 2
    assert "Batas 2 balasan per jam" in res["message"]
    # sisanya tidak dicatat, jadi akan dibalas pada putaran berikutnya
    assert db.query(CommentReply).count() == 2


def test_ai_gagal_tidak_mencatat_dan_dicoba_lagi(client, page, fb, ai, db):
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    ai["error"] = "quota exceeded"
    res = _scan(client, page)

    assert res["success"] is False and "quota" in res["message"]
    assert db.query(CommentReply).count() == 0
    assert "quota" in client.get("/api/pages").json()["pages"][0]["reply_last_error"]

    ai["error"] = None
    assert _scan(client, page)["replied"] == 1
    assert client.get("/api/pages").json()["pages"][0]["reply_last_error"] is None


def test_token_tanpa_izin_berhenti_dengan_pesan_jelas(client, page, fb, ai, db):
    fb["reply_result"] = {"success": False, "permission_error": True,
                          "message": "(#200) Permissions error. Pastikan ... pages_manage_engagement."}
    fb["posts"] = [_post("p1", [_comment("c1", "Satu", hours_ago=2), _comment("c2", "Dua", hours_ago=1)])]
    res = _scan(client, page)

    assert res["success"] is False and "pages_manage_engagement" in res["message"]
    assert db.query(CommentReply).count() == 1      # berhenti setelah kegagalan izin pertama


def test_gagal_membaca_komentar_disimpan_sebagai_error(client, page, fb, ai):
    fb["fetch_error"] = "Token belum punya izin pages_read_user_content"
    res = _scan(client, page)
    assert res["success"] is False
    assert client.get("/api/pages").json()["pages"][0]["reply_last_error"] == fb["fetch_error"]


def test_tanpa_kunci_ai_ditolak(client, make_page, fb, ai):
    p = make_page("111")
    res = client.post(f"/api/replies/scan?page={p['id']}").json()
    assert res["success"] is False and "API Key" in res["message"]


def test_balasan_macet_ditandai_gagal_saat_menyala(client, page, db):
    db.add(CommentReply(page_id=page["id"], fb_post_id="p1", fb_comment_id="c1",
                        comment_message="x", reply_message="y", status="replying"))
    db.commit()
    with TestClient(app_module.app):
        pass
    row = db.query(CommentReply).one()
    db.refresh(row)
    assert row.status == "failed" and "Periksa komentar" in row.note


# ------------------------------------------------------------ penjadwal & pengaturan


def test_job_terjadwal_hanya_untuk_halaman_yang_aktif(client, page, make_page, fb, ai, db):
    mati = make_page("222")            # auto-reply tidak dinyalakan
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]

    sched.comment_reply_job()

    rows = db.query(CommentReply).all()
    assert {r.page_id for r in rows} == {page["id"]}
    assert mati["id"] not in {r.page_id for r in rows}
    assert client.get("/api/scheduler/status").json()["next_reply_scan"] is not None


def test_pengaturan_balasan_divalidasi(client, page):
    assert client.patch(f"/api/pages/{page['id']}", json={"auto_reply_mode": "ngawur"}).json()["success"] is False
    client.patch(f"/api/pages/{page['id']}", json={"reply_max_per_hour": 999})
    assert client.get("/api/pages").json()["pages"][0]["reply_max_per_hour"] == 60


def test_hapus_halaman_menghapus_riwayat_balasannya(client, page, fb, ai, db):
    fb["posts"] = [_post("p1", [_comment("c1", "Mantap")])]
    _scan(client, page)
    client.delete(f"/api/pages/{page['id']}")
    assert db.query(CommentReply).count() == 0


# ------------------------------------------------------------ kealamian teks


@pytest.mark.parametrize("mentah, hasil", [
    ("Betul banget! Arus melambat di tikungan dalam. Coba dulang di sana. Semoga sukses!",
     "Betul banget! Arus melambat di tikungan dalam."),
    ("Mantap bang 😄 Coba cek celah bedrock-nya juga.", "Mantap bang 😄 Coba cek celah bedrock-nya juga."),
    ("Siap! 🙌 Nanti kita bahas. Lagi ya.", "Siap! 🙌 Nanti kita bahas."),
    ('"Iya, pasir hitam itu magnetit. #emas #geologi https://x.com"', "Iya, pasir hitam itu magnetit."),
])
def test_balasan_dirapikan_maksimal_dua_kalimat(mentah, hasil):
    assert cr.clean_reply(mentah) == hasil


def test_generate_memakai_penyedia_teks_dan_membatasi_kalimat(monkeypatch):
    dipanggil = {}

    def palsu(provider, api_key, model, system, user, temperature, reasoning=None):
        dipanggil["reasoning"] = reasoning
        dipanggil.update(provider=provider, system=system, user=user)
        return json.dumps({"skip": False, "reason": "",
                           "reply": "Wah keren. Itu tanda bagus. Lanjutkan. Semangat!"})

    monkeypatch.setattr(cr, "complete_json", palsu)
    out = cr.generate_comment_reply({"provider": "openai", "api_key": "k", "model": "m"},
                                    "Info Emas", "id", "Caption", "Budi", "Dapat 2 butir!", ["Siap bang."])

    assert out == {"skip": False, "reason": "", "reply": "Wah keren. Itu tanda bagus."}
    assert dipanggil["provider"] == "openai"
    assert "MAKSIMAL 2 kalimat" in dipanggil["system"] and "Info Emas" in dipanggil["system"]
    assert "Dapat 2 butir!" in dipanggil["user"] and "Siap bang." in dipanggil["user"]
