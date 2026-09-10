"""Face GPU client: JPEG bytes only, never Drive URLs or the Qwen endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import numpy as np

from app.config import Settings
from app.faces.runpod_gpu import (
    build_face_batch_payload,
    build_face_payload,
    build_face_video_payload,
    encode_face_jpeg,
    jpeg_scale,
    parse_face_output,
    payload_contains_drive_url,
    runpod_face_configured,
    serverless_worker_scale,
)


def test_face_payload_is_bytes_not_drive_url() -> None:
    payload = build_face_payload(b"\xff\xd8fakejpeg", drive_file_id="abc")
    assert "image_b64" in payload
    assert "http" not in str(payload)
    assert payload_contains_drive_url(payload) is False


def test_face_payload_rejects_drive_url_blob() -> None:
    payload = {
        "image_url": "https://drive.google.com/file/d/x/view",
        "image_b64": "",
    }
    assert payload_contains_drive_url(payload) is True


def test_runpod_face_needs_api_key() -> None:
    settings = Settings(_env_file=None, runpod_api_key="", runpod_face_gpu_enabled=True)  # type: ignore[call-arg]
    assert runpod_face_configured(settings) is False
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
    )
    assert runpod_face_configured(settings) is True


def test_runpod_face_refuses_qwen_endpoint() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="ya97mr5kgtwdi1",
        runpod_qwen_endpoint_id="ya97mr5kgtwdi1",
        runpod_face_gpu_enabled=True,
    )
    assert runpod_face_configured(settings) is False


def test_parse_face_output_embeddings() -> None:
    vec = [0.0] * 512
    vec[0] = 1.0
    faces = parse_face_output(
        {
            "providers": ["CUDAExecutionProvider"],
            "images": [
                {
                    "faces": [
                        {
                            "bbox_x": 1,
                            "bbox_y": 2,
                            "bbox_width": 10,
                            "bbox_height": 12,
                            "confidence": 0.9,
                            "embedding": vec,
                        }
                    ]
                }
            ],
        }
    )
    assert len(faces) == 1
    assert faces[0].embedding[0] == 1.0
    assert len(faces[0].embedding) == 512


def test_parse_face_output_keeps_full_float_and_thumbnail() -> None:
    import base64

    vec = [0.123456789] * 512
    thumb = base64.b64encode(b"jpeg-bytes").decode("ascii")
    faces = parse_face_output(
        {
            "images": [
                {
                    "faces": [
                        {
                            "bbox_x": 1,
                            "bbox_y": 2,
                            "bbox_width": 10,
                            "bbox_height": 12,
                            "confidence": 0.9,
                            "embedding": vec,
                            "thumbnail_b64": thumb,
                        }
                    ]
                }
            ]
        }
    )
    assert faces[0].embedding[0] == 0.123456789
    assert faces[0].thumbnail_jpeg == b"jpeg-bytes"


def test_jpeg_scale_zero_is_full_res() -> None:
    assert jpeg_scale(4000, 3000, 0) == 1.0
    assert jpeg_scale(4000, 3000, 1600) < 1.0


def test_serverless_scale_min_one_under_load_zero_when_idle() -> None:
    assert serverless_worker_scale(active=True) == {"workersMin": 1, "workersMax": 1}
    assert serverless_worker_scale(active=True, workers_max=4) == {
        "workersMin": 1,
        "workersMax": 4,
    }
    assert serverless_worker_scale(active=True, workers_max=99) == {
        "workersMin": 1,
        "workersMax": 8,
    }
    assert serverless_worker_scale(active=False, workers_max=4) == {
        "workersMin": 0,
        "workersMax": 0,
    }


def test_set_face_workers_patches_min_one_on_load() -> None:
    import asyncio

    from app.faces import runpod_gpu as gpu

    gpu._scaled = None
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
    )
    patches: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def patch(self, url: str, headers: dict, json: dict):
            patches.append(json)
            return _Resp()

    with patch("app.faces.runpod_gpu.httpx.AsyncClient", _Client):
        asyncio.run(gpu.set_face_workers_max(settings, 1))
        asyncio.run(gpu.set_face_workers_max(settings, 1))
        asyncio.run(gpu.set_face_workers_max(settings, 0))
    gpu._scaled = None
    assert patches == [
        {"workersMin": 1, "workersMax": 1},
        {"workersMin": 0, "workersMax": 0},
    ]


def test_set_face_workers_uses_settings_max_under_load() -> None:
    import asyncio

    from app.faces import runpod_gpu as gpu

    gpu._scaled = None
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
        runpod_face_workers_max=4,
    )
    patches: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def patch(self, url: str, headers: dict, json: dict):
            patches.append(json)
            return _Resp()

    with patch("app.faces.runpod_gpu.httpx.AsyncClient", _Client):
        asyncio.run(gpu.set_face_workers_max(settings, 1))
        asyncio.run(gpu.set_face_workers_max(settings, 0))
    gpu._scaled = None
    assert patches == [
        {"workersMin": 1, "workersMax": 4},
        {"workersMin": 0, "workersMax": 0},
    ]


def test_face_endpoint_script_is_serverless_scale_to_zero() -> None:
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath(
        "scripts", "runpod_create_face_endpoint.py"
    ).read_text()
    assert 'isServerless": True' in text
    assert '"workersMin": 0' in text
    assert '"workersMax": 0' in text
    assert "REQUEST_COUNT" in text
    assert "3600000" in text
    assert "proxy.runpod.net" not in text


def test_encode_face_jpeg_max_edge_zero_does_not_downscale() -> None:
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    image[:] = (10, 20, 30)
    encode_face_jpeg(image, max_edge=0, quality=95)
    assert jpeg_scale(80, 120, 0) == 1.0


def test_batch_payload_is_images_not_drive_url() -> None:
    payload = build_face_batch_payload([(b"\xff\xd8a", "file1"), (b"\xff\xd8b", "file1")])
    assert len(payload["images"]) == 2
    assert payload_contains_drive_url(payload) is False


def test_video_payload_is_signed_url_and_timestamps_not_drive_url() -> None:
    payload = build_face_video_payload(
        [0.0, 1.5],
        video_url="https://api.example.test/internal/face-gpu-video/vid1?exp=1&sig=abc",
        drive_file_id="vid1",
    )
    assert payload["video_url"].startswith("https://")
    assert "video_b64" not in payload
    assert payload["timestamps"] == [0.0, 1.5]
    assert payload["video_max_bytes"] == 10 * 1024 * 1024 * 1024
    assert payload_contains_drive_url(payload) is False


def test_video_payload_rejects_drive_url() -> None:
    payload = build_face_video_payload(
        [0.0],
        video_url="https://drive.google.com/uc?id=abc",
        drive_file_id="vid1",
    )
    assert payload_contains_drive_url(payload) is True


def test_signed_face_video_pull_url_uses_public_base() -> None:
    from app.faces.video_pull import signed_face_video_pull_url, verify_face_video_pull

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        public_base_url="https://api.165.245.170.117.sslip.io",
        runpod_api_key="rp_test",
    )
    url = signed_face_video_pull_url("file-1", settings)
    assert url.startswith("https://api.165.245.170.117.sslip.io/internal/face-gpu-video/file-1?")
    assert "drive.google.com" not in url
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    exp = int(query["exp"][0])
    sig = query["sig"][0]
    assert verify_face_video_pull("file-1", exp, sig, "rp_test") is True
    assert verify_face_video_pull("file-1", exp, "deadbeef", "rp_test") is False


def test_detect_faces_runpod_video_does_not_base64_the_file() -> None:
    from app.faces import runpod_gpu as gpu
    import inspect

    src = inspect.getsource(gpu.detect_faces_runpod_video)
    assert "read_bytes" not in src
    assert "signed_face_video_pull_url" in src
    assert "video_url" in src


def test_video_pipeline_uses_runpod_batch() -> None:
    from app.pipelines import video as video_mod
    import inspect

    src = inspect.getsource(video_mod.apply_faces_to_prepared_video)
    assert "detect_faces_runpod_video" in src
    assert "extract_frame_at" in src  # CPU fallback only
    assert "unlink_drive_source_cache" in src
    assert "RunPodFaceError" in src
    inline = inspect.getsource(video_mod.process_video_file)
    assert "detect_faces_runpod_batch" in inline
    extract = inspect.getsource(video_mod._extract_index_frames)
    assert "extract_frames_runpod" in extract
    assert "extract_frame_at" in extract
    assert "RunPodFaceError" in extract


def test_extract_frames_payload_is_signed_pull_not_drive() -> None:
    payload = build_face_video_payload(
        [0.0, 1.0],
        video_url="https://dfi-backend-production.up.railway.app/internal/face-gpu-video/x?exp=1&sig=abc",
        drive_file_id="abc",
        extract_only=True,
    )
    assert payload["extract_only"] is True
    assert payload["return_jpegs"] is True
    assert payload["max_width"] == 960
    assert "video_url" in payload
    assert payload_contains_drive_url(payload) is False
    assert "image_b64" not in payload


def test_face_queue_skips_local_engine_when_gpu_configured() -> None:
    from app.workers import face_queue as face_mod
    import inspect

    src = inspect.getsource(face_mod.process_face_job)
    assert "runpod_face_configured" in src
    assert "use_runpod=use_gpu" in src
    assert src.index("runpod_face_configured") < src.index("get_face_engine")


def test_face_worker_loop_keeps_claiming_while_ingest_paused() -> None:
    from app.workers import face_queue as face_mod
    import inspect

    src = inspect.getsource(face_mod.FaceWorkerLoop._worker_loop)
    assert "global_indexing_is_paused" not in src
    assert "claim_face_jobs" in src


def test_reverse_face_search_uses_runpod_when_configured() -> None:
    from app.reid import face_search as face_search_mod
    import inspect

    src = inspect.getsource(face_search_mod.search_faces_by_image_bytes)
    assert "detect_faces_runpod" in src
    assert "runpod_face_configured" in src


def test_api_leader_starts_face_loop_alongside_identify() -> None:
    from app import main as main_mod
    import inspect

    src = inspect.getsource(main_mod.lifespan)
    assert "IdentifyWorkerLoop" in src
    assert "FaceWorkerLoop" in src
    assert "OcrWorkerLoop" in src
    assert "runpod_face_configured" in src
    assert "not settings_now.run_indexer" in src
    assert "ObjectWorkerLoop(yield_to_faces=False)" in src


def test_identify_and_object_ignore_global_ingest_pause() -> None:
    from pathlib import Path

    from app.workers import identify_queue as ident
    from app.workers import index_control as control
    from app.workers import maintenance as maint
    from app.workers import object_queue as obj
    import inspect

    assert "global_indexing_is_paused" not in Path(ident.__file__).read_text()
    assert "lane_paused_folder_paths" in Path(ident.__file__).read_text()
    assert "global_indexing_is_paused" not in inspect.getsource(obj.produce_object_backfill)
    assert "lane_paused_folder_paths" in inspect.getsource(obj.produce_object_backfill)
    control_src = inspect.getsource(control.index_control_watch_loop)
    assert "startup-maintenance" not in control_src
    assert "task.get_name()" not in control_src
    tick_src = inspect.getsource(maint.maintenance_tick)
    assert "Ingest paused — caption/object/identify lanes still run" in tick_src
    assert "Maintenance skipped: global indexing pause is active" not in tick_src



def test_image_pipeline_uses_gpu_when_configured() -> None:
    from app.pipelines import image as image_mod
    import inspect

    src = inspect.getsource(image_mod.apply_faces_to_prepared_image)
    assert "detect_faces_runpod" in src
    assert "use_runpod" in src
    assert "RunPodFaceError" in src


@patch("app.faces.runpod_gpu.set_face_workers_max", new_callable=AsyncMock)
def test_detect_faces_runpod_posts_input_bytes(_scale: AsyncMock) -> None:
    import asyncio

    from app.faces.runpod_gpu import detect_faces_runpod

    vec = [0.0] * 512
    vec[3] = 1.0
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        runpod_api_key="rp_test",
        runpod_face_endpoint_id="0zub88paibpsf3",
        runpod_face_gpu_enabled=True,
        runpod_face_max_edge=64,
    )
    image = np.zeros((32, 32, 3), dtype=np.uint8)

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self._payload = payload
            self.status_code = status
            self.text = ""

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            self.posts: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url: str, headers: dict, json: dict):
            self.posts.append(json)
            assert "input" in json
            assert json["input"]["images"][0]["image_b64"]
            assert "drive.google.com" not in str(json)
            return _Resp({"id": "job-1"})

        async def get(self, url: str, headers: dict):
            return _Resp(
                {
                    "status": "COMPLETED",
                    "output": {
                        "providers": ["CUDAExecutionProvider"],
                        "images": [
                            {
                                "faces": [
                                    {
                                        "bbox_x": 1,
                                        "bbox_y": 1,
                                        "bbox_width": 8,
                                        "bbox_height": 8,
                                        "confidence": 0.99,
                                        "embedding": vec,
                                    }
                                ]
                            }
                        ],
                    },
                }
            )

    with patch("app.faces.runpod_gpu.httpx.AsyncClient", _Client):
        faces = asyncio.run(detect_faces_runpod(image, drive_file_id="file1", settings=settings))
    assert len(faces) == 1
    assert faces[0].embedding[3] == 1.0
    _scale.assert_awaited()
