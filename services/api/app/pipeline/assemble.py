"""Final assembly: per-segment images + narration -> one MP4 in B2.

Direct ffmpeg subprocess (the sample confirms genblaze has no composition primitive).
Timing rule: each segment's on-screen duration is the measured length of ITS narration
audio, so visuals stay synced to the voice. Each segment is a SELF-CONTAINED clip (image +
its own audio, -shortest), so frame-rounding can't accumulate into cross-segment drift.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

from app.config import settings
from app.models import Script
from app.storage.b2 import backend, get_by_url

WIDTH, HEIGHT, FPS = 1280, 720, 30
_FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


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

    # 1. Build each segment as a SELF-CONTAINED clip: its image + its own narration, with
    #    -shortest so the clip length is exactly the audio length. Binding audio to its own
    #    image per segment keeps frame-rounding inside the segment (no cross-segment drift).
    for seg in script.segments:
        if not seg.image_url or not seg.audio_url:
            continue
        img = work / f"img_{seg.index}.png"
        aud = work / f"aud_{seg.index}.mp3"
        img.write_bytes(get_by_url(seg.image_url))
        aud.write_bytes(get_by_url(seg.audio_url))
        seg.duration_s = _audio_duration(aud)  # recorded for metadata/provenance

        clip = work / f"clip_{seg.index}.mp4"
        subprocess.run(
            [*_FF, "-loop", "1", "-i", str(img), "-i", str(aud),
             "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(clip)],
            check=True, timeout=300, capture_output=True,
        )
        clips.append(clip)

    if not clips:
        return None

    # 2. Concat the self-contained clips (identical encode params -> stream copy is safe).
    clip_list = work / "clips.txt"
    clip_list.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    final = work / "final.mp4"
    subprocess.run(
        [*_FF, "-f", "concat", "-safe", "0", "-i", str(clip_list), "-c", "copy", str(final)],
        check=True, timeout=300, capture_output=True,
    )

    # 3. Upload to B2 and return a viewable (presigned) URL.
    key = f"{settings.asset_prefix}/videos/{uuid.uuid4().hex}.mp4"
    backend().put(key, final.read_bytes(), content_type="video/mp4")
    try:
        return backend().presigned_get_url(key, expires_in=604800)
    except Exception:
        return key
