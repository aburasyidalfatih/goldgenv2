"""
Shared test setup.

Every test runs against a throwaway database and storage folder: the real
data/autoposter.db and its generated posters are never touched. The environment
variables must be set before any application module is imported, which is why
this happens at the top of conftest (pytest always imports it first).
"""
import os
import sys
import uuid
import shutil
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="autoposter_tests_"))
os.environ["AUTOPOSTER_DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["AUTOPOSTER_STORAGE_DIR"] = str(TEST_ROOT / "storage")

# Import the app package from the project root, not from tests/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import app as app_module  # noqa: E402
import core.pages as pages_module  # noqa: E402
import core.feedback_loop as feedback_module  # noqa: E402
from config import IMAGES_DIR  # noqa: E402
from database.db_session import SessionLocal, engine, Base  # noqa: E402
from database.models import (  # noqa: E402
    AppSetting,
    ContentTopic,
    FacebookPage,
    PageTopicWeight,
    Post,
    PostMetric,
)
from scheduler import scheduler  # noqa: E402


# ---------------------------------------------------------------- lifecycle


@pytest.fixture(scope="session", autouse=True)
def _teardown_session():
    """Stop the background scheduler and remove the throwaway data directory."""
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)
    engine.dispose()
    shutil.rmtree(TEST_ROOT, ignore_errors=True)


@pytest.fixture
def tmp_dir():
    """
    A throwaway directory. Used instead of pytest's built-in `tmp_path`, whose
    shared basetemp can be left unwritable by an earlier interrupted run.
    """
    path = Path(tempfile.mkdtemp(prefix="autoposter_case_", dir=TEST_ROOT))
    yield path


@pytest.fixture
def client():
    """
    A TestClient with a clean slate: no Fanspage, no posts, topics reset to the
    seeded base curriculum. Running the lifespan also exercises the real startup
    path (migrations + seeding) on every test.
    """
    with TestClient(app_module.app) as c:
        _reset_state()
        login(c)
        yield c
        _reset_state()


TEST_EMAIL = "admin@test.local"
TEST_PASSWORD = "kata-sandi-uji"


def login(c):
    """Every page and API needs a session; tests act as the signed-in owner."""
    from core.auth import set_user_password, login_throttle

    session = SessionLocal()
    try:
        set_user_password(session, TEST_EMAIL, TEST_PASSWORD)
    finally:
        session.close()
    login_throttle._failures.clear()
    res = c.post("/api/auth/login", json={"email": TEST_EMAIL, "password": TEST_PASSWORD})
    assert res.status_code == 200, res.text


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _reset_state():
    """Wipes per-test data but keeps the seeded topic catalog."""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        session.query(PostMetric).delete()
        session.query(Post).delete()
        session.query(PageTopicWeight).delete()
        session.query(FacebookPage).delete()
        session.query(ContentTopic).filter(ContentTopic.source == "ai").delete()

        from core.taxonomy import SEED_TOPICS

        baseline = {t["topic_key"]: t.get("weight", 1.0) for t in SEED_TOPICS}
        for topic in session.query(ContentTopic).all():
            topic.weight = baseline.get(topic.topic_key, 1.0)
            topic.posts_count = 0
            topic.total_score = 0.0
            topic.avg_reach = 0.0
            topic.last_used_at = None
            topic.is_active = True
            topic.base_topic_id = topic.id

        for setting in session.query(AppSetting).all():
            if setting.key in ("gemini_api_key", "gemini_verified_fp", "openai_verified_fp"):
                setting.value = ""
        session.commit()
    finally:
        session.close()

    for job in list(scheduler.get_jobs()):
        if job.id.startswith("autopost_") or job.id == "catch_up_job":
            scheduler.remove_job(job.id)


# ---------------------------------------------------------------- fakes


@pytest.fixture
def fake_facebook(monkeypatch):
    """
    Replaces the Graph API with a predictable stand-in. Returns a recorder so a
    test can assert what was sent and simulate failures.
    """

    state = {"verified": [], "published": [], "fail_verify": set(), "fail_publish": False}

    def verify(page_id, access_token):
        state["verified"].append((page_id, access_token))
        if page_id in state["fail_verify"]:
            return {"success": False, "message": "Facebook Error: invalid token"}
        return {
            "success": True,
            "page_id": page_id,
            "page_name": f"Halaman {page_id}",
            "picture_url": f"https://example.test/{page_id}.jpg",
            "link": f"https://facebook.com/{page_id}",
            "fan_count": 1234,
            "message": "ok",
        }

    def publish(page_id, access_token, image_path, caption):
        state["published"].append(
            {"page_id": page_id, "token": access_token, "caption": caption, "image": image_path}
        )
        if state["fail_publish"]:
            return {"success": False, "message": "Facebook Error: permission denied"}
        post_id = f"{page_id}_{len(state['published'])}"
        return {
            "success": True,
            "post_id": post_id,
            "photo_id": post_id,
            "post_url": f"https://www.facebook.com/{post_id}",
            "message": "ok",
        }

    monkeypatch.setattr(pages_module, "test_facebook_credentials", verify)
    monkeypatch.setattr(app_module, "verify_page_credentials", verify)
    monkeypatch.setattr(app_module, "publish_photo_to_page", publish)
    return state


