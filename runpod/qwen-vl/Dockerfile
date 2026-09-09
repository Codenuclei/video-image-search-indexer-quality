# Bake handler + SGLang so RunPod workers pull GHCR, not Docker Hub.
# Build/push once:
#   docker build -t ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-qwen3-vl-sglang:gpu runpod/qwen-vl
#   docker push ghcr.io/codenuclei/video-image-search-indexer-quality/dfi-qwen3-vl-sglang:gpu
#
# The FROM below is only used on the build machine. Workers never hit docker.io.

FROM lmsysorg/sglang:dev-cu12

ENV PYTHONUNBUFFERED=1 \
    SGLANG_URL=http://127.0.0.1:8000 \
    QWEN_MODEL=Qwen/Qwen3-VL-8B-Instruct \
    QWEN_CONCURRENCY=32 \
    QWEN_MAX_TOKENS=1536 \
    RUNPOD_INIT_TIMEOUT=1800 \
    HF_XET_HIGH_PERFORMANCE=1 \
    SGLANG_VLM_CACHE_SIZE_MB=0 \
    QWEN_SERVERLESS=1

WORKDIR /app
RUN python3 -m pip install -q --no-cache-dir runpod 'httpx>=0.27'
COPY handler.py tags.py start.sh /app/
RUN chmod +x /app/start.sh

ENTRYPOINT ["/app/start.sh"]
