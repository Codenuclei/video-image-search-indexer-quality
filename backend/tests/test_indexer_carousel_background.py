from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.db.models import CarouselGenerationSave, DriveFileStatus
from app.workers.indexer import IndexingWorker, _cached_video_recovery_eligible


class _Session:
    def __init__(self, save):
        self.save = save
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, model, key):
        if model is CarouselGenerationSave:
            return self.save
        return None

    async def execute(self, _statement):
        return None

    async def commit(self):
        self.commits += 1


class _CueCountSession:
    """Session stub whose only real answer is the transcript cue count."""

    def __init__(self, cue_count):
        self.cue_count = cue_count
        self.executed = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def scalar(self, _statement):
        return self.cue_count

    async def execute(self, statement):
        self.executed.append(statement)
        return SimpleNamespace(rowcount=0)

    async def commit(self):
        self.commits += 1


class _StatementCaptureSession:
    """Records statements so the real resume query can be asserted on."""

    def __init__(self):
        self.statements = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        rows = ["already_ready"] if len(self.statements) == 1 else ["captioned"]
        return SimpleNamespace(
            rowcount=1,
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )

    async def commit(self):
        pass


def test_cached_video_recovery_requires_transcript_and_frame(tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    assert not _cached_video_recovery_eligible(818, frames_dir)
    (frames_dir / "1.000.jpg").write_bytes(b"jpeg")
    assert _cached_video_recovery_eligible(818, frames_dir)
    assert not _cached_video_recovery_eligible(1, frames_dir)


@pytest.mark.asyncio
async def test_uncaptioned_video_is_skipped_without_claiming_or_erroring():
    """No cues means no slides, so skip quietly instead of recording an error."""
    session = _CueCountSession(0)
    worker = IndexingWorker(lambda: session, settings=get_settings())
    worker._carousel_tasks["v1"] = object()

    await worker._run_carousel_generation("v1")

    assert session.executed == []
    assert session.commits == 0
    assert "v1" not in worker._carousel_tasks


@pytest.mark.asyncio
async def test_orphaned_locks_are_released_so_the_backlog_can_drain():
    session = _StatementCaptureSession()
    worker = IndexingWorker(lambda: session, settings=get_settings())

    await worker.reclaim_stale_carousel_locks(orphaned=True)

    sql = str(session.statements[-1])
    assert "UPDATE drive_files" in sql
    # An orphaned lock is released regardless of age; a kill is not a failure,
    # so the attempt counter is rolled back and the row stays retryable.
    assert "carousel_locked_at" in sql
    assert "carousel_attempts" in sql


@pytest.mark.asyncio
async def test_resume_only_targets_captioned_videos_with_bounded_retries():
    session = _StatementCaptureSession()
    worker = IndexingWorker(lambda: session, settings=get_settings())
    started = []
    worker._start_carousel_task = started.append

    await worker.resume_carousel_generation(limit=2)

    candidate_sql = str(session.statements[-1])
    # Captioned == has a non-empty transcript cue, and a repeatedly failing
    # video must drop out of the drain loop instead of retrying forever.
    assert "EXISTS" in candidate_sql
    assert "video_segments" in candidate_sql
    assert "carousel_attempts <" in candidate_sql
    assert started == ["captioned"]


@pytest.mark.asyncio
async def test_processed_video_runs_full_default_pipeline_without_frontend(monkeypatch):
    save = SimpleNamespace(
        status="ready",
        payload={
            "slides": [{"preview_url": "/media/video/v1/frame?ts=1&cache_only=1"}],
            "carousels": [{"slides": [{"preview_url": "/media/video/v1/frame?ts=1&cache_only=1"}]}],
        },
        source="generate",
        layout_mode="single_1",
    )
    session = _Session(save)
    worker = IndexingWorker(
        lambda: session,
        settings=get_settings(),
    )
    row = SimpleNamespace(id="v1", name="talk.mp4", status=DriveFileStatus.PROCESSED)
    calls = {}

    async def load_cues(_session, _file_id):
        return row, [(0.0, 2.0, "The first exact transcript line."), (3.0, 5.0, "The second exact transcript line.")]

    async def themes(**_kwargs):
        calls["themes"] = True
        return [{"theme_id": "t1", "title": "The first idea", "start_timestamp": 0, "end_timestamp": 5, "summary": "Summary"}], "fallback", None

    async def extract(*_args, **_kwargs):
        calls["extract"] = True
        return {
            "hooks": [{"id": "h1", "text": "The first exact transcript line.", "start_sec": 0, "end_sec": 2}],
            "topics": [{"id": "t1", "text": "The first idea", "start_sec": 0, "end_sec": 5}],
            "topic_tree": [{"text": "The first idea", "hooks": [{"text": "The first exact transcript line."}]}],
        }

    async def generate(body, _session):
        calls["generate"] = body
        assert body.select_images is True
        assert body.hooks and body.topics and body.themes
        return {"save_id": 1}

    monkeypatch.setattr("app.routers.carousel_script._load_video_cues", load_cues)
    monkeypatch.setattr("app.search.carousel_pipeline.build_harmonized_themes", themes)
    monkeypatch.setattr("app.search.carousel_pipeline.extract_hooks_and_topics_async", extract)
    monkeypatch.setattr("app.routers.carousel_script._carousel_pipeline_generate_impl", generate)

    await worker._run_carousel_generation_impl("v1")

    assert calls["themes"] and calls["extract"]
    assert calls["generate"].select_images is True
    assert save.status == "ready"
    assert save.source == "background"
    assert save.payload["themes"]
    assert save.payload["topics"]
    assert save.payload["hooks"]
    assert save.payload["layouts"]["single_1"]["layout_mode"] == "single_1"
    assert save.payload["layouts"]["split_2"]["layout_mode"] == "split_2"
    assert save.payload["images_ready"] is True
    assert save.payload["frames_prewarmed"] is True
