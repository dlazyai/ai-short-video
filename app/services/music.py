"""Background music through dlazy.

Replaces the former Sonilo and ElevenLabs-Music integrations. The interface is
the one `task.py` already expects from a music provider:

    generate_bgm(video_path, output_path, video_duration, prompt) -> None
    MusicError                                                     raised on failure

Two backends are offered, both metered per call:

  * ``elevenlabs-music`` takes an explicit duration, so it matches the timeline
    without post-trimming — this is the default.
  * ``suno-music`` is asked for instrumental output; a vocal track would fight
    with the voice-over it sits under.
"""

import os

from loguru import logger

from app.config import config
from app.services import dlazy_client

DEFAULT_MUSIC_MODEL = "elevenlabs-music"
MUSIC_MODELS = ("elevenlabs-music", "suno-music", "search_audio")
MAX_MUSIC_SECONDS = 300


class MusicError(Exception):
    """Raised when background music could not be produced."""


def _build_payload(model: str, prompt: str, duration: int) -> dict:
    if model == "elevenlabs-music":
        return {"prompt": prompt, "duration": duration, "promptRefs": []}
    if model == "suno-music":
        return {
            "customMode": False,
            # Instrumental on purpose: this track plays under a voice-over.
            "instrumental": True,
            "prompt": prompt,
            "style": "",
            "title": "",
            "negativeTags": "vocals, lyrics, singing",
            "vocalGender": "f",
            "styleWeight": 0.65,
            "weirdnessConstraint": 0.65,
            "audioWeight": 0.65,
            "promptRefs": [],
        }
    if model == "search_audio":
        # Royalty-free catalogue search rather than generation — far cheaper.
        return {"query": prompt, "perPage": 5, "minDuration": max(10, duration // 2)}
    raise MusicError(f"unsupported dlazy music model: {model}")


def _pick_url(model: str, output) -> str:
    if model == "search_audio":
        hits = (output or {}).get("hits") or []
        if not hits:
            raise MusicError(f"no track found for the requested mood")
        return hits[0].get("url") or ""
    urls = (output or {}).get("urls") or []
    if not urls:
        raise MusicError(f"[{model}] returned no audio url")
    return urls[0]


def generate_bgm(
    video_path: str,
    output_path: str,
    video_duration: float,
    prompt: str,
) -> None:
    """Produce a background track for one video and write it to `output_path`.

    `video_path` is part of the provider interface but unused here — dlazy
    scores from the text prompt rather than from the footage.
    """
    model = str(config.dlazy.get("music_model") or DEFAULT_MUSIC_MODEL)
    if model not in MUSIC_MODELS:
        raise MusicError(f"unsupported dlazy music model: {model}")

    query = (prompt or "").strip() or "calm cinematic background music"
    duration = max(10, min(MAX_MUSIC_SECONDS, int(video_duration or 30)))

    logger.info(f"generating {duration}s bgm via {model}: {query[:80]}")
    try:
        output = dlazy_client.run_tool(model, _build_payload(model, query, duration))
        url = _pick_url(model, output)
        dlazy_client.download(url, output_path)
    except MusicError:
        raise
    except Exception as e:
        raise MusicError(str(e)) from e

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise MusicError(f"{model} produced an empty audio file")
