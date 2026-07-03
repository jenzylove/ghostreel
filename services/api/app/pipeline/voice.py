"""Voiceover generation: narration text -> audio asset in B2 (ElevenLabs via Genblaze).

generate_voice       — single ElevenLabs request (up to tts_chunk_chars).
generate_voice_full  — handles narrations of any length by splitting at sentence
                       boundaries, TTS-ing each chunk, and concatenating with ffmpeg.
                       This is the function the runner calls.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from genblaze_core import Modality, Pipeline

from app.config import settings
from app.pipeline.providers import voice_provider
from app.storage.b2 import backend, get_by_url, sink


def generate_voice(narration: str, voice_id: str | None = None) -> str | None:
    """TTS one chunk of narration -> durable B2 audio URL."""
    out = (
        Pipeline("ghostreel-voice")
        .step(
            voice_provider(),
            model=settings.tts_model,
            modality=Modality.AUDIO,
            prompt=narration,
            voice_id=voice_id or settings.voice_id,
        )
        .run(sink=sink(), timeout=90, max_retries=2)
    )
    try:
        run_obj = out[0] if isinstance(out, tuple) else getattr(out, "run", out)
        return run_obj.steps[0].assets[0].url
    except Exception:
        return None


def _split_narration(text: str, limit: int | None = None) -> list[str]:
    """Split at sentence boundaries so each chunk stays under the ElevenLabs char limit."""
    limit = limit or settings.tts_chunk_chars
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > limit and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def generate_voice_full(narration: str, voice_id: str | None = None) -> str | None:
    """TTS a full narration of any length with automatic chunking.

    Single-chunk narrations go direct (no ffmpeg overhead). Multi-chunk narrations
    are TTS'd piece by piece, downloaded, concatenated with ffmpeg stream-copy, then
    re-uploaded to B2 as one file. Returns a durable B2 URL.
    """
    chunks = _split_narration(narration)

    if len(chunks) == 1:
        return generate_voice(narration, voice_id)

    chunk_urls = [generate_voice(c, voice_id) for c in chunks]
    chunk_urls = [u for u in chunk_urls if u]
    if not chunk_urls:
        return None

    work = Path(tempfile.mkdtemp(prefix="gr_tts_"))
    parts: list[Path] = []
    for i, url in enumerate(chunk_urls):
        p = work / f"chunk_{i}.mp3"
        p.write_bytes(get_by_url(url))
        parts.append(p)

    list_file = work / "chunks.txt"
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
    out = work / "full.mp3"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)],
        check=True, timeout=120, capture_output=True,
    )

    key = f"{settings.asset_prefix}/audio/{uuid.uuid4().hex}.mp3"
    backend().put(key, out.read_bytes(), content_type="audio/mpeg")
    # Construct a durable (non-presigned) URL matching B2's path-style format.
    region = settings.b2_region
    bucket = settings.b2_bucket_name
    if region:
        return f"https://s3.{region}.backblazeb2.com/{bucket}/{key}"
    return f"https://s3.backblazeb2.com/{bucket}/{key}"
