from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runpod" / "qwen-vl"))
from tags import filter_tags


def test_keeps_specific_searchable_labels() -> None:
    text = "\n".join(
        [
            "black shirt",
            "Flourish Foods",
            "Qutub Minar",
            "woman in white blouse",
            "MacBook",
            "Shure microphone",
        ]
    )
    tags = filter_tags(text)
    assert "black shirt" in tags
    assert "flourish foods" in tags
    assert "qutub minar" in tags
    assert "woman in white blouse" in tags
    assert "macbook" in tags
    assert "shure microphone" in tags


def test_drops_generic_body_parts_and_scene() -> None:
    text = "\n".join(
        [
            "eye",
            "mouth",
            "nose",
            "hair",
            "smile",
            "wall",
            "ceiling",
            "light",
            "shirt",
            "logo",
            "sign",
            "text",
            "sleeve",
            "person",
            "man",
            "black shirt",
            "Nike",
        ]
    )
    tags = filter_tags(text)
    assert tags == ["black shirt", "nike"]
