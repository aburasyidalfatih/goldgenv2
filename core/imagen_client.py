import io
import uuid
import logging
from PIL import Image
from google import genai
from google.genai import types
from config import IMAGES_DIR, DEFAULT_IMAGE_MODEL, DEFAULT_IMAGE_FALLBACK_MODEL, GEMINI_IMAGE_TIMEOUT_MS
from core import openai_client

logger = logging.getLogger(__name__)

# Enhance every prompt for the vintage field guide look
AESTHETIC_ENHANCER = (
    " Highly detailed vintage geological field guide poster, national park educational chart aesthetic. "
    "Sharp typography, isometric river cutaway diagram, macro mineral texture photography, "
    "aged paper parchment background, crisp contrast, museum quality informational graphic."
)


def _save_jpeg(image_bytes: bytes, filepath) -> None:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(filepath, format="JPEG", quality=95)


def _generate_via_gemini(client, model_name: str, prompt: str, aspect_ratio: str, filepath) -> bool:
    """Native Gemini image model route. Returns True if an image was saved."""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        # Without image_config Gemini image models render square posters.
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        )
    )
    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            if part.inline_data and part.inline_data.data:
                _save_jpeg(part.inline_data.data, filepath)
                logger.info(f"Gemini image ({model_name}) saved to {filepath}")
                return True
    return False


def _generate_via_imagen(client, model_name: str, prompt: str, aspect_ratio: str, filepath) -> bool:
    """Imagen route. Returns True if an image was saved."""
    result = client.models.generate_images(
        model=model_name,
        prompt=prompt,
        config=dict(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
            output_mime_type="image/jpeg",
        )
    )
    if result.generated_images:
        _save_jpeg(result.generated_images[0].image.image_bytes, filepath)
        logger.info(f"Imagen image ({model_name}) saved to {filepath}")
        return True
    return False


def generate_poster_image(
    api_key: str,
    prompt: str,
    aspect_ratio: str = "3:4",
    model_name: str = DEFAULT_IMAGE_MODEL,
    provider: str = "gemini",
) -> tuple[str, str]:
    """
    Generates an educational infographic poster. With provider 'gemini' it uses
    Gemini image models or Imagen, falling back to the other family if the primary
    model fails; with 'openai' it uses the configured OpenAI image model.
    Returns:
        (image_filename, absolute_path)
    """
    filename = f"gold_poster_{uuid.uuid4().hex[:10]}.jpg"
    filepath = IMAGES_DIR / filename
    full_prompt = prompt + AESTHETIC_ENHANCER

    logger.info(f"Generating image with {provider}:{model_name}, aspect_ratio={aspect_ratio}...")

    if provider == "openai":
        try:
            image_bytes = openai_client.generate_image_bytes(api_key, model_name, full_prompt, aspect_ratio)
            _save_jpeg(image_bytes, filepath)
        except Exception as err:
            raise RuntimeError(f"Gagal menghasilkan gambar. {model_name}: {err}") from err
        logger.info(f"OpenAI image ({model_name}) saved to {filepath}")
        return filename, str(filepath)

    if not api_key:
        raise ValueError("Gemini API Key belum diisi.")
    client = genai.Client(api_key=api_key,
                          http_options=types.HttpOptions(timeout=GEMINI_IMAGE_TIMEOUT_MS))

    is_gemini_primary = "gemini" in model_name.lower()
    if is_gemini_primary:
        primary = (_generate_via_gemini, model_name)
        fallback = (_generate_via_imagen, DEFAULT_IMAGE_FALLBACK_MODEL)
    else:
        primary = (_generate_via_imagen, model_name)
        fallback = (_generate_via_gemini, DEFAULT_IMAGE_MODEL)

    errors = []
    for route, route_model in (primary, fallback):
        try:
            if route is _generate_via_gemini:
                saved = _generate_via_gemini(client, route_model, full_prompt, aspect_ratio, filepath)
            else:
                saved = _generate_via_imagen(client, route_model, full_prompt, aspect_ratio, filepath)

            if saved:
                return filename, str(filepath)

            errors.append(f"{route_model}: tidak mengembalikan data gambar")
            logger.warning(f"Model {route_model} returned no image data.")
        except Exception as err:
            errors.append(f"{route_model}: {err}")
            logger.warning(f"Image generation failed with {route_model}: {err}")

    raise RuntimeError("Gagal menghasilkan gambar. " + " | ".join(errors))
