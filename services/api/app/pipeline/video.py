"""Phase 1/2 orchestrator: topic -> script -> style-locked + QA'd images + voice -> MP4.

Synchronous and sequential — proves the whole pipe with the evaluate-retry loop wired in.
Phase 3 moves this behind the async job runner and fans per-segment generation out.
"""
from __future__ import annotations

import time

from app.models import VideoResponse
from app.pipeline.assemble import assemble_video
from app.pipeline.script import generate_script
from app.pipeline.style import DEFAULT_STYLE
from app.pipeline.visuals import generate_and_qa
from app.pipeline.voice import generate_voice


def create_video(topic: str) -> VideoResponse:
    t0 = time.time()

    script = generate_script(topic)
    preset = DEFAULT_STYLE

    for seg in script.segments:
        styled_prompt = preset.apply(seg.visual)             # style-lock per segment
        seg.image_url, seg.attempts = generate_and_qa(styled_prompt, seg, preset)  # QA + retry
        seg.audio_url = generate_voice(seg.narration)

    retries = sum(max(0, len(s.attempts) - 1) for s in script.segments)  # self-healing count
    video_url = assemble_video(script)

    return VideoResponse(
        video_url=video_url,
        segments=len(script.segments),
        retries=retries,
        took_s=round(time.time() - t0, 1),
    )
