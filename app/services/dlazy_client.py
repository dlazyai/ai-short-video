"""HTTP client for the dlazy tool API.

Every model call in this project goes through here — script writing, voice-over,
subtitles, stock footage and background music alike. The endpoints mirror what
the official `dlazy` CLI uses:

    POST /api/cli/tool                  run a tool, returns {output}
    GET  /api/cli/tool?generateId=...   poll an async task
    POST /api/cli/upload-url            signed URL for uploading local media
    GET  /api/cli/tool/manifest         tool list with input/output schemas

Async tools answer the POST with an `output` carrying a `generateId` instead of
the result; we then poll until the task is completed or failed.
"""

import mimetypes
import os
import time

import requests
from loguru import logger

from app.config import config

DEFAULT_BASE_URL = "https://dlazy.com"
POLL_INTERVAL = 3
DEFAULT_TIMEOUT = 1800

# The tool API gates on X-CLI-Version and answers 426 without it. We speak the
# same contract as the official CLI, so we advertise the version we were built
# against; bump it if the server ever raises MIN_SUPPORTED_CLI_VERSION past this.
CLI_VERSION = "1.2.3"


class DlazyError(Exception):
    pass


def base_url() -> str:
    url = str(config.dlazy.get("base_url", "") or DEFAULT_BASE_URL).strip().rstrip("/")
    return url or DEFAULT_BASE_URL


def api_key() -> str:
    key = str(config.dlazy.get("api_key", "") or "").strip()
    if not key:
        raise DlazyError(
            "dlazy API key is not set. Open the sidebar settings and paste the key "
            "from https://dlazy.com/dashboard/organization/api-key"
        )
    return key


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "X-CLI-Version": CLI_VERSION,
    }


