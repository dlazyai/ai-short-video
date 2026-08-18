import asyncio
import base64
import io
import inspect
import json
import math
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from typing import Union
from urllib.parse import urlparse
from xml.sax.saxutils import escape, unescape

import requests
from loguru import logger
from moviepy.video.tools import subtitles
from moviepy.audio.io.AudioFileClip import AudioFileClip

from app.config import config
from app.services import dlazy_client
from app.utils import utils


class SubMaker:
    """Minimal stand-in for edge_tts' SubMaker.

    dlazy TTS returns audio without word-level cues, so the timeline is rebuilt
    from the rendered audio duration instead. Only the two fields the rest of
    this project ever touched are kept: `subs` (sentence texts) and `offset`
    (start/end pairs in 100-nanosecond units).
    """

    def __init__(self):
        self.subs = []
        self.offset = []


NO_VOICE_NAME = "no-voice"
# `none` 是 PR #981 里曾使用过的无配音标识。这里短期兼容这个值，避免
# 已经手动调用过该分支的 API 用户升级后立即失效；WebUI 和新代码统一使用
# 更明确的 `no-voice`。
_NO_VOICE_ALIASES = {NO_VOICE_NAME, "none"}

DEFAULT_TTS_MODEL = "qwen-tts"
# Fallback voice per model, used when no voice is configured.
DEFAULT_VOICES = {
    "qwen-tts": "Cherry",
    "doubao-tts": "zh_female_shuangkuaisisi_uranus_bigtts",
    "elevenlabs-tts": "21m00Tcm4TlvDq8ikWAM",
}
TTS_MODELS = tuple(DEFAULT_VOICES)


def _configure_pydub_ffmpeg(audio_segment_cls):
    configured_ffmpeg = utils.get_ffmpeg_binary()
    if configured_ffmpeg:
        audio_segment_cls.converter = configured_ffmpeg


def mktimestamp(time_unit: float) -> str:
    """
    将 edge_tts 使用的 100 纳秒时间单位转换为字幕时间戳。

    edge_tts 7.x 不再导出旧版本里的 `mktimestamp`，但项目里旧字幕链路
    还需要这个格式化函数来兼容 Azure v2、Gemini、SiliconFlow 这些
    手工构造的字幕时间轴，因此这里内置一个等价实现。
    """
    hour = math.floor(time_unit / 10**7 / 3600)
    minute = math.floor((time_unit / 10**7 / 60) % 60)
    seconds = (time_unit / 10**7) % 60
    return f"{hour:02d}:{minute:02d}:{seconds:06.3f}"




















def parse_voice_name(name: str):
    # zh-CN-XiaoyiNeural-Female
    # zh-CN-YunxiNeural-Male
    # zh-CN-XiaoxiaoMultilingualNeural-V2-Female
    name = name.replace("-Female", "").replace("-Male", "").strip()
    return name


















def is_no_voice(voice_name: str | None) -> bool:
    """
    判断用户是否明确选择了“无配音”模式。

    这里刻意不把空字符串当成无配音：空 voice 更可能是配置损坏、旧版本
    WebUI 状态丢失或接口参数缺失。只有明确的 sentinel 才进入静音分支，
    这样可以避免把真实错误伪装成正常生成。
    """
    return str(voice_name or "").strip().lower() in _NO_VOICE_ALIASES


