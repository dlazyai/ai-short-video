import math
import os
import random
import threading
from pathlib import Path
from typing import Any, Callable, List
from urllib.parse import quote_plus, urlencode, urlsplit, urlunsplit

import requests
from loguru import logger
from moviepy.video.io.VideoFileClip import VideoFileClip

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode
from app.services import dlazy_client, material_cache, task_artifacts
from app.utils import utils

# Thread-safe counter for API key rotation
_api_key_counter = 0
_api_key_lock = threading.Lock()


def _get_tls_verify() -> bool:
    # 默认开启 TLS 证书校验，防止素材下载过程被中间人篡改。
    # 仅在企业代理、自签证书等明确需要的场景下，允许用户通过
    # `config.toml` 显式设置 `tls_verify = false` 临时关闭。
    tls_verify = config.app.get("tls_verify", True)
    if isinstance(tls_verify, str):
        tls_verify = tls_verify.strip().lower() not in ("0", "false", "no", "off")

    if not tls_verify:
        logger.warning(
            "TLS certificate verification is disabled by config.app.tls_verify=false. "
            "Only use this in trusted proxy environments."
        )

    return bool(tls_verify)


def _safe_public_url(value: Any) -> str | None:
    """
    只保留可公开展示的 HTTP(S) 页面地址，并移除查询参数和凭据。

    素材下载地址可能携带 API Key、签名 JWT 或临时 token。任务清单只需要
    帮助用户回到供应商的公开素材页，不应保存鉴权参数；用户信息形式的 URL
    同样拒绝，避免 ``https://user:pass@example.com`` 一类内容落盘。
    """
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _creator_info(value: Any) -> dict[str, str] | None:
    """从不同供应商的作者结构中提取统一的公开字段。"""
    if isinstance(value, str) and value.strip():
        return {"name": value.strip()}
    if not isinstance(value, dict):
        return None

    creator: dict[str, str] = {}
    creator_id = value.get("id")
    creator_name = value.get("name") or value.get("username")
    creator_page = _safe_public_url(
        value.get("url") or value.get("profile_url") or value.get("profile_page")
    )
    if creator_id is not None:
        creator["id"] = str(creator_id)
    if creator_name:
        creator["name"] = str(creator_name)
    if creator_page:
        creator["profile_page"] = creator_page
    return creator or None


def _material_source_record(item: MaterialInfo, local_path: str) -> dict[str, Any]:
    """
    为成功下载的素材生成轻量来源记录。

    ``source_info`` 可能来自缓存，甚至来自外部构造的 ``MaterialInfo``，因此
    不能原样写入。这里按白名单重新构造，只保留公开页面、业务标识和尺寸，
    并只记录本地文件名，避免用户目录或 Docker 挂载路径进入任务文件。
    """
    source = item.source_info if isinstance(item.source_info, dict) else {}
    record: dict[str, Any] = {
        "provider": str(item.provider or source.get("provider") or ""),
        "local_file": Path(local_path).name,
        "duration": int(item.duration),
    }

    search_term = source.get("search_term")
    asset_id = source.get("asset_id")
    source_page = _safe_public_url(source.get("source_page"))
    if isinstance(search_term, str) and search_term.strip():
        record["search_term"] = search_term.strip()
    if asset_id not in (None, ""):
        record["asset_id"] = str(asset_id)
    if source_page:
        record["source_page"] = source_page

    creator = _creator_info(source.get("creator"))
    if creator:
        record["creator"] = creator

    raw_rendition = source.get("rendition")
    if isinstance(raw_rendition, dict):
        rendition = {}
        for field in ("id", "width", "height"):
            value = raw_rendition.get(field)
            if value not in (None, ""):
                rendition[field] = str(value) if field == "id" else value
        if rendition:
            record["rendition"] = rendition
    return record


