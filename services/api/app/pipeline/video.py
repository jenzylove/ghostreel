"""Phase 1 orchestrator: topic -> script -> style-locked images + voice -> assembled MP4.

Synchronous and sequential on purpose — this proves the whole pipe end to end. Phase 2 wraps
each image gen in an evaluate-retry loop; Phase 3 moves this behind the async job runner and
fans the per-segment generation out concurrently.
"""
from __future__ import annotations

import time

from app.models import VideoResponse
from app.pipeline.assemble import assemble_video
from app.pipeline.script import generate_script
from app.pipeline.style import DEFAULT_STYLE
from app.pipeline.visuals import generate_image
from app.pipeline.voice import generate_voice


def create_video(topic: str) -> VideoResponse:
    t0 = time.time()

    script = generate_script(topic)
    preset = DEFAULT_STYLE

    for seg in script.segments:
        styled_prompt = preset.apply(seg.visual)      # style-lock applied per segment
        seg.image_url = generate_image(styled_prompt)["asset_url"]
        seg.audio_url = generate_voice(seg.narration)

    video_url = assemble_video(script)

    return VideoResponse(
        video_url=video_url,
        segments=len(script.segments),
        took_s=round(time.time() - t0, 1),
    )
