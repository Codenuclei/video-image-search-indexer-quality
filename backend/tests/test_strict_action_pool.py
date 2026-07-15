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
