# NOTICE

**ai-short-video** is a derivative work of the open-source project
**MoneyPrinterTurbo**.

- Upstream: https://github.com/harry0703/MoneyPrinterTurbo
- Upstream license: MIT (full text in [LICENSE](LICENSE), copyright 2024 Harry)
- Upstream author: Harry (harry0703)

The pipeline — script generation, keyword extraction, footage assembly,
voice-over, subtitle alignment, background music and the Streamlit workbench —
was designed and built upstream. This fork replaces the model layer with the
[dlazy](https://dlazy.com) API and drops branding and assets that are not ours
to redistribute.

MIT requires the copyright notice to travel with the code; `LICENSE` is kept
verbatim. The change list below is not required by the licence, it is here so
anyone comparing the two trees can see exactly what moved.

## Removed

| Content | Reason |
| --- | --- |
| 21 LLM providers (moonshot, openai, gemini, deepseek, qwen, azure, volcengine, grok, minimax, mimo, cloudflare, modelscope, aihubmix, aimlapi, evolink, ollama, oneapi, litellm, groq, pollinations) and `app/models/llm_provider.py` | Replaced by a single dlazy text model |
| 8 TTS backends (Azure v1/v2, SiliconFlow, Gemini, MiMo, MiniMax, ElevenLabs, Chatterbox) and the bundled `edge-tts` path | Replaced by dlazy TTS |
| `faster-whisper` and the local Whisper model download | Local model — replaced by dlazy speech-to-text |
| Direct Pexels / Pixabay / Coverr API keys and clients | Stock search now goes through dlazy's `search_video`, so no per-vendor key is needed; Coverr and the direct Pexels client are gone |
| `app/services/twelvelabs.py` (Marengo semantic re-ranking) | Third-party video-understanding models |
| `app/services/sonilo.py`, `app/services/elevenlabs_music.py` | Third-party music services — replaced by `app/services/music.py` |
| `app/services/upload_post.py` and the ~618-line cross-posting pipeline | Third-party publishing to TikTok / Instagram / YouTube Shorts |
| `resource/songs/` (29 bundled tracks, 56 MB) | Third-party music of unclear redistribution status; music now comes from dlazy |
| `resource/fonts/MicrosoftYaHei*.ttc`, `STHeiti*.ttc` (147 MB) | **Proprietary fonts.** Microsoft YaHei ships with Windows and STHeiti with macOS; neither grants redistribution. Replaced by Noto Sans SC (SIL OFL, licence kept alongside the file) |
| `docs/skill/` (upstream's agent skill, `MoneyPrinterTurbo.ipynb`, screenshots) | Upstream's branding and their own author contact |
| `.github/` workflows and issue templates | Bound to upstream's repository and container registry |
| `Dockerfile.gpu`, `docker-compose.gpu.yml` | The CUDA image existed for the local Whisper model |

## Changed

| File | Change |
| --- | --- |
| `app/services/dlazy_client.py` | **New.** HTTP client for the dlazy tool API — run tool, poll async tasks, upload media, read the manifest. Resolves `cli_name` to the tool `id` the API keys on, and caches manifest failures so a missing key cannot stall the settings page |
| `app/services/llm.py` | The 266-line, 21-provider dispatch inside `_generate_response` became one dlazy call. dlazy text tools take a single `prompt` and have no `response_format`, so JSON still comes back through the existing text parsing. 976 → 742 lines |
| `app/services/voice.py` | 36 provider functions removed; `tts()` now renders through dlazy and rebuilds the subtitle timeline from the audio duration. edge-tts is gone, so a minimal local `SubMaker` replaces the one it exported. 2092 → 821 lines |
| `app/services/subtitle.py` | Rewritten onto dlazy ASR. dlazy returns a flat word list, so the same punctuation-driven sentence grouping runs over those words instead of Whisper segments |
| `app/services/material.py` | Stock search replaced by per-term clip generation. Generation is metered per clip, so the clip count is computed from the audio duration instead of over-fetching, and results are cached by (term, ratio, duration). 989 → 401 lines |
| `app/services/music.py` | **New.** Background music via `elevenlabs-music` / `suno-music` / `search_audio`, always instrumental — the track sits under a voice-over |
| `app/services/task.py` | Cross-posting removed; the music provider registry points at dlazy; subtitle generation collapsed to the single ASR path. 1309 → 867 lines |
| `webui/Main.py` | Settings became one dlazy API key plus model pickers for LLM / ASR / footage / music, read live from the manifest rather than hardcoded. The provider settings UI, voice-provider credential panels and the stock-API key panel are gone. 4335 → 3540 lines |
| `config.example.toml` | Every third-party credential replaced by one `[dlazy]` section. 370 → 130 lines |
| `webui/i18n/*.json` | 23 new strings; 51 belonging to removed providers and 29 `llm_provider_tips.*` entries deleted; all 9 locales realigned to the same key set |
| `requirements.txt`, `pyproject.toml` | 21 → 15 packages after dropping the torch/whisper/provider-SDK stack |
| `Dockerfile` | Unchanged in shape; the GPU variant was dropped |
| `test/` | 139 cases written for removed functionality deleted; `test_llm.py` rewritten against the dlazy contract |

## Unchanged

The video assembly and effects (`app/services/video.py`), subtitle rendering and
positioning, task state and artifacts, the HTTP API (`app/controllers/`), batch
and CLI entry points, and the Streamlit workbench structure are upstream work,
carried over apart from the naming changes above.

## Functional differences from upstream

Consequences of routing everything through one provider, not bugs:

- **Stock footage costs a credit per search.** Upstream queried Pexels /
  Pixabay / Coverr with your own API keys; here the same Pixabay library is
  reached through dlazy's `search_video`, which is metered but needs no
  per-vendor key. Coverr is gone. `video_source = "generated"` renders clips
  with a video model instead — better matched to the script, but billed per
  clip and minutes each. Local footage remains free.
- **Transcription accepts English or Chinese only.** dlazy's `fun-asr` and
  `elevenlabs-stt` take `zh` or `en`; upstream's local Whisper handled more.
- **No voice cloning and no per-provider voice catalogues.** Dubbing uses the
  preset voices the selected dlazy TTS model exposes.
- **No cross-posting.** Finished videos are downloaded rather than published.

## Trademarks

"dlazy" is a trademark of its owner. This repository does not use the upstream
project's name, logo or brand identity.
