FROM python:3.11-slim

# ffmpeg + ffprobe for assembly; fonts-dejavu-core so libass can render burned-in captions
# (the slim image ships no fonts, so subtitles would otherwise be blank).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Render provides $PORT; bind to it on all interfaces.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