def estimate_no_voice_duration(text: str) -> float:
    """
    为无配音模式估算一个稳定的视频时间轴长度。

    无配音仍需要一个音频占位来驱动现有素材裁剪、字幕时间轴和最终合成。
    估算策略尽量简单：
    1. 中文等 CJK 字符按约 4.2 字/秒估算；
    2. 英文/数字按约 2.7 词/秒估算；
    3. 其他语种文字按约 4.0 字符/秒兜底估算，覆盖俄语、阿拉伯语、
       日文假名、韩文等非 ASCII 文本；
    4. 每个断句补一点停顿，让字幕切换不至于过于紧凑；
    5. 最少 3 秒，避免极短脚本生成 0 秒音频。
    """
    normalized_text = (text or "").strip()
    if not normalized_text:
        return 3.0

    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", normalized_text))
    words = len(re.findall(r"[A-Za-z0-9]+", normalized_text))
    ascii_word_chars = sum(len(word) for word in re.findall(r"[A-Za-z0-9]+", normalized_text))
    other_text_chars = 0
    for char in normalized_text:
        # Unicode category 以 L 开头表示各语种字母，N 表示数字。前面已经单独
        # 统计了 CJK 和 ASCII 单词，这里只统计剩余文字，避免英文被重复计时。
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            other_text_chars += 1
    other_text_chars = max(other_text_chars - cjk_chars - ascii_word_chars, 0)
    sentence_count = max(len(utils.split_string_by_punctuations(normalized_text)), 1)

    cjk_duration = cjk_chars / 4.2
    word_duration = words / 2.7
    other_text_duration = other_text_chars / 4.0
    pause_duration = max(sentence_count - 1, 0) * 0.35
    return max(3.0, cjk_duration + word_duration + other_text_duration + pause_duration)


def generate_silent_audio(duration_seconds: float, output_file: str) -> bool:
    """
    生成 MP3 静音音频，作为“无配音”模式的时间轴占位。

    使用 FFmpeg 的 anullsrc 直接生成静音，比先构造临时 WAV 再转码更少中间
    文件。失败时返回 False，让上层按普通 TTS 失败路径处理并记录日志。
    """
    ensure_file_path_exists(output_file)
    duration_seconds = max(float(duration_seconds or 0), 0.1)
    ffmpeg_binary = utils.get_ffmpeg_binary()
    command = [
        ffmpeg_binary,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        f"{duration_seconds:.3f}",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "4",
        output_file,
    ]

    logger.info(
        f"generating silent audio for no-voice mode, duration: {duration_seconds:.2f}s"
    )
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "failed to generate silent audio: "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
        return False
    if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
        logger.error(
            "silent audio output file is missing or empty, "
            f"file: {output_file}, duration: {duration_seconds:.2f}s"
        )
        return False
    return True


def parse_dlazy_voice(voice_name: str) -> tuple[str, str]:
    """Split a UI voice id into (model, voice).

    The picker stores `"<tts-model>:<voice-id>"`, e.g. `"qwen-tts:Cherry"`.
    A bare value is treated as a voice for the default model so older configs
    keep working.
    """
    name = (voice_name or "").strip()
    if ":" in name:
        model, voice = name.split(":", 1)
        model, voice = model.strip(), voice.strip()
        if model in DEFAULT_VOICES:
            return model, voice or DEFAULT_VOICES[model]
    model = str(config.dlazy.get("tts_model") or DEFAULT_TTS_MODEL)
    if model not in DEFAULT_VOICES:
        model = DEFAULT_TTS_MODEL
    return model, name or DEFAULT_VOICES[model]


def _build_tts_payload(model: str, text: str, voice: str) -> dict:
    """Every dlazy TTS tool declares all of its fields required, so send them in full."""
    if model == "qwen-tts":
        return {
            "prompt": text, "generation_mode": "system", "voice": voice,
            "voice_prompt": "", "language_type": "Auto", "promptRefs": [],
        }
    if model == "doubao-tts":
        return {
            "prompt": text, "voice_language": "zh-cn", "voiceId": voice,
            "speed_ratio": "1.0", "promptRefs": [],
        }
    if model == "elevenlabs-tts":
        return {
            "prompt": text, "use_custom_voice": False,
            "voice_language": "multilingual", "voiceId": voice,
            "stability": 0.5, "similarity_boost": 0.75, "style": 0, "promptRefs": [],
        }
    raise ValueError(f"unsupported dlazy TTS model: {model}")


