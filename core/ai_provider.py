"""
Which AI service writes the copy and which one renders the poster.

Text and image are chosen separately, so e.g. Gemini can write while OpenAI draws.
"""
import logging

from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from config import (
    DEFAULT_TEXT_MODEL,
    DEFAULT_IMAGE_MODEL,
    DEFAULT_OPENAI_TEXT_MODEL,
    DEFAULT_OPENAI_IMAGE_MODEL,
)
from core import openai_client
from database.models import AppSetting

logger = logging.getLogger(__name__)

# Cost controls (Settings). Thinking tokens are billed as output, and image
# quality decides how many image tokens are spent: both are most of the bill.
REASONING_LEVELS = ("low", "medium", "high")
DEFAULT_REASONING = "low"
IMAGE_QUALITIES = ("low", "medium", "high", "auto")
DEFAULT_IMAGE_QUALITY = "medium"

PROVIDER_LABELS = {"gemini": "Gemini", "openai": "OpenAI"}

# setting key of the API key, and of the model per role
_KEY_SETTING = {"gemini": "gemini_api_key", "openai": "openai_api_key"}
_MODEL_SETTING = {
    ("gemini", "text"): ("gemini_text_model", DEFAULT_TEXT_MODEL),
    ("gemini", "image"): ("gemini_image_model", DEFAULT_IMAGE_MODEL),
    ("openai", "text"): ("openai_text_model", DEFAULT_OPENAI_TEXT_MODEL),
    ("openai", "image"): ("openai_image_model", DEFAULT_OPENAI_IMAGE_MODEL),
}


def _setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value else default


def normalize_provider(value) -> str:
    value = (value or "").strip().lower()
    return value if value in PROVIDER_LABELS else "gemini"


def ai_backend(db: Session, role: str) -> dict:
    """
    Resolves the provider, API key and model used for `role` ('text' or 'image').
    `missing` holds a user-facing message when the key is not set, else None.
    """
    provider = normalize_provider(_setting(db, f"{role}_provider", "gemini"))
    model_key, model_default = _MODEL_SETTING[(provider, role)]
    label = PROVIDER_LABELS[provider]
    api_key = _setting(db, _KEY_SETTING[provider])
    reasoning = _setting(db, "text_reasoning", DEFAULT_REASONING)
    quality = _setting(db, "openai_image_quality", DEFAULT_IMAGE_QUALITY)
    return {
        "provider": provider,
        "label": label,
        "api_key": api_key,
        "model": _setting(db, model_key, model_default),
        "missing": None if api_key else f"{label} API Key belum diisi. Silakan isi di tab Pengaturan.",
        # text role: how hard the model thinks; image role: OpenAI image quality
        "reasoning": reasoning if reasoning in REASONING_LEVELS else DEFAULT_REASONING,
        "quality": quality if quality in IMAGE_QUALITIES else DEFAULT_IMAGE_QUALITY,
    }


def complete_json(provider: str, api_key: str, model: str, system: str, user: str,
                  temperature: float, reasoning: str | None = None) -> str:
    """
    Asks the chosen text model for a JSON answer and returns the raw text.
    `reasoning` ('low'/'medium'/'high') limits the model's thinking tokens.
    """
    if provider == "openai":
        return openai_client.complete_json(api_key, model, system, user, temperature, reasoning)

    if not api_key:
        raise ValueError("Gemini API Key belum diisi. Silakan isi di tab Pengaturan UI.")
    # Keep the Client referenced for the whole call: google-genai closes its HTTP
    # session in Client.__del__, so an inline genai.Client(...).models... would be
    # garbage-collected mid-call ("Cannot send a request, as the client has been closed").
    client = genai.Client(api_key=api_key)

    def ask(thinking):
        return client.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=temperature,
                thinking_config=thinking,
            ),
        )

    thinking = types.ThinkingConfig(thinking_level=reasoning.upper()) if reasoning else None
    try:
        response = ask(thinking)
    except Exception as e:
        # Older models (2.5 and earlier) use a token budget instead of levels.
        if thinking is None or "think" not in str(e).lower():
            raise
        logger.info(f"Gemini model '{model}' rejected thinking_level; retrying without it.")
        response = ask(None)
    return (response.text or "").strip()