def _persist_material_sources(
    task_id: str,
    material_sources: list[dict[str, Any]],
) -> None:
    """
    将当前实际下载成功的素材来源补充到任务清单。

    任务记录是辅助能力，不能改变视频下载函数的返回值，也不能因为写盘失败
    中断成片主流程。``patch_script_data`` 会负责原子替换和异常日志；这里仅在
    成功后记录数量，便于确认任务追溯信息是否已经落盘。
    """
    try:
        saved = task_artifacts.patch_script_data(
            task_id,
            material_sources=material_sources,
        )
        if saved:
            logger.info(
                f"saved material source records: "
                f"task_id={task_id}, count={len(material_sources)}"
            )
    except Exception as exc:
        # task_artifacts 自身已经按失败降级设计，这里仍保留最后一道隔离，
        # 防止未来实现调整或目录解析异常意外影响素材下载返回值。
        logger.warning(
            "failed to persist material source records: "
            f"task_id={task_id}, error={type(exc).__name__}, detail={exc}"
        )






















def save_video(video_url: str, save_dir: str = "") -> str:
    if not save_dir:
        save_dir = utils.storage_dir("cache_videos")

    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    url_without_query = video_url.split("?")[0]
    url_hash = utils.md5(url_without_query)
    video_id = f"vid-{url_hash}"
    video_path = f"{save_dir}/{video_id}.mp4"

    # if video already exists, return the path
    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        logger.info(f"video already exists: {video_path}")
        return video_path

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    # if video does not exist, download it
    with open(video_path, "wb") as f:
        f.write(
            requests.get(
                video_url,
                headers=headers,
                proxies=config.proxy,
                verify=_get_tls_verify(),
                timeout=(60, 240),
            ).content
        )

    if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
        clip = None
        try:
            clip = VideoFileClip(video_path)
            duration = clip.duration
            fps = clip.fps
            if duration > 0 and fps > 0:
                return video_path
        except Exception as e:
            logger.warning(f"invalid video file: {video_path} => {str(e)}")
            try:
                os.remove(video_path)
            except Exception as remove_error:
                logger.warning(
                    f"failed to remove invalid video file: {video_path}, error: {str(remove_error)}"
                )
        finally:
            if clip is not None:
                try:
                    clip.close()
                except Exception as close_error:
                    logger.warning(
                        f"failed to close video clip: {video_path}, error: {str(close_error)}"
                    )
    return ""








if __name__ == "__main__":
    download_videos("test123", ["city skyline at dusk"], audio_duration=10)


# ---------------------------------------------------------------------------
# Footage generation
#
# Upstream searched Pexels / Pixabay / Coverr for stock clips. Those stock
# providers are gone; footage is now generated per search term with a dlazy
# video model. Generation is metered per clip, so this path computes exactly
# how many clips the audio needs instead of over-fetching and truncating, and
# caches by (term, ratio, duration) so a retried task does not pay twice.
# ---------------------------------------------------------------------------

DEFAULT_VIDEO_MODEL = "seedance-2.0-fast"
# The `duration` field is a string enum on every dlazy video model.
_CLIP_DURATION_CHOICES = (4, 5, 6, 7, 8, 9, 10, 12)
_generated_clip_cache: dict[tuple, str] = {}
_generation_lock = threading.Lock()


def _clip_duration_value(max_clip_duration: int) -> str:
    """Snap the requested clip length onto what the model actually accepts."""
    try:
        wanted = int(max_clip_duration or 5)
    except (TypeError, ValueError):
        wanted = 5
    allowed = [d for d in _CLIP_DURATION_CHOICES if d <= wanted] or [
        _CLIP_DURATION_CHOICES[0]
    ]
    return str(max(allowed))


def _shot_prompt(search_term: str) -> str:
    """Turn a bare keyword into a shot description the model can film."""
    term = (search_term or "").strip()
    return (
        f"Cinematic b-roll footage of {term}. Natural lighting, realistic motion, "
        f"no text, no captions, no watermark, no people speaking to camera."
    )


