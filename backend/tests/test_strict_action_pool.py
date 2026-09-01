from app.schemas import SearchResultFile
from app.search.local import (
    action_match_keywords,
    build_strict_action_pool,
    caption_contradicts_action,
    caption_matches_action,
    finalize_action_search_results,
)


def _file(fid: str, name: str, score: float, caption: str) -> SearchResultFile:
    return SearchResultFile(
        drive_file_id=fid,
        name=name,
        path=f"/{name}",
        mime_type="image/jpeg",
        score=score,
        caption=caption,
    )


def test_build_strict_action_pool_excludes_unrelated_student_photos():
    query = "students cooking food"
    keywords = action_match_keywords(query)
    keyword_hits = [
        _file("cook1", "cook1.jpg", 0.9, "students cooking in the kitchen"),
    ]
    all_files = keyword_hits + [
        _file("eat1", "eat1.jpg", 0.95, "students eating lunch in cafeteria"),
        _file("grp1", "grp1.jpg", 0.85, "group photo of students smiling"),
        _file("cook2", "cook2.jpg", 0.7, "chopping vegetables on a cutting board"),
    ]
    pool = build_strict_action_pool(all_files, keyword_hits, keywords, query)
    ids = {f.drive_file_id for f in pool}
    assert "cook1" in ids
    assert "cook2" in ids
    assert "eat1" not in ids
    assert "grp1" not in ids


def test_finalize_action_search_results_puts_keyword_hits_first():
    keyword = _file("k1", "k1.jpg", 0.9, "students cooking pasta")
    llm_pass = [
        keyword,
        _file("x1", "x1.jpg", 0.8, "kitchen scene"),
        _file("x2", "x2.jpg", 0.7, "another kitchen"),
    ]
    out = finalize_action_search_results(llm_pass, [keyword], max_results=2)
    assert [f.drive_file_id for f in out] == ["k1", "x1"]


def test_caption_contradicts_cooking_for_eating():
    assert caption_contradicts_action("students eating dinner", "students cooking food")
    assert not caption_contradicts_action("students cooking dinner", "students cooking food")


def test_caption_matches_action_for_cooking():
    keywords = action_match_keywords("students cooking food")
    assert "food" not in keywords or "cooking" in keywords
    assert "eating" not in keywords
    assert caption_matches_action("students cooking in kitchen", keywords)
    assert not caption_matches_action("students eating lunch", keywords)
    assert not caption_matches_action("students standing in hallway", keywords)


def test_rowing_machine_is_object_anchor_but_cooking_is_not():
    from app.search.local import (
        query_has_concrete_object_anchor,
        object_anchor_search_text,
        is_pure_object_anchor_query,
        filter_files_to_object_anchor,
        soften_student_role_for_object_anchor,
        parse_role_context,
        SearchRoleContext,
    )

    assert query_has_concrete_object_anchor("student exercising with rowing machine")
    assert query_has_concrete_object_anchor("rowing machine")
    assert object_anchor_search_text("student exercising with rowing machine") == "rowing machine"
    assert is_pure_object_anchor_query("rowing machine")
    assert is_pure_object_anchor_query("student with rowing machine")
    assert not is_pure_object_anchor_query("student exercising with rowing machine")
    assert not query_has_concrete_object_anchor("students cooking food")
    assert not query_has_concrete_object_anchor("students exercising")

    keep = _file("r1", "r1.jpg", 0.9, "athlete on a Concept2 rowing machine")
    keep_rower = _file("r2", "r2.jpg", 0.9, "athletes sit on rowers indoors at a fitness competition")
    keep_visual = _file("v1", "v1.jpg", 0.94, "athletes training on gym equipment indoors")
    drop_low = _file("l1", "l1.jpg", 0.39, "man poses in front of CLASS OF HYROX backdrop")
    drop = _file("m1", "m1.jpg", 0.95, "people lifting medicine balls in a gym")
    drop_ski = _file("s1", "s1.jpg", 0.95, "woman exercises on a Concept2 SkiErg machine")
    drop_ski_obj = _file("s2", "s2.jpg", 0.96, "A young woman using a ski ergometer machine in a gym setting.")
    drop_ski_obj = drop_ski_obj.model_copy(
        update={
            "matched_objects": [
                {
                    "label": "rowing machine",
                    "category": "sports_equipment",
                    "confidence": 0.9,
                    "source": "caption",
                    "evidence_text": "ergometer",
                    "hit_count": 1,
                }
            ]
        }
    )

    visual_mode = filter_files_to_object_anchor(
        [keep, keep_rower, keep_visual, drop_low, drop, drop_ski, drop_ski_obj],
        "rowing machine",
        mode="visual",
    )
    assert {f.drive_file_id for f in visual_mode} == {"r1", "r2", "v1"}

    soft = filter_files_to_object_anchor(
        [keep, keep_rower, keep_visual, drop_low, drop, drop_ski, drop_ski_obj],
        "student exercising with rowing machine",
        mode="soft",
    )
    assert {f.drive_file_id for f in soft} == {"r1", "r2", "v1"}

    from app.objects.taxonomy import classify_text

    assert not any(x["canonical_label"] == "rowing machine" for x in classify_text(
        "A young woman using a ski ergometer machine in a gym setting."
    ))
    assert any(x["canonical_label"] == "rowing machine" for x in classify_text(
        "athletes training on rowing machines indoors"
    ))

    _, ctx = parse_role_context("student exercising with rowing machine")
    assert ctx.require_all_roles == ("student",)
    soft_role = soften_student_role_for_object_anchor(ctx, object_anchored=True)
    assert soft_role.require_all_roles == ()
    assert soft_role.student_context is True
    hard = soften_student_role_for_object_anchor(
        SearchRoleContext(require_all_roles=("student",), student_context=True),
        object_anchored=False,
    )
    assert hard.require_all_roles == ("student",)


