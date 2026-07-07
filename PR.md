# PR: Video moment search (self-hosted ffmpeg + VTT + Gemini VLM)

## Verdict

**Removed** Fennec Docker sidecar (CLIP/Whisper/PyTorch in containers was fragile and heavy).

**Replaced with** a lightweight self-hosted pipeline in DriveFaceIndexer:

| Step | Tool |
|------|------|
| Cache video from Drive | Drive Connector |
| Transcript | `.vtt` sidecar in Drive, or embedded subs via ffmpeg |
| Keyframes | ffmpeg (`VIDEO_FRAME_INTERVAL_SECONDS`, default 3s) |
| Faces per frame | InsightFace (same as images) |
| Visual captions | Gemini VLM (`gemini-2.5-flash`) on keyframes |
| Search | Local VTT text match + Gemini File Search on transcript + frame JPEGs |

No Docker ML stack. Requires **ffmpeg/ffprobe** on the host and **GEMINI_API_KEY**.

## Setup

```powershell
# backend/.env
VIDEO_INDEXING_ENABLED=true
VIDEO_CACHE_DIR=./data/videos
VIDEO_FRAME_INTERVAL_SECONDS=3.0
FENNEC_ENABLED=false
```

Put `myclip.mp4` and `myclip.vtt` in the connected Drive folder (same basename). Re-index → search with **Videos only** filter.

## Test plan

- [ ] `pytest tests/test_vtt.py tests/test_indexable_mime.py`
- [ ] MP4 + VTT in Drive → indexer marks PROCESSED, frames under `data/thumbnails/video/{id}/`
- [ ] `GET /search?q=party&mime=video` returns `moments[]` with `timestamp_sec`
- [ ] `GET /media/video/{drive_id}/frame?ts=3.00` returns JPEG
- [ ] Person filter returns moments when faces tagged on video frames
