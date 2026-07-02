"""Final assembly: per-segment images + narration -> one MP4 in B2.

Direct ffmpeg subprocess (the sample confirms genblaze has no composition primitive).
Timing rule: each segment's on-screen duration is the measured length of ITS narration
audio, so visuals stay synced to the voice. Each segment is a SELF-CONTAINED clip (image +
its own audio, -shortest), so frame-rounding can't accumulate into cross-segment drift.

Captions: built from the narration text + measured per-segment durations (segment-level, no
Whisper needed) and burned in with libass. Word-level karaoke sync would need Whisper.
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
_CAPTION_STYLE = (
    "FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,"
    "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=30"
)


def _audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(text: str, width: int = 42) -> str:
    lines: list[str] = []
    cur = ""
    for w in text.split():
        if cur and len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def _build_srt(cues: list[tuple[str, float]], path: Path) -> None:
    out: list[str] = []
    t = 0.0
    for i, (text, dur) in enumerate(cues, 1):
        start, end = t, t + dur
        t = end
        out += [str(i), f"{_srt_ts(start)} --> {_srt_ts(end)}", _wrap(text), ""]
    path.write_text("\n".join(out), encoding="utf-8")


def assemble_video(script: Script, captions: bool = True) -> str | None:
    work = Path(tempfile.mkdtemp(prefix="ghostreel_"))
    clips: list[Path] = []
    cues: list[tuple[str, float]] = []   # (narration, duration) for captions, in render order

    # 1. Build each segment as a SELF-CONTAINED clip: its image + its own narration, with
    #    -shortest so the clip length is exactly the audio length (no cross-segment drift).
    for seg in script.segments:
        if not seg.image_url or not seg.audio_url:
            continue
        img = work / f"img_{seg.index}.png"
        aud = work / f"aud_{seg.index}.mp3"
        img.write_bytes(get_by_url(seg.image_url))
        aud.write_bytes(get_by_url(seg.audio_url))
        seg.duration_s = _audio_duration(aud)

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
        cues.append((seg.narration, seg.duration_s))

    if not clips:
        return None

    # 2. Concat the self-contained clips (identical encode params -> stream copy is safe).
    clip_list = work / "clips.txt"
    clip_list.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    concat = work / "concat.mp4"
    subprocess.run(
        [*_FF, "-f", "concat", "-safe", "0", "-i", str(clip_list), "-c", "copy", str(concat)],
        check=True, timeout=300, capture_output=True,
    )

    # 3. Optionally burn captions (re-encode pass). Run with cwd=work + relative filenames so
    #    the subtitles filter never has to escape a temp path with special characters.
    if captions:
        _build_srt(cues, work / "captions.srt")
        final = work / "final.mp4"
        subprocess.run(
            [*_FF, "-i", "concat.mp4",
             "-vf", f"subtitles=captions.srt:force_style='{_CAPTION_STYLE}'",
             "-c:a", "copy", "final.mp4"],
            check=True, timeout=600, capture_output=True, cwd=str(work),
        )
    else:
        final = concat

    # 4. Upload to B2 and return a viewable (presigned) URL.
    key = f"{settings.asset_prefix}/videos/{uuid.uuid4().hex}.mp4"
    backend().put(key, final.read_bytes(), content_type="video/mp4")
    try:
        return backend().presigned_get_url(key, expires_in=604800)
    except Exception:
        return key