def _apply_voice_rate(audio_file: str, voice_rate: float) -> None:
    """Re-time the rendered audio with ffmpeg's atempo.

    Only doubao-tts exposes a native speed control, so the rate is applied
    uniformly here instead — otherwise the setting would be silently ignored
    for the other voices.
    """
    try:
        rate = float(voice_rate or 1.0)
    except (TypeError, ValueError):
        return
    if abs(rate - 1.0) < 0.01:
        return
    # atempo only accepts 0.5–2.0 per filter instance; chain to reach the rest.
    factors, remaining = [], max(0.25, min(4.0, rate))
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)

    tmp = f"{audio_file}.rate.mp3"
    cmd = [
        utils.get_ffmpeg_binary(), "-y", "-loglevel", "error", "-i", audio_file,
        "-filter:a", ",".join(f"atempo={f:.4f}" for f in factors), tmp,
    ]
    try:
        subprocess.run(cmd, check=True)
        os.replace(tmp, audio_file)
    except Exception as e:
        logger.warning(f"failed to apply voice rate {rate}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def tts(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float = 1.0,
) -> Union[SubMaker, None]:
    if is_no_voice(voice_name):
        duration_seconds = estimate_no_voice_duration(text)
        if not generate_silent_audio(duration_seconds, voice_file):
            return None

        sub_maker = ensure_legacy_submaker_fields(SubMaker())
        return populate_legacy_submaker_with_full_text(
            sub_maker=sub_maker,
            text=text,
            audio_duration_seconds=duration_seconds,
        )

    model, voice = parse_dlazy_voice(voice_name)
    logger.info(f"tts model: {model}, voice: {voice}")
    ensure_file_path_exists(voice_file)

    try:
        output = dlazy_client.run_tool(model, _build_tts_payload(model, text, voice))
        urls = (output or {}).get("urls") or []
        if not urls:
            raise ValueError(f"[{model}] returned no audio url")

        raw = f"{voice_file}.download"
        dlazy_client.download(urls[0], raw)
        # The pipeline downstream (moviepy, pydub) expects the configured path.
        os.replace(raw, voice_file)
        _apply_voice_rate(voice_file, voice_rate)
    except Exception as e:
        logger.error(f"tts failed: {e}")
        return None

    duration_seconds = _get_audio_duration_from_file(voice_file)
    if not duration_seconds:
        logger.error(f"tts produced an unreadable audio file: {voice_file}")
        return None

    # dlazy TTS returns no word-level cues, so the sentence timeline is derived
    # from the rendered duration. Accurate per-word timing comes from the ASR
    # subtitle path instead.
    sub_maker = ensure_legacy_submaker_fields(SubMaker())
    return populate_legacy_submaker_with_full_text(
        sub_maker=sub_maker,
        text=text,
        audio_duration_seconds=duration_seconds,
    )


def convert_rate_to_percent(rate: float) -> str:
    # edge-tts requires a sign-prefixed percentage (e.g. "+0%", "-20%").
    # Rounding can yield 0 for rates near but not equal to 1.0 (e.g. 1.004,
    # 0.997); those must still be returned as "+0%", not the unsigned "0%"
    # which edge-tts rejects with ValueError: Invalid rate '0%'.
    # API 或批处理调用可能传入 0、0.0、None 或无法转换的空值；这些值不代表
    # 合法语速，直接计算会变成 -100% 或抛异常。这里统一回退到正常语速，
    # 避免生成极慢音频或让 TTS 流程在边界输入下失败。
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        rate = 1.0
    if rate <= 0:
        rate = 1.0
    percent = round((rate - 1.0) * 100)
    if percent >= 0:
        return f"+{percent}%"
    return f"{percent}%"


def ensure_file_path_exists(file_path: str) -> None:
    """
    确保输出文件所在目录一定存在。

    这里单独做一层兜底，是因为 edge_tts 7.x 在真正发起网络请求之前，
    就会先打开目标音频文件；如果目录不存在，会直接因为本地文件路径报错，
    从而掩盖真正的 TTS 行为结果。
    """
    dir_path = os.path.dirname(file_path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)


