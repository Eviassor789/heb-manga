# ─────────────────────────────────────────────────────────────────────────────
# Hebrew Manga Translator — backend image
#
# Runs the FastAPI app as ONE long-lived process. Detection + inpainting are
# offloaded to each user's Modal GPU (BYOK), so this image is CPU-only and
# stays relatively small (no CUDA). The only local model is the EasyOCR OCR
# fallback, which is warmed at build time.
#
# Build:  docker build -t hmt-backend ./backend
# Run:    docker run -p 8000:8000 --env-file backend/.env hmt-backend
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# System libraries needed by OpenCV / Pillow at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the CPU-only build of torch FIRST so the much larger default CUDA
# wheel is never pulled (torch/torchvision in requirements.txt are then already
# satisfied and skipped).
RUN pip install --upgrade pip && \
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy the application code.
COPY . .

# Warm the EasyOCR fallback weights into the image (skips the unused detector +
# LaMa weights, which run on Modal). Non-fatal if it can't reach the network.
RUN MODELS_EASYOCR_ONLY=1 python core/download_models.py || true

# Most platforms inject $PORT; default to 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Single worker: the app keeps job state, SSE subscribers, caches and the
# per-IP/concurrency guards in process memory, so it must run as one process.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 75"]
