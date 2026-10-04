import os
from pathlib import Path

# Base Directories.
# The data/storage locations can be redirected via environment variables so the
# test suite (and alternative deployments) never touch the real database.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AUTOPOSTER_DATA_DIR") or (BASE_DIR / "data"))
STORAGE_DIR = Path(os.environ.get("AUTOPOSTER_STORAGE_DIR") or (BASE_DIR / "storage"))
IMAGES_DIR = STORAGE_DIR / "generated_images"
# Nightly database backups. Docker mounts its own volume here, so a damaged data
# volume does not take the backups with it.
BACKUP_DIR = Path(os.environ.get("AUTOPOSTER_BACKUP_DIR") or (BASE_DIR / "backups"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# SQLite Database Location (inside data/ for Dokploy persistent volume)
DB_PATH = DATA_DIR / "autoposter.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Language of new Fanspages' captions and comment replies: 'en' (American English)
# or 'id' (Indonesian). Each page can still be switched in the Fanspage tab.
DEFAULT_CONTENT_LANGUAGE = "en"

# Default Model & Application Settings (Gemini 3 Family)
DEFAULT_TEXT_MODEL = "gemini-3.8-flash"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_IMAGE_FALLBACK_MODEL = "imagen-3.0-generate-002"

# Gemini request timeouts (milliseconds). Without one a stalled call blocks its
# scheduler job forever, and max_instances=1 then skips every later run.
GEMINI_TEXT_TIMEOUT_MS = 120_000
GEMINI_IMAGE_TIMEOUT_MS = 300_000

# Optional OpenAI route (chosen per role in Settings: text_provider / image_provider)
DEFAULT_OPENAI_TEXT_MODEL = "gpt-6.1-sol"
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2.5-flare"

# OpenAI models still served by the API (checked 1 Oct 2026, developers.openai.com
# /api/docs/models and /deprecations). Retired ones are swapped for their
# replacement at startup so a saved setting never points at a dead model.
OPENAI_RETIRED_MODELS = {
    # text: gpt-3.5/4/4-turbo/4.1-nano off 2026-10-23; gpt-5 snapshots off 2026-12-11
    "gpt-3.5-turbo": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-4": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-4-turbo": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-4.1-nano": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5-mini": DEFAULT_OPENAI_TEXT_MODEL,
    "gpt-5-nano": DEFAULT_OPENAI_TEXT_MODEL,
    # image: dall-e removed 2026-05-12; gpt-image-1 off 2026-10-23; 1-mini/1.5 off 2026-12-01
    "dall-e-2": DEFAULT_OPENAI_IMAGE_MODEL,
    "dall-e-3": DEFAULT_OPENAI_IMAGE_MODEL,
    "gpt-image-1": DEFAULT_OPENAI_IMAGE_MODEL,
    "gpt-image-1-mini": DEFAULT_OPENAI_IMAGE_MODEL,
    "gpt-image-1.5": DEFAULT_OPENAI_IMAGE_MODEL,
    "chatgpt-image-latest": DEFAULT_OPENAI_IMAGE_MODEL,
}

DEFAULT_SETTINGS = {
    "gemini_api_key": "",
    "gemini_text_model": DEFAULT_TEXT_MODEL,
    "gemini_image_model": DEFAULT_IMAGE_MODEL,
    "openai_api_key": "",
    "openai_text_model": DEFAULT_OPENAI_TEXT_MODEL,
    "openai_image_model": DEFAULT_OPENAI_IMAGE_MODEL,
    "text_provider": "gemini",    # 'gemini' | 'openai' — writes concept & caption
    "image_provider": "gemini",   # 'gemini' | 'openai' — renders the poster
    # Cost controls: how much the text model "thinks" (thinking tokens are billed as
    # output) and the OpenAI image quality (decides how many image tokens are spent).
    "text_reasoning": "low",          # 'low' | 'medium' | 'high'
    "openai_image_quality": "medium", # 'low' | 'medium' | 'high' | 'auto'
    "fb_page_id": "",
    "fb_page_access_token": "",
    "fb_page_name": "",
    "fb_page_picture": "",
    "fb_token_status": "Not Configured",
    "content_language": DEFAULT_CONTENT_LANGUAGE,
    "aspect_ratio": "3:4",     # 3:4 portrait optimal for Facebook
    "auto_scheduler_enabled": "false",
    "auto_post_times": "10:00,19:00",
    # Topic evolution: grow new topics from the best performers of the last N days
    "auto_topic_evolution": "true",
    "topic_window_days": "7",
    "max_new_topics_per_cycle": "2",
    # Email alerts (Gmail: smtp.gmail.com:587 + an App Password). An empty
    # recipient falls back to the dashboard login's email.
    "notify_email_enabled": "false",
    "notify_email_to": "",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
}

# Timezone used by the autopilot scheduler when reading auto_post_times
SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "Asia/Jakarta")

# Facebook Graph API version (v19.0 reached end-of-life in early 2024+2y).
# Override via environment if Meta sunsets this one too.
FB_GRAPH_API_VERSION = os.environ.get("FB_GRAPH_API_VERSION", "v25.0")

# Setting keys that must never be sent back to the browser in cleartext.
SENSITIVE_SETTING_KEYS = {"gemini_api_key", "openai_api_key", "fb_page_access_token", "smtp_password"}

# Placeholder returned instead of a stored secret. When the UI sends this value
# back, the server keeps whatever is already in the database.
SECRET_MASK = "••••••••••••"