def test_lifting_sandbag_requires_sandbag_evidence():
    from app.search.local import (
        action_match_keywords,
        caption_matches_action,
        caption_contradicts_action,
        query_has_concrete_object_anchor,
        is_pure_object_anchor_query,
        filter_files_to_object_anchor,
    )
    from app.objects.taxonomy import classify_text
    from app.objects.query_concepts import parse_query_concepts

    q = "lifting sandbag"
    concepts = parse_query_concepts(q)
    assert "sandbag" in concepts.taxonomy_labels
    assert query_has_concrete_object_anchor(q)
    assert not is_pure_object_anchor_query(q)
    assert any(x["canonical_label"] == "sandbag" for x in classify_text("holding a sandbag"))

    keywords = action_match_keywords(q)
    assert "sandbag" in keywords
    assert "dumbbell" not in keywords
    assert caption_matches_action(
        "A woman performs weighted lunges holding a sandbag across her shoulders",
        keywords,
    )
    assert not caption_matches_action(
        "People are lifting heavy medicine balls in an athletic training space",
        keywords,
    )
    assert not caption_matches_action(
        "A person carries heavy weights at an event",
        keywords,
    )
    assert caption_contradicts_action(
        "focusing on wall ball throws inside a sports facility",
        q,
    )

    keep = _file("s1", "s1.jpg", 0.56, "woman lunges with a sandbag across her shoulders")
    drop_med = _file("m1", "m1.jpg", 0.56, "people lifting heavy medicine balls indoors")
    drop_wall = _file("w1", "w1.jpg", 0.56, "athletes focusing on wall ball throws")
    drop_kb = _file("k1", "k1.jpg", 0.53, "A person carries heavy weights at an event.")
    drop_kb_named = _file(
        "k2",
        "k2.jpg",
        0.9,
        "athlete walks with kettlebells in each hand",
    )

    evidence = filter_files_to_object_anchor(
        [keep, drop_med, drop_wall, drop_kb, drop_kb_named],
        q,
        mode="evidence",
    )
    assert {f.drive_file_id for f in evidence} == {"s1"}

    # Soft visual scores must not resurrect medicine-ball / kettlebell misses
    # when sandbag evidence is required by the router path.
    soft = filter_files_to_object_anchor(
        [keep, drop_med, drop_wall, drop_kb, drop_kb_named],
        q,
        mode="soft",
    )
    assert "s1" in {f.drive_file_id for f in soft}
    assert "m1" not in {f.drive_file_id for f in soft}
    assert "w1" not in {f.drive_file_id for f in soft}
    assert "k2" not in {f.drive_file_id for f in soft}
