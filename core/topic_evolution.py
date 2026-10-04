"""
Topic evolution: turns last week's best-performing posts into brand new topics.

The feedback loop only re-weights the topics that already exist. This module goes
one step further: it reads the winners of the last N days and asks the text model to design
fresh topic variants in the same vein, which then enter the rotation with an
above-average starting weight.
"""
import re
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from config import DEFAULT_TEXT_MODEL, DEFAULT_CONTENT_LANGUAGE
from database.models import ContentTopic
from core.ai_provider import complete_json
from core.feedback_loop import window_performance
from core.taxonomy import SEED_TOPICS

logger = logging.getLogger(__name__)

# Same categories as the base curriculum, so variants group with their bases.
ALLOWED_CATEGORIES = sorted({t["category"] for t in SEED_TOPICS})
CATEGORY_CHOICES = " | ".join(ALLOWED_CATEGORIES)

# Starting weight for a new variant: above the 1.0 baseline (it descends from a
# proven winner) but below a topic that has actually earned its numbers.
NEW_TOPIC_WEIGHT = 1.4

MIN_WINNER_REACH = 1  # a winner must have at least some measured reach


def slugify(text_value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (text_value or "").lower()).strip("_")
    return slug[:80] or "topik_baru"


def unique_topic_key(db: Session, base_key: str) -> str:
    key = base_key
    suffix = 2
    while db.query(ContentTopic).filter(ContentTopic.topic_key == key).first():
        key = f"{base_key[:75]}_{suffix}"
        suffix += 1
    return key


def _is_duplicate_title(db: Session, title: str) -> bool:
    """Rejects a variant whose title already exists (case-insensitive)."""
    normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
    for existing in db.query(ContentTopic).all():
        if re.sub(r"\s+", " ", existing.title.strip().lower()) == normalized:
            return True
    return False


def build_evolution_prompt(
    winners: list,
    existing_titles: list,
    max_new: int,
    language: str = DEFAULT_CONTENT_LANGUAGE,
    base_curriculum: list = None,
    window_days: int = 7,
) -> str:
    winner_lines = []
    for i, w in enumerate(winners, start=1):
        winner_lines.append(
            f"{i}. \"{w['topic_title']}\" [{w['category']}]\n"
            f"   - Jangkauan (reach): {w['reach']:,} dari {w['posts']} postingan\n"
            f"   - Reaksi: {w['reactions']}, Komentar: {w['comments']}, Dibagikan: {w['shares']}\n"
            f"   - Konsep inti: {w['core_concept'][:300]}"
        )

    curriculum_lines = []
    for base in (base_curriculum or []):
        curriculum_lines.append(
            f"- [{base['category']}] \"{base['title']}\"\n"
            f"    Prinsip dasar: {base['core_concept'][:260]}"
        )

    caption_lang = "Bahasa Indonesia" if language == "id" else "American English"

    return f"""
KURIKULUM DASAR (materi fondasi yang sudah diajarkan halaman ini).
Semua topik baru WAJIB merupakan pendalaman dari salah satu materi dasar berikut,
bukan konsep yang berdiri sendiri:
{chr(10).join(curriculum_lines) or '- (belum ada materi dasar)'}

TOPIK PEMENANG {window_days} HARI TERAKHIR (diurutkan dari jangkauan tertinggi):
{chr(10).join(winner_lines)}

TOPIK YANG SUDAH ADA (JANGAN diulang atau dibuat mirip):
{chr(10).join('- ' + t for t in existing_titles)}

TUGAS:
Rancang {max_new} topik edukasi BARU tentang pencarian emas/geologi sungai yang:
1. Berakar pada SATU materi dasar di kurikulum di atas (sebutkan judul persisnya
   di field "base_topic") — topik baru adalah bab lanjutan dari materi itu.
2. Mengambil arah yang terbukti diminati, yaitu tema topik pemenang di atas.
3. Menggali aspek yang BELUM dibahas topik manapun di daftar yang sudah ada.
4. Tetap berbasis prinsip fisika/geologi yang benar dan bisa dipraktikkan di lapangan.
5. Punya potensi visual kuat sebagai poster infografis vintage field guide.
6. Judul ditulis dalam {caption_lang} atau Inggris singkat yang menarik.

Balas HANYA dengan JSON valid dengan bentuk:
{{
  "topics": [
    {{
      "base_topic": "Judul PERSIS salah satu materi dasar di kurikulum di atas",
      "category": "{CATEGORY_CHOICES}",
      "title": "Judul topik yang spesifik dan menggugah rasa ingin tahu",
      "core_concept": "2-4 kalimat menjelaskan prinsip geologi/hidrolika di baliknya, termasuk angka atau mekanisme konkret.",
      "visual_blueprint": "Deskripsi detail poster infografis: judul banner, diagram penampang, panah aliran, panel identifikasi mineral, tekstur kertas tua.",
      "why_this_works": "1 kalimat: kenapa topik ini melanjutkan materi dasar tersebut sekaligus tren pemenang di atas."
    }}
  ]
}}
"""


