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
    assert "hero scene with clearly visible gold" in AESTHETIC_ENHANCER
    assert "no disclaimer boxes" in AESTHETIC_ENHANCER


@pytest.mark.parametrize("language", ["en", "id"])
def test_caption_cadangan_juga_satu_paragraf_lalu_hashtag(language):
    for topic in SEED_TOPICS:
        caption = gemini_client.template_content(topic, language)["caption"]
        body, hashtags = caption.split("\n\n")
        assert "\n" not in body
        assert len(body) <= 700, topic["topic_key"]
        assert 3 <= len(re.findall(r"#\w+", hashtags)) <= 5
