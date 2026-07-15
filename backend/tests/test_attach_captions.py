from app.qdrant.image_captions import is_valid_caption


def test_is_valid_caption_threshold():
    assert is_valid_caption("Two men seated in pink armchairs conversing on stage", min_words=4)
    assert not is_valid_caption("photo", min_words=4)
