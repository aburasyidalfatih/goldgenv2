import json
import logging
import re
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

DEFAULT_HASHTAGS = {
    "id": "#emas #geologi #goldprospecting #goldpanning",
    "en": "#goldprospecting #goldpanning #placergold #geology",
}


def _template_caption(title: str, concept: str, language: str) -> str:
    """Same short one-paragraph format the model is asked for."""
    if language == "id":
        return f"{concept} Uji dulang beberapa titik sebelum menggali.\n\n{DEFAULT_HASHTAGS['id']}"
    return f"{concept} Test-pan a few spots before you dig.\n\n{DEFAULT_HASHTAGS['en']}"


def _with_hashtags(caption: str, language: str) -> str:
    """Every caption ends with a hashtag line (Threads also takes its one tag from it)."""
    caption = caption.rstrip()
    if re.search(r"(?:^|\s)#[^\W\d]\w*", caption):
        return caption
    return f"{caption}\n\n{DEFAULT_HASHTAGS.get(language, DEFAULT_HASHTAGS['en'])}"


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
            "black sand and quartz, aged paper texture."
        ),
        "caption": _template_caption(title, topic_dict.get("core_concept", ""), language),
    }

SCENE_LAYOUT = """     * A short, bold slab-serif title of 2-5 words at the top, plus one short punchy subtitle line.
     * ONE dominant hero scene filling about two thirds of the poster: a dramatic, realistic 3D cutaway of the place this topic is about (river, bedrock, hillside or rock outcrop) where the gold is clearly visible: bright, warm gold flakes, nuggets or a glowing paystreak, easy to spot at a glance, exactly where the topic says it collects. Where real gold would be too fine to see, show it in one small magnified circle on the scene.
     * At most 6 short labels (2-4 words each) with leader lines pointing at the key features of that scene.
     * Optionally one narrow strip along the bottom with 3-4 small specimen or field photos, each captioned in 1-3 words."""

SPECIMEN_LAYOUT = """     * A short, bold slab-serif title of 2-5 words at the top, plus one short punchy subtitle line.
     * A SPECIMEN CHART filling at least two thirds of the poster: 4-6 large photo-realistic hand specimens of the rocks or minerals this topic is about, in a clean grid of one or two rows on a natural ground or field-table background, lit like museum specimen photography, so readers can compare them with rocks they have found. No decorative landscape or scenery filling the rest of the poster. Where gold is present, make it bright and easy to spot.
     * Under each specimen, its name in bold and one short identifying feature of 2-6 words (e.g. "brassy cubes, black streak").
     * Optionally make one specimen larger, with a small magnified close-up circle of its key feature."""

# Topics about telling rocks and minerals apart get the specimen chart; topics about
# where and how gold moves or settles keep the hero scene.
SPECIMEN_CATEGORIES = ("Geology", "Minerals")
SPECIMEN_TITLE = re.compile(
    r"quartz|gossan|iron hat|greenstone|schist|\brocks?\b|mariposite|limestone|volcanic|banded iron|\bores?\b"
    r"|mineral|crystal|sulfide|pyrite|galena|sphalerite|\bmica\b|black sand|magnetite|hematite|meteorite"
    r"|\bflakes?\b|\bnuggets?\b|raw gold|specimen|\brust",
    re.I,
)


def poster_layout(topic_dict: dict) -> str:
    """'specimen' for rock and mineral identification topics, 'scene' for everything else."""
    if (topic_dict.get("category") in SPECIMEN_CATEGORIES
            and SPECIMEN_TITLE.search(topic_dict.get("title") or "")):
        return "specimen"
    return "scene"


def generate_post_content(api_key: str, topic_dict: dict, language: str = DEFAULT_CONTENT_LANGUAGE,
                          model_name: str = DEFAULT_TEXT_MODEL, provider: str = "gemini",
                          reasoning: str | None = None) -> dict:
    """
    Generates structured infographic blueprint prompt and Facebook caption with the
    chosen text provider ('gemini' or 'openai').
    """

    if language == "id":
        lang_prompt = "Bahasa Indonesia yang santai, edukatif, berbobot untuk komunitas pencari/penghobi emas sungai."
        hashtag_hint = "#goldprospecting #goldpanning #geologi #berburupenambang #emas dll"
        caption_example = ("Emas tidak mengendap sembarangan. [satu paragraf pendek: alasan fisika/geologinya "
                           "dan satu tips lapangan]\\n\\n#emas #goldprospecting #geologi")
    else:
        lang_prompt = (
            "natural American English for a U.S. audience of recreational gold prospectors "
            "(American spelling and idioms, U.S. customary units such as feet, inches, pounds and "
            "troy ounces, and U.S. goldfields like California, Alaska, Arizona or Georgia where they fit); "
            "friendly, down-to-earth, field-guide expert tone. Do NOT use any Indonesian words"
        )
        hashtag_hint = "e.g. #goldprospecting #goldpanning #placergold #geology #prospecting #goldrush"
        caption_example = ("Gold doesn't settle randomly. [one short paragraph: the physical/geological reason "
                           "and one field tip]\\n\\n#goldprospecting #placergold #geology")

    # Poster and caption rules follow the posts that drew the most views and earnings
    # on a gold-prospecting profile with this audience (Meta export, Aug-Oct 2026):
    # one dramatic scene with visible gold and few words, and a short one-paragraph
    # caption. Long, multi-paragraph, first-person or hedged captions drew the least.
    # Rock and mineral topics use a specimen chart instead: side-by-side realistic
    # specimens are what prospectors compare with their own finds and answer with
    # photos of their collection in the comments.
    specimen = poster_layout(topic_dict) == "specimen"
    layout_rules = SPECIMEN_LAYOUT if specimen else SCENE_LAYOUT
    caption_ending = (
        "End with one short line inviting readers to post a photo of a similar rock or specimen they have "
        "found, in the caption's language (e.g. \"Found a rusty quartz piece like this? Drop a photo below.\")."
        if specimen else "You may end with one short question."
    )
    system_instruction = f"""
You are a World-Class Geological Field Illustrator and Senior Gold Prospecting Educator.
Your task is to create two things for a given gold prospecting topic:
1. An extremely detailed Image Generation Prompt for a vintage national-park-style educational poster (the canvas shape is set separately; keep the title and all text well inside the edges).
   - Layout rules:
{layout_rules}
     * Keep text minimal: no paragraphs, no numbered tip lists, no legends, and no disclaimer or warning boxes.
     * Use the reference blueprint for the subject matter, but follow this layout instead of its panel list.
     * Do not specify colors or paper: the page's own color palette is added to the prompt separately.
2. A Facebook caption written in: {lang_prompt}.
   - ONE short paragraph of 250-450 characters, hashtags not included. No line breaks inside it, no bullet points, no numbered lists.
   - Open with a punchy hook of at most 12 words: a bold claim ("Gold doesn't settle randomly.") or a short curiosity question ("Why does gold hide in the darkest sand?").
   - Then explain the one physical or geological reason in plain words, and give one concrete field tip.
   - Confident and accurate: say what usually happens and why, without disclaimers or hedging asides. No first-person stories, no "save this" call to action, at most one emoji. {caption_ending}
   - Then a blank line and one line of 3-5 relevant hashtags ({hashtag_hint}).

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
    raw_text = complete_json(provider, api_key, model_name, system_instruction, user_prompt, 0.7,
                             reasoning=reasoning)
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

    data["caption"] = _with_hashtags(str(data["caption"]), language)
    return data