def generate_video_clip(
    search_term: str,
    video_aspect: VideoAspect = VideoAspect.portrait,
    max_clip_duration: int = 5,
    save_dir: str = "",
) -> MaterialInfo | None:
    """Generate one clip for a search term and return it as a MaterialInfo."""
    model = str(config.dlazy.get("video_model") or DEFAULT_VIDEO_MODEL)
    resolution = str(config.dlazy.get("video_resolution") or "720p")
    ratio = getattr(video_aspect, "value", str(video_aspect))
    duration = _clip_duration_value(max_clip_duration)

    cache_key = (model, search_term, ratio, duration, resolution)
    with _generation_lock:
        cached = _generated_clip_cache.get(cache_key)
    if cached and os.path.exists(cached):
        logger.info(f"reusing generated clip for '{search_term}': {cached}")
        return MaterialInfo(
            provider=model,
            url=cached,
            duration=int(duration),
            source_info={"provider": model, "search_term": search_term},
        )

    logger.info(f"generating {duration}s {ratio} clip for '{search_term}' via {model}")
    try:
        output = dlazy_client.run_tool(
            model,
            {
                "prompt": _shot_prompt(search_term),
                "generation_mode": "components",
                "resolution": resolution,
                "ratio": ratio,
                "duration": duration,
                # The voice-over is mixed in later; a generated soundtrack would
                # fight with it.
                "generate_audio": False,
                "promptRefs": [],
            },
        )
    except Exception as e:
        logger.error(f"clip generation failed for '{search_term}': {e}")
        return None

    urls = (output or {}).get("urls") or []
    if not urls:
        logger.error(f"{model} returned no video url for '{search_term}'")
        return None

    saved_path = save_video(video_url=urls[0], save_dir=save_dir)
    if not saved_path:
        return None

    with _generation_lock:
        _generated_clip_cache[cache_key] = saved_path
    return MaterialInfo(
        provider=model,
        url=saved_path,
        duration=int(duration),
        source_info={
            "provider": model,
            "search_term": search_term,
            "generated": True,
        },
    )


# ---------------------------------------------------------------------------
# Stock footage
#
# Pixabay search through dlazy: 1 credit and near-instant, versus a metered
# generation that takes minutes per clip. This is the default source;
# generation stays available for scripts the stock library cannot match.
# ---------------------------------------------------------------------------

# Pixabay rejects per_page outside 3-200; asking for 2 answers 400.
_STOCK_MIN_PER_PAGE = 3
_STOCK_PER_PAGE = 10
_ASPECT_TOLERANCE = 0.35


def _pick_stock_variant(hit: dict, video_aspect: VideoAspect):
    """Choose the download variant closest to the requested aspect ratio.

    Pixabay returns each clip in four sizes. Preferring a variant whose aspect
    already matches avoids letterboxing during assembly; `medium` comes first
    because `large` is often a 4K file well over 40 MB.
    """
    variants = hit.get("videos") or {}
    try:
        w, h = video_aspect.to_resolution()
        wanted = w / h
    except Exception:
        wanted = 9 / 16

    best = None
    for key in ("medium", "large", "small", "tiny"):
        v = variants.get(key)
        if not v or not v.get("url"):
            continue
        vw, vh = float(v.get("width") or 0), float(v.get("height") or 0)
        if vw <= 0 or vh <= 0:
            continue
        delta = abs((vw / vh) - wanted)
        if delta <= _ASPECT_TOLERANCE:
            return v
        if best is None or delta < best[0]:
            best = (delta, v)
    return best[1] if best else None


def search_stock_videos(
    search_term: str,
    video_aspect: VideoAspect,
    minimum_duration: int,
) -> List[MaterialInfo]:
    """Search the stock library for one term and return usable clips."""
    try:
        output = dlazy_client.run_tool(
            "search_video",
            {
                "query": search_term,
                "videoType": "all",
                "page": 1,
                "perPage": max(_STOCK_MIN_PER_PAGE, _STOCK_PER_PAGE),
            },
        )
    except Exception as e:
        logger.error(f"stock search failed for '{search_term}': {e}")
        return []

    items: List[MaterialInfo] = []
    for hit in (output or {}).get("hits") or []:
        duration = int(hit.get("duration") or 0)
        if duration < minimum_duration:
            continue
        variant = _pick_stock_variant(hit, video_aspect)
        if not variant:
            continue
        items.append(
            MaterialInfo(
                provider="pixabay",
                url=variant["url"],
                duration=duration,
                source_info={
                    "provider": "pixabay",
                    "search_term": search_term,
                    "asset_id": str(hit.get("id") or ""),
                    "page_url": hit.get("pageURL") or "",
                    "creator": hit.get("user") or "",
                },
            )
        )
    logger.info(f"found {len(items)} stock clip(s) for '{search_term}'")
    return items