@pytest.fixture
def fake_metrics(monkeypatch):
    """Graph API metrics stand-in. `values[fb_post_id]` drives what each post returns."""
    state = {"values": {}, "calls": []}

    def fetch(page_id, access_token, fb_post_id):
        state["calls"].append(fb_post_id)
        return state["values"].get(
            fb_post_id, {"reactions": 0, "comments": 0, "shares": 0, "reach": 0, "impressions": 0}
        )

    monkeypatch.setattr(feedback_module, "fetch_post_metrics", fetch)
    return state


@pytest.fixture
def fake_gemini(monkeypatch):
    """Content generation stand-in, so no API key or quota is needed."""
    state = {"calls": [], "ratios": []}

    def generate_content(api_key, topic_dict, language="id", model_name="", provider="gemini"):
        state["calls"].append({"topic": topic_dict["title"], "language": language,
                               "provider": provider, "api_key": api_key, "model": model_name})
        # Unique per call: two generations can pick the same topic, and tests that
        # compare captions must not collide by chance.
        n = len(state["calls"])
        return {
            "visual_title": f"POSTER {topic_dict['title'][:20]}",
            "subtitle": "sub",
            "imagen_prompt": f"prompt #{n} untuk {topic_dict['title']}",
            "caption": f"caption #{n} {language} untuk {topic_dict['title']}",
        }

    def generate_image(api_key, prompt, aspect_ratio="3:4", model_name="", provider="gemini"):
        state["ratios"].append(aspect_ratio)
        state.setdefault("images", []).append({"provider": provider, "api_key": api_key,
                                               "model": model_name})
        filename = f"test_poster_{uuid.uuid4().hex[:8]}.jpg"
        path = IMAGES_DIR / filename
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (30, 40), (200, 170, 110)).save(path, "JPEG")
        return filename, str(path)

    monkeypatch.setattr(app_module, "generate_post_content", generate_content)
    monkeypatch.setattr(app_module, "generate_poster_image", generate_image)
    return state


# ---------------------------------------------------------------- builders


@pytest.fixture
def make_page(client, fake_facebook):
    """Registers a Fanspage through the real API and returns its serialized form."""

    def _make(page_id="100", **settings):
        res = client.post(
            "/api/pages", json={"page_id": page_id, "access_token": f"token-{page_id}"}
        ).json()
        assert res["success"], res
        row_id = res["page"]["id"]
        if settings:
            client.patch(f"/api/pages/{row_id}", json=settings)
        return client.get("/api/pages").json()["pages"][-1]

    return _make


@pytest.fixture
def make_post(db):
    """
    Inserts a published post with metrics directly, to simulate history without
    calling any external API.
    """
    from datetime import datetime, timedelta, timezone

    def _make(page_row_id, topic, days_ago=1, reach=1000, status="published", with_metrics=True):
        published = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None)
        post = Post(
            page_id=page_row_id,
            topic_id=topic.id,
            topic_title=topic.title,
            language="id",
            visual_title=f"{topic.title[:20]} ({days_ago}d)",
            prompt_used="prompt",
            image_filename="none.jpg",
            image_path="none.jpg",
            caption="caption",
            status=status,
            fb_post_id=f"fb_{page_row_id}_{topic.id}_{days_ago}_{reach}" if status == "published" else None,
            created_at=published,
            published_at=published if status == "published" else None,
        )
        db.add(post)
        db.flush()
        if with_metrics and status == "published":
            reactions, comments, shares = reach // 60, reach // 250, reach // 180
            db.add(
                PostMetric(
                    post_id=post.id,
                    fb_post_id=post.fb_post_id,
                    reactions=reactions,
                    comments=comments,
                    shares=shares,
                    reach=reach,
                    impressions=int(reach * 1.3),
                    calculated_score=shares * 4 + comments * 3 + reactions * 1.5 + reach * 0.05,
                )
            )
        db.commit()
        return post

    return _make


@pytest.fixture
def topics(db):
    """The seeded base curriculum, ordered by id."""
    return db.query(ContentTopic).order_by(ContentTopic.id).all()


@pytest.fixture
def with_gemini_key(db):
    """Stores a dummy Gemini key so endpoints that require one proceed."""
    setting = db.query(AppSetting).filter(AppSetting.key == "gemini_api_key").first()
    setting.value = "dummy-key"
    db.commit()
    yield
    setting.value = ""
    db.commit()


@pytest.fixture
def finish_test_phase(make_post, topics):
    """
    Publishes every base topic once on a page, old enough to be measured, so the
    page leaves the test phase and enters the focus phase.
    """
    def _finish(page_row_id, reach=1000, days_ago=20, skip=()):
        for topic in topics:
            if topic.source == "seed" and topic.id not in skip:
                make_post(page_row_id, topic, days_ago=days_ago, reach=reach)

    return _finish
