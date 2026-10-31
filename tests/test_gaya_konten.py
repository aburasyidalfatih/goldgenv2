"""Caption & poster format modelled on the posts that went viral with this audience."""
import json
import re

import pytest

import core.gemini_client as gemini_client
from core.imagen_client import AESTHETIC_ENHANCER
from core.taxonomy import SEED_TOPICS


@pytest.fixture
def captured(monkeypatch):
    state = {"caption": "c"}

    def complete_json(provider, api_key, model, system, user, temperature, reasoning=None):
        state.update(system=system, user=user)
        return json.dumps({"visual_title": "WHERE WATER SLOWS", "subtitle": "s",
                           "imagen_prompt": "p", "caption": state["caption"]})

    monkeypatch.setattr(gemini_client, "complete_json", complete_json)
    return state


@pytest.mark.parametrize("language, tag", [("en", "#placergold"), ("id", "#emas")])
def test_caption_tanpa_hashtag_diberi_baris_hashtag(captured, language, tag):
    """Model kadang lupa baris hashtag; Threads juga mengambil tag dari baris itu."""
    captured["caption"] = "The #1 mistake is panning the boil hole. Sample the tail-out instead."

    caption = gemini_client.generate_post_content("k", SEED_TOPICS[0], language=language)["caption"]

    body, hashtags = caption.split("\n\n")
    assert body == captured["caption"]
    assert tag in hashtags


def test_caption_yang_sudah_punya_hashtag_tidak_diubah(captured):
    captured["caption"] = "Gold doesn't settle randomly.\n\n#goldprospecting #geology"
    assert gemini_client.generate_post_content("k", SEED_TOPICS[0], language="en")["caption"] == captured["caption"]


@pytest.mark.parametrize("language", ["en", "id"])
def test_prompt_meminta_caption_pendek_satu_paragraf(captured, language):
    gemini_client.generate_post_content("k", SEED_TOPICS[0], language=language)
    system = captured["system"]

    assert "ONE short paragraph of 250-450 characters" in system
    assert "no bullet points" in system
    assert "punchy hook of at most 12 words" in system
    assert "without disclaimers" in system
    assert "3-5 relevant hashtags" in system
    assert "bullet-point field tips" not in system


def test_prompt_meminta_poster_satu_adegan_dengan_emas_terlihat(captured):
    gemini_client.generate_post_content("k", SEED_TOPICS[0], language="en")
    system = captured["system"]

    assert "ONE dominant hero scene" in system
    assert "gold is clearly visible" in system
    assert "title of 2-5 words" in system
    assert "no disclaimer or warning boxes" in system
    assert "Numbered field tips (1-5)" not in system


def test_gaya_poster_ditambahkan_ke_setiap_prompt_gambar():
    assert "clearly visible gold" in AESTHETIC_ENHANCER
    assert "no disclaimer boxes" in AESTHETIC_ENHANCER
    assert "hero scene" not in AESTHETIC_ENHANCER, "berlaku untuk kedua tata letak"


SPECIMEN_KEYS = {
    "black_sand_indicator", "pyrite_vs_gold_test", "gold_in_quartz_veins", "gossan_iron_hats", "gold_host_rocks",
    "raw_gold_sizes", "mica_vs_gold", "your_first_flake", "mariposite_mother_lode", "carlin_invisible_gold",
    "volcanic_banded_quartz", "banded_iron_formation_gold", "arsenopyrite_gold_sulfide", "test_a_promising_rock",
}


def test_topik_identifikasi_batu_memakai_katalog_spesimen():
    specimen = {t["topic_key"] for t in SEED_TOPICS if gemini_client.poster_layout(t) == "specimen"}
    assert specimen == SPECIMEN_KEYS


@pytest.mark.parametrize("topic, layout", [
    ({"category": "Minerals", "title": "Gossan or Just Rusty Float?"}, "specimen"),
    ({"category": "Geology", "title": "Read the Crystal Faces"}, "specimen"),
    ({"category": "Equipment", "title": "Hot Rocks & Ground Balance"}, "scene"),
    ({"category": "Fluvial", "title": "The Boulder Shadow: How Giant Rocks Catch Heavy Gold"}, "scene"),
    ({"category": "Geology", "title": "Bedrock Crevices: Nature's Ultimate Natural Gold Sluice"}, "scene"),
])
def test_variasi_ikut_dipilah_dari_judul_dan_kategori(topic, layout):
    assert gemini_client.poster_layout(topic) == layout


def test_topik_batuan_meminta_katalog_dan_ajakan_kirim_foto(captured):
    pyrite = next(t for t in SEED_TOPICS if t["topic_key"] == "pyrite_vs_gold_test")
    gemini_client.generate_post_content("k", pyrite, language="en")
    system = captured["system"]

    assert "SPECIMEN CHART" in system and "photo-realistic hand specimens" in system
    assert "inviting readers to post a photo" in system
    assert "ONE dominant hero scene" not in system and "You may end with one short question" not in system


def test_topik_sungai_tetap_adegan_tanpa_ajakan_foto(captured):
    gemini_client.generate_post_content("k", SEED_TOPICS[0], language="en")
    system = captured["system"]

    assert "ONE dominant hero scene" in system and "You may end with one short question" in system
    assert "SPECIMEN CHART" not in system and "inviting readers to post a photo" not in system


@pytest.mark.parametrize("language", ["en", "id"])
def test_caption_cadangan_juga_satu_paragraf_lalu_hashtag(language):
    for topic in SEED_TOPICS:
        caption = gemini_client.template_content(topic, language)["caption"]
        body, hashtags = caption.split("\n\n")
        assert "\n" not in body
        assert len(body) <= 700, topic["topic_key"]
        assert 3 <= len(re.findall(r"#\w+", hashtags)) <= 5