def ensure_legacy_submaker_fields(sub_maker: SubMaker) -> SubMaker:
    """
    为项目里仍然沿用旧字幕结构的调用方补齐兼容字段。

    edge_tts 7.x 的 `SubMaker` 主要暴露 `cues/get_srt()`，但项目里 Azure v2、
    Gemini、SiliconFlow 这些路径仍然会直接读写 `subs/offset`。这里统一补齐，
    避免升级 edge_tts 后这些非 edge 路径被连带破坏。
    """
    if not hasattr(sub_maker, "subs"):
        sub_maker.subs = []
    if not hasattr(sub_maker, "offset"):
        sub_maker.offset = []
    return sub_maker


def populate_legacy_submaker_with_full_text(
    sub_maker: SubMaker, text: str, audio_duration_seconds: float
) -> SubMaker:
    """
    用整段文本填充项目历史沿用的 `subs/offset` 字幕结构。

    背景：
    1. edge_tts 7.x 的 `SubMaker` 不再提供旧版本里的 `create_sub()`；
    2. 项目里 Gemini、SiliconFlow 等非 edge 路径依然需要返回一个
       带 `subs/offset` 的对象，供后续统一计算音频时长和生成字幕；
    3. 对于拿不到逐词边界的 TTS 服务，需要至少按脚本断句切成多个片段，
       这样后续 `subtitle_provider=edge` 的聚合逻辑才能继续工作，而不是
       因为整段文本无法和脚本断句逐行匹配而回退 Whisper。

    Args:
        sub_maker: 需要写入兼容字段的字幕对象
        text: 原始脚本文本
        audio_duration_seconds: 音频总时长，单位秒

    Returns:
        已填充兼容字幕数据的 SubMaker 对象
    """
    sub_maker = ensure_legacy_submaker_fields(sub_maker)

    # 清空旧值，避免调用方重复复用对象时出现脏数据叠加。
    sub_maker.subs = []
    sub_maker.offset = []

    normalized_text = (text or "").strip()
    if not normalized_text:
        return sub_maker

    audio_duration_100ns = max(int(audio_duration_seconds * 10000000), 1)

    # Gemini / SiliconFlow 这类路径拿不到逐词边界时，仍然尽量沿用项目
    # 原来的“按标点断句 + 按字符数比例分配时长”的策略。这样既能让
    # create_subtitle() 匹配脚本断句，也能避免再次回退 Whisper。
    sentences = utils.split_string_by_punctuations(normalized_text)
    if not sentences:
        sentences = [normalized_text]

    total_chars = sum(len(sentence) for sentence in sentences)
    if total_chars <= 0:
        sub_maker.subs.append(normalized_text)
        sub_maker.offset.append((0, audio_duration_100ns))
        return sub_maker

    current_offset = 0
    for index, sentence in enumerate(sentences):
        cleaned_sentence = sentence.strip()
        if not cleaned_sentence:
            continue

        # 前面的句子按字符数比例分配时长，最后一句兜底吃掉剩余时长，
        # 避免整数取整导致总时长丢失或字幕结束时间短于音频。
        if index == len(sentences) - 1:
            sentence_end = audio_duration_100ns
        else:
            sentence_chars = len(cleaned_sentence)
            sentence_duration = max(
                int(audio_duration_100ns * (sentence_chars / total_chars)),
                1,
            )
            sentence_end = min(current_offset + sentence_duration, audio_duration_100ns)

        sub_maker.subs.append(cleaned_sentence)
        sub_maker.offset.append((current_offset, sentence_end))
        current_offset = sentence_end

    return sub_maker








































def _format_text(text: str) -> str:
    """
    清理字幕对齐前的脚本文本。

    这里不能只在 LLM 生成阶段处理，因为用户也可能手动粘贴脚本，或通过
    API 直接传入包含 Markdown 标记的文本。TTS 通常不会朗读 `---`、
    `___`、`***` 这类分隔符行，也不会朗读 `_` 这种强调标记；如果字幕
    对齐仍保留这些字符，`create_subtitle()` 会一直等待不存在的 cue，
    最终导致字幕文件缺失并在 Whisper fallback 校正时补出全 0 时间轴。
    """
    text = text.replace("[", " ")
    text = text.replace("]", " ")
    text = text.replace("(", " ")
    text = text.replace(")", " ")
    text = text.replace("{", " ")
    text = text.replace("}", " ")
    return utils.normalize_script_for_subtitle_matching(text)


