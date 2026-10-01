"""
OpenAI route for copywriting and poster rendering.

Talks to the REST API directly with `requests` (already a dependency), so no extra
SDK is needed. Error messages never include the API key.
"""
import base64
import logging

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.openai.com/v1"
TEXT_TIMEOUT = 120
IMAGE_TIMEOUT = 300   # image models regularly take more than a minute

# DALL-E 3 only accepts these sizes and prompts up to 4000 characters.
DALLE3_SIZES = {"portrait": "1024x1792", "landscape": "1792x1024", "square": "1024x1024"}
GPT_IMAGE_SIZES = {"portrait": "1024x1536", "landscape": "1536x1024", "square": "1024x1024"}
DALLE3_PROMPT_LIMIT = 4000


def _headers(api_key: str) -> dict:
    if not api_key:
        raise ValueError("OpenAI API Key belum diisi. Silakan isi di tab Pengaturan.")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _error_message(response) -> str:
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {response.status_code}"


def _raise_for_error(response) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI menolak permintaan: {_error_message(response)}")


def orientation(aspect_ratio: str) -> str:
    """'3:4' -> 'portrait'. OpenAI only offers three canvas shapes."""
    try:
        w, h = (float(x) for x in aspect_ratio.split(":"))
        ratio = w / h
    except (ValueError, ZeroDivisionError, AttributeError):
        return "portrait"
    if ratio < 0.95:
        return "portrait"
    if ratio > 1.05:
        return "landscape"
    return "square"


def complete_json(api_key: str, model: str, system: str, user: str, temperature: float) -> str:
    """Returns the raw JSON text produced by a chat model."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    response = requests.post(f"{API_BASE}/chat/completions", headers=_headers(api_key),
                             json=body, timeout=TEXT_TIMEOUT)
    # Reasoning models (gpt-5, o-series) only accept the default temperature.
    if response.status_code == 400 and "temperature" in _error_message(response).lower():
        body.pop("temperature")
        response = requests.post(f"{API_BASE}/chat/completions", headers=_headers(api_key),
                                 json=body, timeout=TEXT_TIMEOUT)
    _raise_for_error(response)

    choice = (response.json().get("choices") or [{}])[0]
    if choice.get("finish_reason") == "content_filter":
        raise RuntimeError("OpenAI memblokir jawaban (content filter).")
    return ((choice.get("message") or {}).get("content") or "").strip()


def generate_image_bytes(api_key: str, model: str, prompt: str, aspect_ratio: str) -> bytes:
    """Renders one image and returns its encoded bytes (PNG/JPEG/WebP)."""
    is_dalle3 = model.lower().startswith("dall-e-3")
    sizes = DALLE3_SIZES if is_dalle3 else GPT_IMAGE_SIZES
    body = {"model": model, "prompt": prompt, "n": 1, "size": sizes[orientation(aspect_ratio)]}
    if is_dalle3:
        body["prompt"] = prompt[:DALLE3_PROMPT_LIMIT]
        body["response_format"] = "b64_json"   # gpt-image models always return base64

    response = requests.post(f"{API_BASE}/images/generations", headers=_headers(api_key),
                             json=body, timeout=IMAGE_TIMEOUT)
    _raise_for_error(response)

    data = response.json().get("data") or []
    encoded = data[0].get("b64_json") if data else None
    if not encoded:
        raise RuntimeError(f"{model}: tidak mengembalikan data gambar")
    return base64.b64decode(encoded)


def test_openai_key(api_key: str, text_model: str, image_model: str) -> dict:
    """
    Checks the key and that both configured models are available to it. Only reads
    model metadata, so it costs no tokens.
    """
    try:
        headers = _headers(api_key)
        for model in dict.fromkeys(m for m in (text_model, image_model) if m):
            response = requests.get(f"{API_BASE}/models/{model}", headers=headers, timeout=20)
            if response.status_code == 404:
                return {"success": False,
                        "message": f"Kunci OpenAI valid, tetapi model '{model}' tidak tersedia untuk akun ini."}
            _raise_for_error(response)
        return {"success": True,
                "message": f"Koneksi OpenAI berhasil! Model: {text_model} & {image_model}"}
    except Exception as e:
        logger.error(f"OpenAI API test error: {e}")
        return {"success": False, "message": f"Gagal terhubung ke OpenAI API: {e}"}
