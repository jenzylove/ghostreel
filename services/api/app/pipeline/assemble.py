"""Final assembly: beat-timed images + single audio track -> one MP4 in B2.

Architecture: one audio file (TTS or BYO recording), images shown in slideshow order
timed to the beat windows derived from word timings. No per-segment A/V coupling —
the audio is the source of truth; images are a display layer on top of it.

Captions: word-level karaoke SRT built from AssemblyAI timings, burned with libass.
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


def _build_word_srt(words: list[dict], path: Path, group: int = 4) -> None:
    """Karaoke-style captions: small word groups timed to the actual spoken audio."""
    out: list[str] = []
    i = n = 0
    while i < len(words):
        chunk = words[i : i + group]
        i += group
        n += 1
        out += [
            str(n),
            f"{_srt_ts(chunk[0]['start'])} --> {_srt_ts(chunk[-1]['end'])}",
            " ".join(w["word"] for w in chunk),
            "",
        ]
    path.write_text("\n".join(out), encoding="utf-8")


def assemble_slideshow(script: Script, audio_bytes: bytes, captions: bool = True) -> str | None:
    """Beat-timed slideshow assembly over a single audio track.

    Each beat's image is shown for (beat.end_s - beat.start_s) seconds. The last beat
    extends to fill remaining audio. Word-level karaoke captions are burned when enabled.
    """
    work = Path(tempfile.mkdtemp(prefix="ghostreel_sl_"))

    # Write the single audio track.
    audio_path = work / "audio.mp3"
    audio_path.write_bytes(audio_bytes)
    audio_dur = _audio_duration(audio_path)

    beats = [b for b in script.beats if b.image_url]
    if not beats:
        return None

    # Build one silent video clip per beat, sized to the beat's display duration.
    clips: list[Path] = []
    for i, beat in enumerate(beats):
        img = work / f"img_{i}.png"
        img.write_bytes(get_by_url(beat.image_url))

        # Use the gap to the next beat as duration; stretch the last beat to fill audio.
        if i < len(beats) - 1:
            dur = beats[i + 1].start_s - beat.start_s
        else:
            dur = max(0.1, audio_dur - beat.start_s)

        clip = work / f"clip_{i}.mp4"
        subprocess.run(
            [*_FF, "-loop", "1", "-t", f"{dur:.3f}", "-i", str(img),
             "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip)],
            check=True, timeout=300, capture_output=True,
        )
        clips.append(clip)

    # Stream-copy concat (identical encode params = safe).
    clip_list = work / "clips.txt"
    clip_list.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
    concat = work / "concat.mp4"
    subprocess.run(
        [*_FF, "-f", "concat", "-safe", "0", "-i", str(clip_list), "-c", "copy", str(concat)],
        check=True, timeout=600, capture_output=True,
    )

    # Mux with single audio track + optional word-level captions.
    # Run with cwd=work + relative filenames so libass never has to escape a temp path.
    final = work / "final.mp4"
    if captions and script.word_timings:
        _build_word_srt(script.word_timings, work / "cap.srt")
        subprocess.run(
            [*_FF, "-i", "concat.mp4", "-i", "audio.mp3",
             "-vf", f"subtitles=cap.srt:force_style='{_CAPTION_STYLE}'",
             "-c:a", "aac", "-shortest", "final.mp4"],
            check=True, timeout=900, capture_output=True, cwd=str(work),
        )
    else:
        subprocess.run(
            [*_FF, "-i", str(concat), "-i", str(audio_path),
             "-c:v", "copy", "-c:a", "aac", "-shortest", str(final)],
            check=True, timeout=600, capture_output=True,
        )

    key = f"{settings.asset_prefix}/videos/{uuid.uuid4().hex}.mp4"
    backend().put(key, final.read_bytes(), content_type="video/mp4")
    try:
        return backend().presigned_get_url(key, expires_in=604800)
    except Exception:
        return key
