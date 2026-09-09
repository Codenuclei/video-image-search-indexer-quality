"""OCR brand-on-garment association helpers (unit-level, no RapidOCR import)."""
from __future__ import annotations

from types import SimpleNamespace

from app.objects.query_concepts import (
    apparel_brand_association_strength,
    parse_query_concepts,
)


def test_ocr_torso_beats_caption_backdrop():
    concepts = parse_query_concepts("hat with text mastersunion")
    caption = (
        "Two people pose in front of a backdrop featuring the text "
        "'MASTERS UNION' and a giant graduation cap"
    )
    ocr_signage = [
        SimpleNamespace(
            normalized_text="masters union",
            text="MASTERS UNION",
            region="signage",
        )
    ]
    # Signage-only OCR must kill association even if caption is noisy.
    assert apparel_brand_association_strength(
        caption, concepts, ocr_spans=ocr_signage
    ) == 0.0

    ocr_other = [
        SimpleNamespace(
            normalized_text="masters union",
            text="MASTERS UNION",
            region="other",
        )
    ]
    assert apparel_brand_association_strength(
        caption, concepts, ocr_spans=ocr_other
    ) == 0.0

    ocr_upper = [
        SimpleNamespace(
            normalized_text="mastersunion",
            text="mastersunion",
            region="upper",
        )
    ]
    on_hat = apparel_brand_association_strength(
        caption, concepts, ocr_spans=ocr_upper
    )
    assert on_hat >= 0.9


def test_ocr_torso_shirt_query():
    concepts = parse_query_concepts("mastersunion tshirt")
    spans = [
        SimpleNamespace(
            normalized_text="masters union class of 2027",
            text="MASTERS UNION",
            region="torso",
        )
    ]
    assert apparel_brand_association_strength(
        "a person standing outdoors", concepts, ocr_spans=spans
    ) >= 0.95


def test_classify_top_strip_as_signage():
    from app.ocr.rapidocr_engine import BBox, classify_ocr_region

    span = BBox(x=10, y=20, w=400, h=40)
    assert classify_ocr_region(span, torsos=[], image_h=1000, image_w=800) == "signage"
