FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Install FFmpeg and system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download Demucs model (bakes 80MB model into image, avoids cold-start download)
RUN python -c "from demucs import pretrained; pretrained.get_model('htdemucs')"

# Copy application code
COPY . .

# Ensure writable dirs exist
RUN mkdir -p /tmp/input /tmp/audio /tmp/output

# Use /tmp for storage (Cloud Run writable)
ENV STORAGE_DIR=/tmp

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
