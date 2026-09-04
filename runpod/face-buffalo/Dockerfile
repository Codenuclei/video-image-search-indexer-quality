FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir insightface opencv-python-headless numpy runpod onnx \
    && pip uninstall -y onnxruntime \
    && pip install --no-cache-dir "onnxruntime-gpu>=1.20.0"

# Bake buffalo_l ONNX into the image so workers skip the GitHub download.
ENV INSIGHTFACE_HOME=/root/.insightface
RUN python -c "from insightface.app import FaceAnalysis; \
app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider']); \
app.prepare(ctx_id=-1, det_size=(640, 640))"

COPY handler.py .

CMD ["python", "-u", "handler.py"]
