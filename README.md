# Ghostreel

Faceless-video studio: topic in, upload-ready video out — script → timestamped segments →
style-locked visuals → voiceover → assembly, orchestrated with **Genblaze** and stored on
**Backblaze B2** with verifiable per-asset provenance.

This is a generation-first app. The differentiators (vs. the many lookalike pipelines) are
the depth: an agentic **evaluate-generate-retry** loop, end-to-end **provenance**, and
**resumable B2-backed jobs**.

## Status: Phase 0 (the seam)

Proves the core path through the API: `POST /generate` → Genblaze (Imagen) → B2.
Synchronous and single-image on purpose — later phases add the async job runner, the full
script→segments→visuals→voice→assemble pipeline, evaluate-retry, provenance, and the UI.

## Run it (WSL / Linux — NOT native Windows)

> The Genblaze local-file→B2 path malforms `file://` URLs on Windows. Develop on Linux,
> which is also the deployment target. (Full diagnosis lives in the spike repo.)

```
cd /mnt/c/Users/LENOVO/projects/ghostreel/services/api
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in GEMINI_API_KEY, B2_KEY_ID, B2_APP_KEY, B2_BUCKET_NAME
uvicorn app.main:app --reload
```

Confirm the seam:

```
curl localhost:8000/health
curl -X POST localhost:8000/generate -H "content-type: application/json" \
  -d '{"prompt":"a hand-drawn marker doodle of a person walking through a forest, thick uneven black felt-tip outlines, no photorealism"}'
```

`/health` reports any missing env. `/generate` returns the B2 asset URL + timing.

## Layout

```
services/api/app/
  main.py            FastAPI routes (/health, /generate)  [Phase 0]
  config.py          env loading + validation
  models.py          request/response schemas
  storage/b2.py      B2 backend (singleton) + sink (HIERARCHICAL keys)
  pipeline/
    providers.py     provider construction (fallback_models -> Phase 2)
    visuals.py       image generation (the ported spike)
    # Phase 1+: script.py, style.py, voice.py, assemble.py
  # Phase 1+: jobs/ (store.py, runner.py), tests/
# Phase 1+: apps/web/ (React UI)
```