def _build_subtitle_formatter():
    """
    返回统一的 SRT 行格式化函数。

    这里单独拆成一个小工具，是为了让 edge_tts 7.x 的 cues 路径
    和项目原有的 legacy `subs/offset` 路径共用同一套字幕落盘格式，
    避免两套逻辑各自产生细微格式差异。
    """

    def formatter(idx: int, start_time: float, end_time: float, sub_text: str) -> str:
        start_t = mktimestamp(start_time).replace(".", ",")
        end_t = mktimestamp(end_time).replace(".", ",")
        return f"{idx}\n{start_t} --> {end_t}\n{sub_text}\n"

    return formatter


# 阿拉伯语变音符号和 Tatweel 拉长符在 edge-tts 返回文本中可能出现，
# 这些字符不影响语义，但会导致脚本文本和字幕 cue 字符串精确匹配失败。
_ARABIC_DIACRITICS = re.compile("[\u0610-\u061A\u064B-\u065F\u0670\u0640\u06D6-\u06ED]")


def _normalize_arabic(text: str) -> str:
    """统一阿拉伯语常见字母变体，提升字幕 cue 与脚本行的匹配容错率。

    edge-tts 对阿拉伯语可能返回与原脚本不同的字母形态，例如把 أ/إ/آ
    归一成 ا，或者携带变音符号。这里仅在最后一层匹配兜底中使用，
    不改变原始字幕文本，避免影响最终展示内容。
    """
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in (
        ("أإآٱ", "ا"),
        ("ىئ", "ي"),
        ("ة", "ه"),
        ("ؤ", "و"),
    ):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def _match_script_line(script_lines: list[str], current_text: str, sub_index: int) -> str:
    """
    尝试把当前累计的字幕文本，与脚本中的某一条标准断句匹配起来。

    这里复用了项目原有的“按标点拆脚本，再逐段比对”的思路：
    1. 优先精确匹配；
    2. 再做一次去标点和 Markdown `_` 格式符后的匹配；
    3. 最后做一次阿拉伯语字符形态归一化匹配。

    这样可以兼容：
    - TTS 返回里可能缺失或单独拆分的标点；
    - 中文场景下词边界和脚本文本不完全一一对应的情况。
    """
    if len(script_lines) <= sub_index:
        return ""

    target_line = script_lines[sub_index]
    if current_text == target_line:
        return target_line.strip()

    current_text_normalized = re.sub(r"[_\W]+", "", current_text)
    target_line_normalized = re.sub(r"[_\W]+", "", target_line)
    if current_text_normalized == target_line_normalized:
        return target_line.strip()

    # 最后一层阿拉伯语容错：edge-tts 返回的字母形态、变音符号或 Tatweel
    # 可能和脚本不同。只在常规匹配失败后归一化比较，非阿拉伯语文本不会受影响。
    current_ar = re.sub(r"[_\W]+", "", _normalize_arabic(current_text))
    target_ar = re.sub(r"[_\W]+", "", _normalize_arabic(target_line))
    if current_ar and current_ar == target_ar:
        return target_line.strip()

    return ""


def _write_subtitle_items(sub_items: list[str], subtitle_file: str) -> bool:
    """
    将已经聚合好的字幕段写入到 SRT 文件，并做一次基本可读性验证。

    返回值：
    - `True`：字幕文件成功落盘且可被 moviepy 解析；
    - `False`：字幕文件写入或解析失败。
    """
    try:
        ensure_file_path_exists(subtitle_file)
        with open(subtitle_file, "w", encoding="utf-8") as file:
            file.write("\n".join(sub_items) + "\n")

        sbs = subtitles.file_to_subtitles(subtitle_file, encoding="utf-8")
        duration = max([tb for ((ta, tb), txt) in sbs]) if sbs else 0
        logger.info(
            f"completed, subtitle file created: {subtitle_file}, duration: {duration}"
        )
        return True
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")
        if os.path.exists(subtitle_file):
            os.remove(subtitle_file)
        return False