def generate_topic_variants(
    api_key: str,
    winners: list,
    existing_titles: list,
    max_new: int = 2,
    model_name: str = DEFAULT_TEXT_MODEL,
    language: str = DEFAULT_CONTENT_LANGUAGE,
    base_curriculum: list = None,
    window_days: int = 7,
    provider: str = "gemini",
) -> list:
    """Calls the chosen text model and returns the raw list of proposed topic dicts."""
    system_instruction = (
        "You are a senior content strategist and field geologist for a gold prospecting "
        "education page. The page teaches a fixed base curriculum; your job is to write "
        "advanced chapters that deepen that curriculum, never standalone trivia. "
        "Topics must be scientifically accurate, practically useful in the field, and "
        "visually strong as infographic posters. You never repeat an existing topic."
    )

    raw = complete_json(
        provider, api_key, model_name, system_instruction,
        build_evolution_prompt(winners, existing_titles, max_new, language, base_curriculum, window_days),
        0.9,  # higher: we want genuinely new angles
    )
    if not raw:
        raise RuntimeError("Model AI tidak mengembalikan usulan topik apapun.")

    data = json.loads(raw)
    topics = data.get("topics") if isinstance(data, dict) else data
    if not isinstance(topics, list):
        raise RuntimeError("Format usulan topik dari model AI tidak sesuai (bukan list).")
    return topics


STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "how", "why",
    "dan", "yang", "untuk", "dari", "pada", "dengan", "ini", "itu", "di", "ke", "adalah",
    "gold", "emas",  # present in nearly every topic, so useless for matching
}


def _tokens(text_value: str) -> set:
    words = re.findall(r"[a-z]{3,}", (text_value or "").lower())
    return {w for w in words if w not in STOPWORDS}


def _best_content_match(bases: list, variant: dict) -> ContentTopic | None:
    """Picks the base topic whose wording overlaps the variant the most."""
    variant_tokens = _tokens(f"{variant.get('title','')} {variant.get('core_concept','')}")
    if not variant_tokens:
        return None

    best, best_score = None, 0
    for base in bases:
        overlap = len(variant_tokens & _tokens(f"{base.title} {base.core_concept}"))
        if overlap > best_score:
            best, best_score = base, overlap

    # Require a real signal, not one accidental shared word.
    return best if best_score >= 3 else None


def resolve_base_topic(
    db: Session,
    proposed_title: str,
    fallback: ContentTopic | None,
    variant: dict | None = None,
) -> ContentTopic | None:
    """
    Matches the base topic the AI claims to extend against the real curriculum.
    When the claim does not match, falls back to the base whose subject matter is
    closest to the new topic, and only then to the winner's base.
    """
    bases = db.query(ContentTopic).filter(ContentTopic.source == "seed").all()
    wanted = re.sub(r"\s+", " ", (proposed_title or "").strip().lower())

    if wanted:
        for base in bases:
            if re.sub(r"\s+", " ", base.title.strip().lower()) == wanted:
                return base
        # Loose match: the model often shortens the title.
        for base in bases:
            base_norm = re.sub(r"\s+", " ", base.title.strip().lower())
            if wanted in base_norm or base_norm.split(":")[0] in wanted:
                return base
        logger.warning(f"[Topic Evolution] Unknown base topic '{proposed_title}', matching by content.")

    if variant:
        matched = _best_content_match(bases, variant)
        if matched:
            logger.info(f"[Topic Evolution] Linked to base by content: '{matched.title}'")
            return matched

    if fallback is None:
        return None
    # A variant's base is its parent's base; a seed topic is its own base.
    if fallback.source == "seed":
        return fallback
    return fallback.base_topic or fallback.parent


def validate_variant(raw_topic: dict) -> dict | None:
    """Returns a clean topic dict, or None when the proposal is unusable."""
    if not isinstance(raw_topic, dict):
        return None

    def text(key: str) -> str:
        # Models occasionally answer a field with a number or list; never crash on it.
        return str(raw_topic.get(key) or "").strip()

    title = text("title")
    concept = text("core_concept")
    blueprint = text("visual_blueprint")
    category = text("category").title()

    if len(title) < 10 or len(concept) < 40 or len(blueprint) < 40:
        return None
    if category not in ALLOWED_CATEGORIES:
        category = "Geology"  # safe default rather than dropping a good topic

    return {
        "title": title[:200],
        "core_concept": concept,
        "visual_blueprint": blueprint,
        "category": category,
        "base_topic": text("base_topic"),
        "why_this_works": text("why_this_works")[:500],
    }


