import json
import os.path
import re
from timeit import default_timer as timer

from loguru import logger

from app.config import config
from app.services import dlazy_client
from app.utils import utils

DEFAULT_ASR_MODEL = "fun-asr"
# dlazy speech-to-text accepts these two source languages only.
SUPPORTED_LANGS = {"zh", "en"}


def create(audio_file, subtitle_file: str = ""):
    """Transcribe the voice-over into an SRT through dlazy.

    Replaces the former local faster-whisper model. dlazy returns a flat word
    list rather than segments, so the sentence grouping below is the same
    punctuation-driven pass the whisper path used — only the source of the
    words changed.
    """
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    model = str(config.dlazy.get("asr_model") or DEFAULT_ASR_MODEL)
    lang = str(config.dlazy.get("asr_language") or "zh").lower()
    if lang not in SUPPORTED_LANGS:
        logger.warning(f"dlazy ASR supports zh/en only, falling back to zh for '{lang}'")
        lang = "zh"

    logger.info(f"start, model: {model}, language: {lang}, output file: {subtitle_file}")
    start = timer()

    try:
        audio_url = dlazy_client.upload_file(audio_file)
        output = dlazy_client.run_tool(
            model,
            {"audio_url": audio_url, "language_code": lang, "diarize": False},
        )
    except Exception as e:
        logger.error(f"transcription failed: {e}")
        return None

    raw_words = ((output or {}).get("data") or {}).get("words") or []
    words = []
    for w in raw_words:
        if w.get("type") not in (None, "word"):
            continue  # skip spacing / audio_event entries
        text = (w.get("text") or "").strip()
        if not text:
            continue
        words.append((text, w.get("start"), w.get("end")))

    if not words:
        logger.warning(f"{model} returned no words for {audio_file}")
        return None

    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return
        logger.debug("[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text))
        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    seg_text = ""
    seg_start = seg_end = 0.0
    is_segmented = False
    for text, w_start, w_end in words:
        if not is_segmented:
            seg_start = w_start if w_start is not None else seg_end
            is_segmented = True
        if w_end is not None:
            seg_end = w_end

        seg_text += text
        # Same rule as before: a word carrying punctuation closes the sentence.
        if utils.str_contains_punctuation(text):
            seg_text = seg_text[:-1]
            if not seg_text:
                is_segmented = False
                continue
            recognized(seg_text, seg_start, seg_end)
            is_segmented = False
            seg_text = ""

    if seg_text:
        recognized(seg_text, seg_start, seg_end)

    logger.info(f"complete, elapsed: {timer() - start:.2f} s")

    idx = 1
    out_lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            out_lines.append(
                utils.text_to_srt(
                    idx, text, subtitle.get("start_time"), subtitle.get("end_time")
                )
            )
            idx += 1

    with open(subtitle_file, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")
    logger.info(f"subtitle file created: {subtitle_file}")


def file_to_subtitles(filename):
    if not filename or not os.path.isfile(filename):
        return []

    times_texts = []
    current_times = None
    current_text = ""
    index = 0
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            times = re.findall("([0-9]*:[0-9]*:[0-9]*,[0-9]*)", line)
            if times:
                current_times = line
            elif line.strip() == "" and current_times:
                index += 1
                times_texts.append((index, current_times.strip(), current_text.strip()))
                current_times, current_text = None, ""
            elif current_times:
                current_text += line

    # Flush the final block. SRT files whose last subtitle is not followed by a
    # trailing blank line never hit the blank-line branch above, so without this
    # the last subtitle would be silently dropped.
    if current_times:
        index += 1
        times_texts.append((index, current_times.strip(), current_text.strip()))
    return times_texts


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_lines = utils.split_string_by_punctuations(normalized_script)

    corrected = False
    new_subtitle_items = []
    script_index = 0
    subtitle_index = 0

    while script_index < len(script_lines) and subtitle_index < len(subtitle_items):
        script_line = script_lines[script_index].strip()
        subtitle_line = subtitle_items[subtitle_index][2].strip()

        if script_line == subtitle_line:
            new_subtitle_items.append(subtitle_items[subtitle_index])
            script_index += 1
            subtitle_index += 1
        else:
            combined_subtitle = subtitle_line
            start_time = subtitle_items[subtitle_index][1].split(" --> ")[0]
            end_time = subtitle_items[subtitle_index][1].split(" --> ")[1]
            next_subtitle_index = subtitle_index + 1

            while next_subtitle_index < len(subtitle_items):
                next_subtitle = subtitle_items[next_subtitle_index][2].strip()
                if similarity(
                    script_line, combined_subtitle + " " + next_subtitle
                ) > similarity(script_line, combined_subtitle):
                    combined_subtitle += " " + next_subtitle
                    end_time = subtitle_items[next_subtitle_index][1].split(" --> ")[1]
                    next_subtitle_index += 1
                else:
                    break

            if similarity(script_line, combined_subtitle) > 0.8:
                logger.warning(
                    f"Merged/Corrected - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True
            else:
                logger.warning(
                    f"Mismatch - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True

            script_index += 1
            subtitle_index = next_subtitle_index

    # Process the remaining lines of the script.
    while script_index < len(script_lines):
        logger.warning(f"Extra script line: {script_lines[script_index]}")
        if subtitle_index < len(subtitle_items):
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    subtitle_items[subtitle_index][1],
                    script_lines[script_index],
                )
            )
            subtitle_index += 1
        else:
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    "00:00:00,000 --> 00:00:00,000",
                    script_lines[script_index],
                )
            )
        script_index += 1
        corrected = True

    if corrected:
        with open(subtitle_file, "w", encoding="utf-8") as fd:
            for i, item in enumerate(new_subtitle_items):
                fd.write(f"{i + 1}\n{item[1]}\n{item[2]}\n\n")
        logger.info("Subtitle corrected")
    else:
        logger.success("Subtitle is correct")


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
