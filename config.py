import os
from pathlib import Path

# Base Directories.
# The data/storage locations can be redirected via environment variables so the
# test suite (and alternative deployments) never touch the real database.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AUTOPOSTER_DATA_DIR") or (BASE_DIR / "data"))
STORAGE_DIR = Path(os.environ.get("AUTOPOSTER_STORAGE_DIR") or (BASE_DIR / "storage"))
IMAGES_DIR = STORAGE_DIR / "generated_images"

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# SQLite Database Location (inside data/ for Dokploy persistent volume)
DB_PATH = DATA_DIR / "autoposter.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Default Model & Application Settings (Gemini 3 Family)
DEFAULT_TEXT_MODEL = "gemini-3.8-flash"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_IMAGE_FALLBACK_MODEL = "imagen-3.0-generate-002"

# Optional OpenAI route (chosen per role in Settings: text_provider / image_provider)
DEFAULT_OPENAI_TEXT_MODEL = "gpt-5-mini"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-1"

DEFAULT_SETTINGS = {
    "gemini_api_key": "",
    "gemini_text_model": DEFAULT_TEXT_MODEL,
    "gemini_image_model": DEFAULT_IMAGE_MODEL,
    "openai_api_key": "",
    "openai_text_model": DEFAULT_OPENAI_TEXT_MODEL,
    "openai_image_model": DEFAULT_OPENAI_IMAGE_MODEL,
    "text_provider": "gemini",    # 'gemini' | 'openai' — writes concept & caption
    "image_provider": "gemini",   # 'gemini' | 'openai' — renders the poster
    "fb_page_id": "",
    "fb_page_access_token": "",
    "fb_page_name": "",
    "fb_page_picture": "",
    "fb_token_status": "Not Configured",
    "content_language": "id",  # 'id' for Indonesian, 'en' for English
    "aspect_ratio": "3:4",     # 3:4 portrait optimal for Facebook
    "auto_scheduler_enabled": "false",
    "auto_post_times": "10:00,19:00",
    # Topic evolution: grow new topics from the best performers of the last N days
    "auto_topic_evolution": "true",
    "topic_window_days": "7",
    "max_new_topics_per_cycle": "2",
}

# Timezone used by the autopilot scheduler when reading auto_post_times
SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "Asia/Jakarta")

# Facebook Graph API version (v19.0 reached end-of-life in early 2024+2y).
# Override via environment if Meta sunsets this one too.
FB_GRAPH_API_VERSION = os.environ.get("FB_GRAPH_API_VERSION", "v25.0")

# Setting keys that must never be sent back to the browser in cleartext.
SENSITIVE_SETTING_KEYS = {"gemini_api_key", "openai_api_key", "fb_page_access_token"}

# Placeholder returned instead of a stored secret. When the UI sends this value
# back, the server keeps whatever is already in the database.
SECRET_MASK = "••••••••••••"