def evolve_topics(
    db: Session,
    api_key: str,
    model_name: str = DEFAULT_TEXT_MODEL,
    window_days: int = 7,
    max_new: int = 2,
    language: str = DEFAULT_CONTENT_LANGUAGE,
    page_id: int | None = None,
    generator=generate_topic_variants,
    provider: str = "gemini",
) -> dict:
    """
    Reads the winners of the last `window_days` and creates up to `max_new` topics
    derived from them. With `page_id` the winners come from that Fanspage only, so
    each page grows topics that suit its own audience. New topics join the shared
    catalog and become available to every page.

    `generator` is injectable so the pipeline can be tested without the AI API.
    """
    if not api_key:
        return {"success": False, "message": "API Key model teks belum diisi."}

    performance = window_performance(db, window_days, page_id)
    winners = [
        e for e in performance["ranking"]
        if e["measured_posts"] > 0 and e["reach"] >= MIN_WINNER_REACH
    ][:3]

    if not winners:
        return {
            "success": False,
            "message": (
                f"Belum ada data performa dalam {window_days} hari terakhir. "
                "Publikasikan beberapa postingan lalu klik 'Refresh Metrik Facebook' dulu."
            ),
            "window": performance,
        }

    existing_titles = [t.title for t in db.query(ContentTopic).all()]

    # The base curriculum is the learning material every new topic must extend.
    base_curriculum = [
        {"title": t.title, "category": t.category, "core_concept": t.core_concept}
        for t in db.query(ContentTopic)
                   .filter(ContentTopic.source == "seed")
                   .order_by(ContentTopic.weight.desc())
                   .all()
    ]
    if not base_curriculum:
        return {"success": False, "message": "Kurikulum dasar kosong — tidak ada fondasi untuk dipelajari."}

    try:
        proposals = generator(
            api_key=api_key,
            winners=winners,
            existing_titles=existing_titles,
            max_new=max_new,
            model_name=model_name,
            language=language,
            base_curriculum=base_curriculum,
            window_days=window_days,
            provider=provider,
        )
    except Exception as e:
        logger.exception("Topic evolution failed")
        return {"success": False, "message": f"Gagal membuat topik baru: {e}"}

    parent = db.query(ContentTopic).filter(ContentTopic.id == winners[0]["topic_id"]).first()
    created, skipped = [], []

    for raw_topic in proposals[:max_new]:
        variant = validate_variant(raw_topic)
        if not variant:
            skipped.append({"title": str(raw_topic)[:80], "reason": "format tidak lengkap"})
            continue
        if _is_duplicate_title(db, variant["title"]):
            skipped.append({"title": variant["title"], "reason": "judul sudah ada"})
            continue

        base = resolve_base_topic(db, variant["base_topic"], parent, variant)
        topic = ContentTopic(
            category=variant["category"],
            topic_key=unique_topic_key(db, slugify(variant["title"])),
            title=variant["title"],
            core_concept=variant["core_concept"],
            visual_blueprint=variant["visual_blueprint"],
            weight=NEW_TOPIC_WEIGHT,
            source="ai",
            parent_topic_id=parent.id if parent else None,
            base_topic_id=base.id if base else None,
            origin_note=(
                f"Pendalaman materi dasar \"{base.title}\". " if base else ""
            ) + (
                f"Dibuat dari pemenang {window_days} hari terakhir: "
                f"\"{winners[0]['topic_title']}\" (reach {winners[0]['reach']:,}). "
                f"{variant['why_this_works']}"
            ).strip(),
            created_at=datetime.now(timezone.utc),
            is_active=True,
        )
        db.add(topic)
        db.flush()  # assign id before reporting it back
        created.append({
            "id": topic.id,
            "title": topic.title,
            "category": topic.category,
            "weight": topic.weight,
            "base_topic": base.title if base else None,
            "origin_note": topic.origin_note,
        })

    db.commit()

    if not created:
        return {
            "success": False,
            "message": "Gemini tidak menghasilkan topik baru yang layak (semua duplikat atau tidak lengkap).",
            "skipped": skipped,
            "winners": winners,
        }

    logger.info(f"[Topic Evolution] Created {len(created)} new topic(s) from '{winners[0]['topic_title']}'.")
    return {
        "success": True,
        "page_id": page_id,
        "message": f"{len(created)} topik baru dibuat dari pemenang {window_days} hari terakhir.",
        "created": created,
        "skipped": skipped,
        "winners": winners,
        "window_days": window_days,
    }


def retire_topic(db: Session, topic_id: int) -> dict:
    """
    Deactivates a topic instead of deleting it, so published posts keep their
    history. Base curriculum topics cannot be retired: they are the foundation
    every variant is derived from and must keep being taught.
    """
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        return {"success": False, "message": "Topik tidak ditemukan."}

    if topic.source != "ai":
        return {
            "success": False,
            "message": (
                f"'{topic.title}' adalah topik dasar (kurikulum fondasi) dan tidak bisa "
                "dinonaktifkan. Bobotnya akan menyesuaikan sendiri bila kurang diminati."
            ),
        }

    topic.is_active = False
    db.commit()
    return {"success": True, "message": f"Topik turunan '{topic.title}' dinonaktifkan."}


def reactivate_topic(db: Session, topic_id: int) -> dict:
    topic = db.query(ContentTopic).filter(ContentTopic.id == topic_id).first()
    if not topic:
        return {"success": False, "message": "Topik tidak ditemukan."}
    topic.is_active = True
    db.commit()
    return {"success": True, "message": f"Topik '{topic.title}' diaktifkan kembali."}
