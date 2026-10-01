"""
Which AI service writes the copy and which one renders the poster.

Text and image are chosen separately, so e.g. Gemini can write while OpenAI draws.
"""
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
    return {
        "provider": provider,
        "label": label,
        "api_key": api_key,
        "model": _setting(db, model_key, model_default),
        "missing": None if api_key else f"{label} API Key belum diisi. Silakan isi di tab Pengaturan.",
    }


def complete_json(provider: str, api_key: str, model: str, system: str, user: str,
                  temperature: float) -> str:
    """Asks the chosen text model for a JSON answer and returns the raw text."""
    if provider == "openai":
        return openai_client.complete_json(api_key, model, system, user, temperature)

    if not api_key:
        raise ValueError("Gemini API Key belum diisi. Silakan isi di tab Pengaturan UI.")
    # Keep the Client referenced for the whole call: google-genai closes its HTTP
    # session in Client.__del__, so an inline genai.Client(...).models... would be
    # garbage-collected mid-call ("Cannot send a request, as the client has been closed").
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=temperature,
        ),
    )
    return (response.text or "").strip()
