import json
import logging
from google import genai
from google.genai import types
from config import DEFAULT_TEXT_MODEL, DEFAULT_CONTENT_LANGUAGE, GEMINI_TEXT_TIMEOUT_MS
from core.ai_provider import complete_json, PROVIDER_LABELS

logger = logging.getLogger(__name__)

def get_gemini_client(api_key: str):
    if not api_key:
        raise ValueError("Gemini API Key belum diisi. Silakan isi di tab Pengaturan UI.")
    return genai.Client(api_key=api_key,
                        http_options=types.HttpOptions(timeout=GEMINI_TEXT_TIMEOUT_MS))

def test_gemini_key(api_key: str, model_name: str = DEFAULT_TEXT_MODEL) -> dict:
    """
    Validates Gemini API Key by sending a small ping request.
    """
    try:
        client = get_gemini_client(api_key)
        response = client.models.generate_content(
            model=model_name,
            contents="Say 'OK' if you can read this.",
        )
        text = response.text or ""
        return {
            "success": True,
            "message": f"Koneksi Gemini API berhasil! Model: {model_name}",
            "response": text.strip()
        }
    except Exception as e:
        logger.error(f"Gemini API test error: {e}")
        return {
            "success": False,
            "message": f"Gagal terhubung ke Gemini API: {str(e)}"
        }

def _template_caption(title: str, concept: str, language: str) -> str:
    if language == "id":
        return (
            f"💡 EDUKASI PROSPEKSI EMAS: {title}\n\n{concept}\n\n"
            "📌 Tips Lapangan:\n"
            "1. Selalu uji sampel dengan dulang di berbagai kedalaman.\n"
            "2. Perhatikan konsentrasi pasir besi hitam (magnetite).\n"
            "3. Cari celah di batuan dasar (bedrock).\n\n"
            "Bagaimana pengalaman Anda saat mendulang di titik seperti ini? "
            "Tulis di kolom komentar! 👇\n\n"
            "#emas #geologi #prospeksiemas #goldpanning #outdooradventure"
        )
    return (
        f"💡 GOLD PROSPECTING 101: {title}\n\n{concept}\n\n"
        "📌 Field Tips:\n"
        "1. Always test-pan samples from several depths.\n"
        "2. Watch for heavy concentrations of black sand (magnetite).\n"
        "3. Hunt for cracks and crevices in the bedrock.\n\n"
        "Ever panned a spot like this? "
        "Tell us how it went in the comments! 👇\n\n"
        "#goldprospecting #goldpanning #geology #placergold #outdoors"
    )


def template_content(topic_dict: dict, language: str = DEFAULT_CONTENT_LANGUAGE) -> dict:
    """
    Deterministic offline blueprint used when the model output is unusable.
    """
    title = topic_dict.get("title", "GOLD PROSPECTING FIELD GUIDE")
    return {
        "visual_title": title,
        "subtitle": "Understanding Natural Gold Traps in Rivers",
        "imagen_prompt": (
            f"Vintage field guide educational infographic poster titled '{title}'. "
            "Detailed geological cross section diagram of river, water dynamics arrows, "
            "bedrock gravel layers with gold nuggets, bottom panel with macro photos of "
            "black sand and quartz, aged paper texture, 3:4 aspect ratio."
        ),
        "caption": _template_caption(title, topic_dict.get("core_concept", ""), language),
    }

