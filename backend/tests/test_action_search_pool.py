from app.schemas import SearchResultFile
from app.search.local import merge_action_search_pool


def _file(fid: str, name: str, score: float, caption: str = "x") -> SearchResultFile:
    return SearchResultFile(
        drive_file_id=fid,
        name=name,
        path=f"/{name}",
        mime_type="image/jpeg",
        score=score,
        caption=caption,
    )


def test_merge_action_search_pool_puts_keywords_first():
    all_files = [
        _file("a", "a.jpg", 0.9, "group photo"),
        _file("b", "b.jpg", 0.8, "cheque ceremony"),
        _file("c", "c.jpg", 0.7, "stage"),
    ]
    keywords = [_file("b", "b.jpg", 0.8, "cheque ceremony")]
    merged = merge_action_search_pool(all_files, keywords, max_pool=10)
    assert [f.drive_file_id for f in merged[:2]] == ["b", "a"]
