# Studio Whisper (RunPod Serverless)

Build and push, then create a Serverless endpoint pointing at the image.

```bash
cd deploy/runpod-whisper
docker build -t <registry>/dfi-carousel-whisper:latest .
docker push <registry>/dfi-carousel-whisper:latest
```

Set on `dfi-carousel-backend` / App Platform `carousel-backend` (Studio only):

- `VIDEO_TRANSCRIPT_FIRST_ENABLED=true`
- `RUNPOD_API_KEY=...`
- `RUNPOD_WHISPER_ENABLED=true`
- `RUNPOD_WHISPER_ENDPOINT_ID=<endpoint id>`
- `RUNPOD_WHISPER_MODEL_SIZE=base`
- `RUNPOD_WHISPER_TIMEOUT_SECONDS=900` (optional)
- `RUNPOD_FACE_GPU_ENABLED=true`
- `RUNPOD_FACE_ENDPOINT_ID=<existing ArcFace endpoint id>`
- `RUNPOD_FACE_TIMEOUT_SECONDS=180` (optional)

Placeholders also appear in [`.do/backend.yaml`](../../.do/backend.yaml). Do not attach search Postgres / face-worker. Media, vectors, and associations stay in Carousel Postgres/Qdrant (Railway today; DigitalOcean Droplet when migrated).
