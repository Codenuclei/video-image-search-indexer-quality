# Studio Whisper (RunPod Serverless)

Build and push, then create a Serverless endpoint pointing at the image.

```bash
cd deploy/runpod-whisper
docker build -t <registry>/dfi-carousel-whisper:latest .
docker push <registry>/dfi-carousel-whisper:latest
```

Set on `dfi-carousel-backend` (Studio only):

- `VIDEO_TRANSCRIPT_FIRST_ENABLED=true`
- `RUNPOD_API_KEY=...`
- `RUNPOD_WHISPER_ENABLED=true`
- `RUNPOD_WHISPER_ENDPOINT_ID=<endpoint id>`
- `RUNPOD_WHISPER_MODEL_SIZE=base`
- `RUNPOD_FACE_GPU_ENABLED=true`
- `RUNPOD_FACE_ENDPOINT_ID=<existing ArcFace endpoint id>`

Do not attach search Postgres / face-worker.
