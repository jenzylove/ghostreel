"""Final assembly: per-segment images + narration -> one MP4 in B2.

Direct ffmpeg subprocess (the sample confirms genblaze has no composition primitive).
Timing rule: each segment's on-screen duration is the measured length of ITS narration
audio, so visuals stay synced to the voice (audio drives timing).

Asset fetch: the Asset.url is durable but credential-free, and our bucket is private, so an
anonymous GET 401s. Per the Asset model's own guidance ("parse the key from this URL if the
sink backend is known"), we derive the object key and pull bytes through the authenticated
backend().get(key) - no second client, no presigning.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import settings
from app.models import Script
from app.storage.b2 import backend

WIDTH, HEIGHT, FPS = 1280, 720, 30
_FF = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]


def _key_from_url(url: str) -> str:
    """Recover the B2 object key from a durable Asset URL across the common URL shapes."""
    path = unquote(urlparse(url).path).lstrip("/")
    bucket = settings.b2_bucket_name
    if path.startswith(f"file/{bucket}/"):        # B2 "friendly" URL: /file/{bucket}/{key}
        return path[len(f"file/{bucket}/"):]
    if path.startswith(f"{bucket}/"):             # path-style: /{bucket}/{key}
        return path[len(bucket) + 1:]
    return path                                   # virtual-hosted: /{key}


def _fetch(url: str, dest: Path) -> None:
    dest.write_bytes(backend().get(_key_from_url(url)))


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
    #    image per segment means any frame-rounding stays inside the segment and cannot
    #    accumulate across joins (the a/v-drift bug from separate video+audio concat).
    for seg in script.segments:
        if not seg.image_url or not seg.audio_url:
            continue
        img = work / f"img_{seg.index}.png"
        aud = work / f"aud_{seg.index}.mp3"
        _fetch(seg.image_url, img)
        _fetch(seg.audio_url, aud)
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

    # 3. Upload to B2 and return a viewable URL.
    key = f"{settings.asset_prefix}/videos/{uuid.uuid4().hex}.mp4"
    backend().put(key, final.read_bytes(), content_type="video/mp4")
    # Presigned so the finished video is directly viewable/downloadable despite the private
    # bucket. 7-day expiry (S3 sig-v4 max). Phase 3 will store the key and presign on demand
    # rather than baking an expiry into the persisted record.
    try:
        return backend().presigned_get_url(key, expires_in=604800)
    except Exception:
        return key
