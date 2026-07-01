"""Final assembly: per-segment images + narration -> one MP4 in B2.

Direct ffmpeg subprocess (the sample confirms genblaze has no composition primitive).
Timing rule: each segment's on-screen duration is the measured length of ITS narration
audio, so visuals stay synced to the voice (audio drives timing).

FIRST-RUN CHECK: confirm backend().put(key, bytes, content_type=...) and
backend().get_durable_url(key) exist (pattern from the sample's composer). The ffmpeg half
is independent of that.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import httpx

from app.config import settings
from app.models import Script
from app.storage.b2 import backend

WIDTH, HEIGHT, FPS = 1280, 720, 30
_FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


def _download(url: str, dest: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)


def _audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def assemble_video(script: Script) -> str | None:
    work = Path(tempfile.mkdtemp(prefix="ghostreel_"))
    clips: list[Path] = []
    audios: list[Path] = []

    # 1. Download each segment's image + audio; measure audio -> per-segment duration;
    #    render the still into a silent clip of exactly that length.
    for seg in script.segments:
        if not seg.image_url or not seg.audio_url:
            continue
        img = work / f"img_{seg.index}.png"
        aud = work / f"aud_{seg.index}.mp3"
        _download(seg.image_url, img)
        _download(seg.audio_url, aud)
        dur = _audio_duration(aud)
        seg.duration_s = dur

        clip = work / f"clip_{seg.index}.mp4"
        subprocess.run(
            [*_FF, "-loop", "1", "-t", f"{dur:.3f}", "-i", str(img),
             "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
            check=True, timeout=300, capture_output=True,
        )
        clips.append(clip)
        audios.append(aud)

    if not clips:
        return None

    # 2. Concat the silent clips (durations already match their audio).
    clip_list = work / "clips.txt"
    clip_list.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    silent = work / "silent.mp4"
    subprocess.run(
        [*_FF, "-f", "concat", "-safe", "0", "-i", str(clip_list), "-c", "copy", str(silent)],
        check=True, timeout=300, capture_output=True,
    )

    # 3. Concat the narration (re-encode to aac so mixed source formats concat cleanly).
    audio_list = work / "audio.txt"
    audio_list.write_text("".join(f"file '{a.as_posix()}'\n" for a in audios))
    voice = work / "voice.m4a"
    subprocess.run(
        [*_FF, "-f", "concat", "-safe", "0", "-i", str(audio_list), "-c:a", "aac", str(voice)],
        check=True, timeout=300, capture_output=True,
    )

    # 4. Mux video + narration.
    final = work / "final.mp4"
    subprocess.run(
        [*_FF, "-i", str(silent), "-i", str(voice),
         "-c:v", "copy", "-c:a", "copy", "-shortest", str(final)],
        check=True, timeout=300, capture_output=True,
    )

    # 5. Upload to B2 and return a durable URL.
    key = f"{settings.asset_prefix}/videos/{uuid.uuid4().hex}.mp4"
    data = final.read_bytes()
    backend().put(key, data, content_type="video/mp4")
    try:
        return backend().get_durable_url(key)
    except Exception:
        return key