def _download_stock_videos(
    task_id: str,
    search_terms: List[str],
    video_aspect: VideoAspect,
    video_concat_mode: VideoConcatMode,
    audio_duration: float,
    max_clip_duration: int,
    match_script_order: bool,
    material_directory: str,
) -> List[str]:
    """Collect stock clips until the narration is covered."""
    candidates: List[MaterialInfo] = []
    seen = set()
    for term in search_terms:
        for item in search_stock_videos(term, video_aspect, max_clip_duration):
            if item.url in seen:
                continue
            seen.add(item.url)
            candidates.append(item)

    if not candidates:
        logger.error("stock search returned no usable clips")
        return []

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if not match_script_order and concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(candidates)

    video_paths: List[str] = []
    material_sources: List[dict] = []
    total = 0.0
    for item in candidates:
        try:
            saved = save_video(video_url=item.url, save_dir=material_directory)
        except Exception as e:
            logger.error(f"failed to download stock clip: {type(e).__name__}: {e}")
            continue
        if not saved:
            continue
        video_paths.append(saved)
        try:
            material_sources.append(_material_source_record(item, saved))
        except Exception as source_error:
            logger.warning(
                "failed to prepare material source record: "
                f"error={type(source_error).__name__}"
            )
        total += min(max_clip_duration, item.duration)
        if total >= audio_duration:
            break

    logger.success(f"downloaded {len(video_paths)} stock clip(s)")
    _persist_material_sources(task_id, material_sources)
    return video_paths


def download_videos(
    task_id: str,
    search_terms: List[str],
    source: str = "stock",
    video_aspect: VideoAspect = VideoAspect.portrait,
    video_concat_mode: VideoConcatMode = VideoConcatMode.random,
    audio_duration: float = 0.0,
    max_clip_duration: int = 5,
    match_script_order: bool = False,
) -> List[str]:
    """Produce the footage the timeline needs.

    `source` selects the backend: "stock" searches the library through dlazy
    (1 credit, immediate), "generated" renders one clip per search term with a
    dlazy video model (metered per clip, minutes each).
    """
    material_directory = config.app.get("material_directory", "").strip()
    if material_directory == "task":
        material_directory = utils.task_dir(task_id)
    elif material_directory and not os.path.isdir(material_directory):
        material_directory = ""

    terms = [t for t in (search_terms or []) if str(t).strip()]
    if not terms:
        logger.error("no search terms to source footage from")
        return []

    if source != "generated":
        return _download_stock_videos(
            task_id=task_id,
            search_terms=terms,
            video_aspect=video_aspect,
            video_concat_mode=video_concat_mode,
            audio_duration=audio_duration,
            max_clip_duration=max_clip_duration,
            match_script_order=match_script_order,
            material_directory=material_directory,
        )

    clip_seconds = int(_clip_duration_value(max_clip_duration))
    needed = max(1, math.ceil((audio_duration or 0) / clip_seconds))
    logger.info(
        f"need {needed} clip(s) of {clip_seconds}s to cover {audio_duration:.1f}s of audio"
    )

    # Cycle through the terms so every part of the script gets screen time even
    # when the timeline needs more clips than there are terms.
    plan = [terms[i % len(terms)] for i in range(needed)]

    video_paths: List[str] = []
    material_sources: List[dict] = []
    for search_term in plan:
        item = generate_video_clip(
            search_term=search_term,
            video_aspect=video_aspect,
            max_clip_duration=clip_seconds,
            save_dir=material_directory,
        )
        if item is None:
            continue
        video_paths.append(item.url)
        try:
            material_sources.append(_material_source_record(item, item.url))
        except Exception as source_error:
            logger.warning(
                "failed to prepare material source record: "
                f"provider={item.provider}, error={type(source_error).__name__}"
            )

    concat_mode_value = getattr(video_concat_mode, "value", video_concat_mode)
    if not match_script_order and concat_mode_value == VideoConcatMode.random.value:
        random.shuffle(video_paths)

    logger.success(f"generated {len(video_paths)} clip(s)")
    _persist_material_sources(task_id, material_sources)
    return video_paths
