FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

WORKDIR /app
RUN pip install --no-cache-dir faster-whisper runpod
COPY handler.py /app/handler.py

ENV WHISPER_MODEL_SIZE=base
ENV WHISPER_DEVICE=cuda
ENV WHISPER_COMPUTE_TYPE=float16

CMD ["python", "-u", "/app/handler.py"]
