FROM python:3.12-slim-bookworm AS builder

# Compile rawpy against system libraw (ARW/CR3/NEF). imagecodecs uses a prebuilt wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    pkg-config \
    libraw-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall --no-binary rawpy "rawpy>=0.22.0"


FROM python:3.12-slim-bookworm

# Runtime codecs for Pillow + imagecodecs TIFF/LZW/JPEG2000/LERC decompressors.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libheif1 \
    libtiff6 \
    libjpeg62-turbo \
    zlib1g \
    libopenjp2-7 \
    libwebp7 \
    liblcms2-2 \
    libdeflate0 \
    liblerc4 \
    libzstd1 \
    libraw20 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY . .

RUN mkdir -p data/tmp data/thumbnails data/videos data/frame_thumbs

ENV PYTHONUNBUFFERED=1
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port $PORT"]