def generate_post_content(api_key: str, topic_dict: dict, language: str = DEFAULT_CONTENT_LANGUAGE,
                          model_name: str = DEFAULT_TEXT_MODEL, provider: str = "gemini") -> dict:
    """
    Generates structured infographic blueprint prompt and Facebook caption with the
    chosen text provider ('gemini' or 'openai').
    """

    if language == "id":
        lang_prompt = "Bahasa Indonesia yang santai, edukatif, berbobot untuk komunitas pencari/penghobi emas sungai."
        hashtag_hint = "#goldprospecting #goldpanning #geologi #berburupenambang #emas dll"
        caption_example = "Pernahkah Anda bertanya mengapa emas selalu berkumpul di tikungan dalam sungai?...\\n\\n[caption lengkap]"
    else:
        lang_prompt = (
            "natural American English for a U.S. audience of recreational gold prospectors "
            "(American spelling and idioms, U.S. customary units such as feet, inches, pounds and "
            "troy ounces, and U.S. goldfields like California, Alaska, Arizona or Georgia where they fit); "
            "friendly, down-to-earth, field-guide expert tone. Do NOT use any Indonesian words"
        )
        hashtag_hint = "e.g. #goldprospecting #goldpanning #placergold #geology #prospecting #goldrush"
        caption_example = "Ever wonder why gold always piles up on the inside bend of a creek?...\\n\\n[full caption]"

    system_instruction = f"""
You are a World-Class Geological Field Illustrator and Senior Gold Prospecting Educator.
Your task is to create two things for a given gold prospecting topic:
1. An extremely detailed, structured Image Generation Prompt for Imagen 3 to create a vintage national-park-style educational infographic poster (aspect ratio 3:4 portrait).
   - The visual MUST follow the formula of high-end geological field manuals:
     * A bold, aged slab-serif banner title at the top.
     * A 3D cutaway / cross-section diagram showing water dynamics (arrows for current speed), gravel stratification, and bedrock cracks trapping gold flakes.
     * Bottom section split into 4-5 identification panels / macro mineral photos (magnetite black sand, quartz, garnet, nuggets).
     * Numbered field tips (1-5) and aged parchment/topographic aesthetic.
2. A viral, high-value Facebook Caption written in: {lang_prompt}.
   - The caption must include:
     * An attention-grabbing hook question.
     * The physical/geological principle explained simply (why gold settles here).
     * 3-4 bullet-point field tips on how to sample this in the field.
     * An engaging call to action asking followers to share their experience.
     * 4-6 relevant hashtags ({hashtag_hint}).

Output MUST be strictly valid JSON matching this schema:
{{
  "visual_title": "THE INSIDE BEND RULE",
  "subtitle": "Why Heavy Gold Settles On The Inside Curve",
  "imagen_prompt": "Vintage field guide educational infographic poster titled 'THE INSIDE BEND RULE'...",
  "caption": "{caption_example}"
}}
"""

    user_prompt = f"""
Topik Edukasi:
- Judul: {topic_dict.get('title')}
- Kategori: {topic_dict.get('category')}
- Konsep Dasar: {topic_dict.get('core_concept')}
- Blueprint Visual Acuan: {topic_dict.get('visual_blueprint')}

Tolong buatkan visual prompt Imagen 3 dan caption Facebook yang berkualitas tinggi dan siap tayang!
"""

    # API/network/auth errors are intentionally NOT caught here: they must surface
    # to the caller instead of being masked by generic fallback copy.
    raw_text = complete_json(provider, api_key, model_name, system_instruction, user_prompt, 0.7)
    if not raw_text:
        raise RuntimeError(
            f"{PROVIDER_LABELS.get(provider, provider)} tidak mengembalikan teks apapun "
            "(kemungkinan diblokir safety filter atau model tidak mendukung response JSON)."
        )

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        # Only malformed JSON falls back to the built-in template.
        logger.error(f"Model returned invalid JSON ({e}). Using template fallback.")
        return template_content(topic_dict, language)

    if not isinstance(data, dict):
        logger.error("Gemini JSON is not an object. Using template fallback.")
        return template_content(topic_dict, language)

    # Fill in any key the model omitted so callers can rely on the schema.
    template = template_content(topic_dict, language)
    for key, value in template.items():
        if not data.get(key):
            logger.warning(f"Gemini response missing '{key}'. Using template value.")
            data[key] = value

    return data
