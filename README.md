# ai-short-video

[English](./README.md) | [简体中文](./README_CN.md)

Turn a topic into a finished short video — script, footage, voice-over,
subtitles and background music — with **one API key**.

Everything runs through the [dlazy](https://dlazy.com) API. There is no local
model to download, no GPU to provision, and no per-vendor key to juggle.

## What it does

Give it a subject (or your own script), and the pipeline runs:

1. **Script** — an LLM writes the narration, then extracts the visual search
   terms that drive the footage
2. **Footage** — clips are pulled from the stock library by search term, in
   the aspect ratio you picked. Switch the source to generate them with a
   video model instead when the library has nothing close enough
3. **Voice-over** — TTS renders the narration
4. **Subtitles** — the rendered voice-over is transcribed back, so the timing
   comes from the actual audio rather than an estimate
5. **Music** — an instrumental bed, generated or pulled from a royalty-free
   catalogue
6. **Assembly** — clips, narration, subtitles and music are cut to one video

Drive it from the Streamlit workbench, the CLI, or the HTTP API.

## Requirements

- Python 3.11+
- ffmpeg **and ffprobe** on your PATH (pydub probes durations with ffprobe;
  some portable ffmpeg builds ship only `ffmpeg.exe`, and `app.ffmpeg_path`
  only points at ffmpeg)
- A dlazy API key — get one at
  [dlazy.com/dashboard/organization/api-key](https://dlazy.com/dashboard/organization/api-key)

## Install

```bash
git clone https://github.com/dlazyai/ai-short-video.git
cd ai-short-video
uv sync                       # or: pip install -r requirements.txt
cp config.example.toml config.toml
```

Then start the workbench:

```bash
streamlit run ./webui/Main.py
```

Or with Docker:

```bash
docker compose up -d          # workbench on :8501, API on :8080
```

## Configure

Open the sidebar, paste your dlazy API key, and you are done. The model pickers
below it are filtered against your account, so you only see models your key can
actually run:

| Setting | Options |
| --- | --- |
| LLM | `claude-sonnet-5`, `qwen3.8-max`, `kimi-k3` |
| Speech-to-text | `fun-asr`, `elevenlabs-stt` |
| Voice-over | `qwen-tts`, `doubao-tts`, `elevenlabs-tts` |
| Footage source | `stock` (default), `generated`, `local` |
| Footage model (when generating) | `seedance-2.0-fast`, `seedance-2.0`, `seedance-2.5`, `veo-3.1-fast`, `kling-v3`, `wan2.7` |
| Music | `elevenlabs-music`, `suno-music`, `search_audio` |

Everything else lives in `config.toml`, which the settings page writes for you.

## Cost and speed

Footage is where the cost lives, so the source is a deliberate choice:

| `video_source` | Cost | Speed | When |
| --- | --- | --- | --- |
| `stock` (default) | 1 credit per search | Immediate | Almost always |
| `generated` | Metered per clip | Minutes per clip | The library has nothing close enough to the script |
| `local` | Free | Immediate | You already have the footage |

When generating, the pipeline computes exactly how many clips the narration
needs instead of over-fetching, and caches clips by search term so a retried
task does not pay twice. Pick a fast model — `seedance-2.0-fast` is the
default.

For background music, `search_audio` searches a royalty-free catalogue instead
of generating a track — far cheaper than `elevenlabs-music` or `suno-music`.

## CLI

```bash
python cli.py --video-subject "how compound interest works"
```

`python cli.py --help` lists the rest.

## Known limits

Honest about what this fork gives up by moving everything to one provider:

- **Generated footage is slow and metered.** It matches the script better
  than stock clips, but costs per clip and takes minutes each. Stock search
  is the default for that reason; local footage is free.
- **Transcription accepts English or Chinese only.** The *target* language for
  the script is free-form.
- **No voice cloning.** Dubbing uses the preset voices each model exposes.
- **No cross-posting.** Finished videos are downloaded, not published.

## Credits

Built on [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) by
Harry, MIT licensed. The pipeline design is theirs; this fork swaps the model
layer for dlazy. See [NOTICE.md](NOTICE.md) for the full change list.

## License

MIT — see [LICENSE](LICENSE).
