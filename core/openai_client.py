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
# gpt-image-2 and later accept any size (edges multiples of 16, 655,360 to
# 8,294,400 pixels), so posters come out exactly in the Fanspage's ratio.
# 1088x1360 is 4:5 at about Instagram's 1080 width, with fewer pixels (and
# image tokens) than the 1024x1536 portrait canvas.
GPT_IMAGE_EXACT_SIZES = {"4:5": "1088x1360", "1:1": "1024x1024"}


def _accepts_any_size(model: str) -> bool:
    m = model.lower()
    return m.startswith("gpt-image-") and not m.startswith("gpt-image-1")


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
    """'4:5' -> 'portrait'. OpenAI only offers three canvas shapes."""
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


# model -> optional parameters it has rejected. Remembered so every later call
# skips them up front instead of paying a failed request first each time.
_REJECTED_PARAMS: dict = {}


def _post_dropping_unsupported(url: str, api_key: str, body: dict, optional: tuple, timeout: int):
    """
    POSTs `body`; when the model rejects one of the `optional` parameters (e.g. a
    reasoning model refusing `temperature`, or a non-reasoning model refusing
    `reasoning_effort`), drops that parameter and tries again.
    """
    known = _REJECTED_PARAMS.setdefault(body.get("model"), set())
    for key in known:
        body.pop(key, None)
    while True:
        response = requests.post(url, headers=_headers(api_key), json=body, timeout=timeout)
        if response.status_code != 400:
            return response
        message = _error_message(response).lower()
        rejected = next((key for key in optional if key in body and key in message), None)
        if rejected is None:
            return response
        logger.info(f"OpenAI model rejected '{rejected}'; retrying without it.")
        known.add(rejected)
        body.pop(rejected)


def complete_json(api_key: str, model: str, system: str, user: str, temperature: float,
                  reasoning: str | None = None) -> str:
    """
    Returns the raw JSON text produced by a chat model. `reasoning` ('low',
    'medium', 'high') caps how much a reasoning model thinks before answering;
    thinking tokens are billed as output, so a low effort makes captions cheaper.
    """
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    if reasoning:
        body["reasoning_effort"] = reasoning
    response = _post_dropping_unsupported(f"{API_BASE}/chat/completions", api_key, body,
                                          ("temperature", "reasoning_effort"), TEXT_TIMEOUT)
    _raise_for_error(response)

    choice = (response.json().get("choices") or [{}])[0]
    if choice.get("finish_reason") == "content_filter":
        raise RuntimeError("OpenAI memblokir jawaban (content filter).")
    return ((choice.get("message") or {}).get("content") or "").strip()


def generate_image_bytes(api_key: str, model: str, prompt: str, aspect_ratio: str,
                         quality: str | None = None) -> bytes:
    """
    Renders one image and returns its encoded bytes (PNG/JPEG/WebP). `quality`
    ('low', 'medium', 'high', 'auto') sets how many image tokens are spent, which
    is most of the price: 'auto' lets the model pick, often an expensive level.
    """
    is_dalle3 = model.lower().startswith("dall-e-3")
    sizes = DALLE3_SIZES if is_dalle3 else GPT_IMAGE_SIZES
    canvas = sizes[orientation(aspect_ratio)]
    exact = GPT_IMAGE_EXACT_SIZES.get(aspect_ratio) if _accepts_any_size(model) else None
    body = {"model": model, "prompt": prompt, "n": 1, "size": exact or canvas}
    if is_dalle3:
        body["prompt"] = prompt[:DALLE3_PROMPT_LIMIT]
        body["response_format"] = "b64_json"   # gpt-image models always return base64
    elif quality:
        body["quality"] = quality

    response = _post_dropping_unsupported(f"{API_BASE}/images/generations", api_key, body,
                                          ("quality",), IMAGE_TIMEOUT)
    if (exact and exact != canvas and response.status_code == 400
            and "size" in _error_message(response).lower()):
        # A model that only knows the standard canvases: use the nearest one
        # (posters outside Instagram's range are padded when sent there).
        logger.info(f"OpenAI model '{model}' rejected size {exact}; using {canvas}.")
        body["size"] = canvas
        response = _post_dropping_unsupported(f"{API_BASE}/images/generations", api_key, body,
                                              ("quality",), IMAGE_TIMEOUT)
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