def upload_file(path: str) -> str:
    """Upload a local file to dlazy object storage, return its public URL."""
    if not os.path.exists(path):
        raise DlazyError(f"file not found: {path}")
    filename = os.path.basename(path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    resp = requests.post(
        f"{base_url()}/api/cli/upload-url",
        headers=_headers(),
        json={"filename": filename, "contentType": content_type},
        timeout=60,
    )
    if not resp.ok:
        raise DlazyError(f"upload-url failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()

    with open(path, "rb") as f:
        put_headers = dict(data.get("requiredHeaders") or {})
        put_headers.setdefault("Content-Type", content_type)
        put = requests.put(data["signedUrl"], data=f, headers=put_headers, timeout=600)
    if not put.ok:
        raise DlazyError(f"upload failed ({put.status_code}): {put.text[:300]}")
    return data["publicUrl"]


# A long generation is polled for minutes. A proxy or flaky link can abort a
# single poll, and giving up there would discard work that is already paid for
# and probably finished server-side — so transient errors are retried.
POLL_MAX_CONSECUTIVE_ERRORS = 5


def _poll(generate_id: str, timeout: int):
    deadline = time.time() + timeout
    url = f"{base_url()}/api/cli/tool?generateId={generate_id}"
    errors = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            resp = requests.get(url, headers=_headers(), timeout=60)
            if not resp.ok:
                raise DlazyError(
                    f"poll failed ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
        except DlazyError:
            raise
        except Exception as e:
            errors += 1
            if errors >= POLL_MAX_CONSECUTIVE_ERRORS:
                raise DlazyError(
                    f"task {generate_id}: polling failed {errors} times in a row: {e}"
                ) from e
            logger.warning(
                f"transient error polling {generate_id} "
                f"({errors}/{POLL_MAX_CONSECUTIVE_ERRORS}): {e}"
            )
            time.sleep(POLL_INTERVAL * errors)
            continue

        errors = 0
        status = data.get("status")
        if status == "completed":
            return data.get("result")
        if status == "failed":
            raise DlazyError(f"task {generate_id} failed: {data.get('error')}")
    raise DlazyError(f"task {generate_id} did not finish within {timeout}s")


def _resolve_model_id(model: str) -> str:
    """Map a user-facing tool name onto the id the tool API keys on.

    Everything user-facing (docs, config, the CLI) uses `cli_name`, but
    /api/cli/tool looks the model up by `id`, and the two differ for a good
    third of the catalogue — e.g. `qwen3.8-max` -> `qwen-3-8-max` and
    `search_audio` -> `search-audio`. Sending the cli_name for one of those
    answers 400 invalid_tool, so resolve through the manifest first.
    """
    try:
        for tool in get_manifest().get("tools", []):
            if tool.get("cli_name") == model:
                return tool.get("id") or model
    except Exception:
        pass
    return model


def run_tool(model: str, payload: dict, timeout: int = DEFAULT_TIMEOUT):
    """Run one dlazy tool and return its output, waiting out async tasks."""
    resp = requests.post(
        f"{base_url()}/api/cli/tool",
        headers=_headers(),
        json={"model": _resolve_model_id(model), "input": payload},
        timeout=timeout,
    )
    if not resp.ok:
        raise DlazyError(f"{model} failed ({resp.status_code}): {resp.text[:500]}")

    output = resp.json().get("output")
    if isinstance(output, dict) and isinstance(output.get("generateId"), str):
        logger.info(f"{model} running as async task {output['generateId']}")
        return _poll(output["generateId"], timeout)
    return output


def download(url: str, save_as: str) -> str:
    """Fetch a result media URL to a local path."""
    os.makedirs(os.path.dirname(os.path.abspath(save_as)), exist_ok=True)
    resp = requests.get(url, stream=True, timeout=600)
    if not resp.ok:
        raise DlazyError(f"download failed ({resp.status_code}): {url}")
    with open(save_as, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            f.write(chunk)
    return save_as


# ---------------------------------------------------------------------------
# manifest — the server is the source of truth for tool names and voice lists,
# so the settings page reads them live instead of hardcoding a list that goes
# stale as soon as dlazy adds a model or a voice.
# ---------------------------------------------------------------------------

_MANIFEST_CACHE = {}


# The manifest is a small metadata call, but it is hit on every settings render.
# A short timeout plus caching the *failure* keeps a missing key or a slow network
# from stalling the page for a minute on each rerun.
MANIFEST_TIMEOUT = 10
MANIFEST_FAILURE_TTL = 60


def get_manifest(force: bool = False) -> dict:
    if not force:
        if "data" in _MANIFEST_CACHE:
            return _MANIFEST_CACHE["data"]
        failed_at = _MANIFEST_CACHE.get("failed_at")
        if failed_at is not None and time.time() - failed_at < MANIFEST_FAILURE_TTL:
            raise DlazyError(_MANIFEST_CACHE.get("error", "manifest unavailable"))
    try:
        resp = requests.get(
            f"{base_url()}/api/cli/tool/manifest",
            headers=_headers(),
            timeout=MANIFEST_TIMEOUT,
        )
        if not resp.ok:
            raise DlazyError(
                f"manifest failed ({resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
    except Exception as e:
        _MANIFEST_CACHE["failed_at"] = time.time()
        _MANIFEST_CACHE["error"] = str(e)
        raise
    _MANIFEST_CACHE.pop("failed_at", None)
    _MANIFEST_CACHE["data"] = data
    return data


def _tool(model: str):
    for t in get_manifest().get("tools", []):
        if t.get("cli_name") == model:
            return t
    return None


def list_voices(model: str):
    """Return (voice_ids, default_voice) for a TTS model, straight from the manifest."""
    tool = _tool(model)
    if not tool:
        return [], ""
    props = (tool.get("inputJsonSchema") or {}).get("properties") or {}
    field = props.get("voice") or props.get("voiceId") or {}
    return list(field.get("enum") or []), field.get("default") or ""


def available_models(cli_names) -> list:
    """Filter a candidate model list down to what this account can actually run."""
    try:
        live = {t.get("cli_name") for t in get_manifest().get("tools", [])}
    except Exception:
        return list(cli_names)
    return [n for n in cli_names if n in live] or list(cli_names)


def check_credentials() -> bool:
    """Cheap round-trip used by the settings page to validate the key."""
    try:
        resp = requests.get(
            f"{base_url()}/api/cli/tool/manifest", headers=_headers(), timeout=30
        )
        return resp.ok
    except Exception:
        return False