def _build_subtitle_items_from_legacy_submaker(
    sub_maker: SubMaker, script_lines: list[str]
) -> list[str]:
    """
    将项目原有 `subs/offset` 结构聚合为按脚本断句的 SRT 片段。

    这部分保留了原来的核心思路，只是拆成独立函数，便于与 edge_tts 7.x
    的 cues 聚合逻辑共享同一套断句匹配与落盘流程。
    """
    formatter = _build_subtitle_formatter()
    start_time = -1.0
    sub_items = []
    sub_index = 0
    sub_line = ""

    legacy_offsets = getattr(sub_maker, "offset", [])
    legacy_subs = getattr(sub_maker, "subs", [])
    for _, (offset, sub) in enumerate(zip(legacy_offsets, legacy_subs)):
        current_start_time, current_end_time = offset
        if start_time < 0:
            start_time = current_start_time

        sub_line += unescape(sub)
        matched_text = _match_script_line(script_lines, sub_line, sub_index)
        if not matched_text:
            continue

        sub_index += 1
        sub_items.append(
            formatter(
                idx=sub_index,
                start_time=start_time,
                end_time=current_end_time,
                sub_text=matched_text,
            )
        )
        start_time = -1.0
        sub_line = ""

    if sub_line.strip():
        logger.warning(
            f"legacy subtitle items still have unmatched text after aggregation: {sub_line}"
        )

    return sub_items


def create_subtitle(sub_maker: SubMaker, text: str, subtitle_file: str):
    """
    优化字幕文件
    1. 将字幕文件按照标点符号分割成多行
    2. 逐行匹配字幕文件中的文本
    3. 生成新的字幕文件
    """
    text = _format_text(text)
    script_lines = utils.split_string_by_punctuations(text)
    try:
        sub_items = _build_subtitle_items_from_legacy_submaker(
            sub_maker, script_lines
        )

        if len(sub_items) != len(script_lines):
            logger.warning(
                f"failed, sub_items len: {len(sub_items)}, script_lines len: {len(script_lines)}"
            )
            return

        _write_subtitle_items(sub_items, subtitle_file)
    except Exception as e:
        logger.error(f"failed, error: {str(e)}")


def _get_audio_duration_from_submaker(sub_maker: SubMaker):
    """
    获取音频时长
    """
    # 优先兼容 edge_tts 7.x 的 cues 结构；
    # 如果是项目里其他 TTS 手工填充的旧结构，则继续读取 offset。
    if hasattr(sub_maker, "cues") and sub_maker.cues:
        return sub_maker.cues[-1].end.total_seconds()

    legacy_offsets = getattr(sub_maker, "offset", [])
    if not legacy_offsets:
        return 0.0
    return legacy_offsets[-1][1] / 10000000

def _get_audio_duration_from_file(audio_file: str) -> float:
    """
    获取音频文件时长（支持 mp3/m4a/wav/aac 等 ffmpeg 可解码的格式）
    """
    if not os.path.exists(audio_file):
        logger.error(f"audio file does not exist: {audio_file}")
        return 0.0

    try:
        # Use moviepy (ffmpeg) to read the duration of any supported audio format
        with AudioFileClip(audio_file) as audio:
            return audio.duration  # Duration in seconds
    except Exception as e:
        logger.error(f"Failed to get audio duration from file: {str(e)}")
        return 0.0

def get_audio_duration(target: Union[str, SubMaker]) -> float:
    """
    获取音频时长
    如果是SubMaker对象，则从SubMaker中获取时长
    如果是音频文件路径，则从音频文件中获取时长（支持 mp3/m4a/wav 等格式）
    """
    if isinstance(target, SubMaker):
        return _get_audio_duration_from_submaker(target)
    elif isinstance(target, str):
        return _get_audio_duration_from_file(target)
    else:
        logger.error(f"Invalid target type: {type(target)}")
        return 0.0
