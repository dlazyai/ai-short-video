import asyncio
import base64
import os
import shutil
import unittest
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# add project root to python path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.utils import utils
from app.services import voice as vs
from app.services import task as task_service
from pydub import AudioSegment

temp_dir = utils.storage_dir("temp")

text_en = """
What is the meaning of life? 
This question has puzzled philosophers, scientists, and thinkers of all kinds for centuries. 
Throughout history, various cultures and individuals have come up with their interpretations and beliefs around the purpose of life. 
Some say it's to seek happiness and self-fulfillment, while others believe it's about contributing to the welfare of others and making a positive impact in the world. 
Despite the myriad of perspectives, one thing remains clear: the meaning of life is a deeply personal concept that varies from one person to another. 
It's an existential inquiry that encourages us to reflect on our values, desires, and the essence of our existence.
"""

text_zh = """
预计未来3天深圳冷空气活动频繁，未来两天持续阴天有小雨，出门带好雨具；
10-11日持续阴天有小雨，日温差小，气温在13-17℃之间，体感阴凉；
12日天气短暂好转，早晚清凉；
"""

voice_rate=1.0
voice_volume=1.0
RUN_INTEGRATION_TESTS = os.environ.get("MPT_RUN_INTEGRATION_TESTS", "").lower() in {
    "1",
    "true",
    "yes",
}
                    
class TestVoiceService(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
    
    def tearDown(self):
        self.loop.close()



    def test_no_voice_tts_generates_silent_audio_and_subtitle_timeline(self):
        """
        无配音模式不调用任何外部 TTS provider，只生成静音音频作为时间轴占位。
        这里 mock FFmpeg，验证请求参数、输出文件和 legacy 字幕结构都符合后续
        视频合成链路的预期。
        """

        def fake_run(command, capture_output, text, check):
            self.assertEqual(command[0], "/tmp/fake-ffmpeg")
            self.assertIn("anullsrc=r=44100:cl=mono", command)
            Path(command[-1]).write_bytes(b"fake-silent-mp3")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.utils,
            "get_ffmpeg_binary",
            return_value="/tmp/fake-ffmpeg",
        ), patch.object(vs.subprocess, "run", side_effect=fake_run):
            voice_file = str(Path(tmp_dir) / "silent.mp3")
            sub_maker = vs.tts(
                text="第一句话。Second sentence.",
                voice_name=vs.NO_VOICE_NAME,
                voice_rate=1.0,
                voice_file=voice_file,
            )

            self.assertEqual(Path(voice_file).read_bytes(), b"fake-silent-mp3")

        self.assertIsNotNone(sub_maker)
        self.assertEqual(getattr(sub_maker, "subs", []), ["第一句话", "Second sentence"])
        self.assertEqual(len(getattr(sub_maker, "offset", [])), 2)
        self.assertGreater(vs.get_audio_duration(sub_maker), 0)

    def test_get_audio_duration_accepts_non_mp3_files(self):
        """
        自定义音频（custom_audio_file）常见为 m4a/wav/aac 等非 mp3 格式。
        get_audio_duration 不应因扩展名不是 .mp3 就报 "Invalid target type" 并返回 0，
        而应交给 moviepy(ffmpeg) 读取真实时长。
        """
        for path in ("custom-audio.m4a", "voice.wav", "clip.aac"):
            with patch.object(vs.os.path, "exists", return_value=True), \
                    patch.object(vs, "AudioFileClip") as mock_afc:
                mock_afc.return_value.__enter__.return_value.duration = 28.89
                self.assertEqual(vs.get_audio_duration(path), 28.89)
                mock_afc.assert_called_once_with(path)

    def test_get_audio_duration_missing_file_returns_zero(self):
        """音频文件不存在时安全返回 0，而不是抛异常或读取失败。"""
        with patch.object(vs.os.path, "exists", return_value=False):
            self.assertEqual(vs.get_audio_duration("does-not-exist.m4a"), 0.0)

    def test_no_voice_alias_none_is_supported_temporarily(self):
        """
        兼容 PR #981 曾使用过的 none sentinel，避免少量直接调用 API 的用户
        升级后立即失效。新 UI 和新代码仍统一使用 no-voice。
        """
        self.assertTrue(vs.is_no_voice("none"))
        self.assertTrue(vs.is_no_voice(vs.NO_VOICE_NAME))
        self.assertFalse(vs.is_no_voice(""))

    def test_no_voice_duration_estimates_non_ascii_languages(self):
        """
        无配音没有真实 TTS 音频，只能根据脚本文字估算阅读时间。俄语、阿拉伯语、
        日文假名、韩文等非 ASCII 文本也必须参与估算，不能都落到最短 3 秒。
        """
        russian_text = (
            "Это длинный тестовый сценарий без озвучки. "
            "Он должен получить достаточно времени для чтения субтитров."
        )
        arabic_text = "هذا اختبار طويل بدون تعليق صوتي، ويجب أن يحصل على وقت كاف لقراءة الترجمة."

        self.assertGreater(vs.estimate_no_voice_duration(russian_text), 8.0)
        self.assertGreater(vs.estimate_no_voice_duration(arabic_text), 8.0)

    def test_generate_silent_audio_rejects_missing_output_file(self):
        """
        即使 FFmpeg 进程返回成功，也要确认输出文件真实存在且非空。这样可以把
        异常收敛在 TTS 阶段，而不是拖到后续视频合成阶段才暴露。
        """
        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            vs.utils,
            "get_ffmpeg_binary",
            return_value="/tmp/fake-ffmpeg",
        ), patch.object(
            vs.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ):
            voice_file = str(Path(tmp_dir) / "missing-silent.mp3")

            self.assertFalse(vs.generate_silent_audio(3.0, voice_file))


    



















    def test_script_split_keeps_thousand_separator_comma(self):
        """
        Edge TTS 会把 "1,000 years" 作为连续文本返回。脚本断句时不能把
        数字中间的英文逗号当成句子边界，否则字幕聚合会出现 issue #894
        里的 sub_items 数量少于 script_lines，并错误回退 Whisper。
        """
        text = (
            "It takes about 1,000 years for a single drop of water to finish "
            "the whole trip!"
        )

        self.assertEqual(
            utils.split_string_by_punctuations(text),
            [
                (
                    "It takes about 1,000 years for a single drop of water to finish "
                    "the whole trip"
                )
            ],
        )


    def test_script_split_supports_arabic_punctuation(self):
        """
        阿拉伯语脚本常用 ، ؛ ؟ 作为自然断句标点。断句阶段必须识别这些
        标点，否则 edge-tts cue 的停顿边界和脚本行边界会错位。
        """
        text = "مرحبا بالعالم، كيف حالك؟ هذا اختبار؛ يعمل بشكل جيد."

        self.assertEqual(
            utils.split_string_by_punctuations(text),
            [
                "مرحبا بالعالم",
                "كيف حالك",
                "هذا اختبار",
                "يعمل بشكل جيد",
            ],
        )

    def test_match_script_line_normalizes_arabic_letter_forms(self):
        """
        edge-tts 可能把阿拉伯语中的不同字母形态归一化，或返回带变音符号、
        Tatweel 的 cue 文本。匹配时应容错，但最终字幕仍保留原始脚本文案。
        """
        script_lines = ["أهلاً وسهلاً بك في المدرسة"]

        matched = vs._match_script_line(
            script_lines,
            "اهلا وسهلا بك في المدرسه",
            0,
        )

        self.assertEqual(matched, script_lines[0])




    def test_convert_rate_to_percent_signs_zero_rate(self):
        # Rates near but not exactly 1.0 round to 0 percent. edge-tts rejects
        # an unsigned "0%" (ValueError: Invalid rate '0%'), so the helper must
        # emit a sign-prefixed "+0%". Regression test for that crash.
        self.assertEqual(vs.convert_rate_to_percent(1.0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(1.004), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(0.997), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(1.5), "+50%")
        self.assertEqual(vs.convert_rate_to_percent(0.8), "-20%")

    def test_convert_rate_to_percent_invalid_values_default_to_normal(self):
        # API 和批处理脚本可能把空语速传成 0、None 或空字符串；这些都不应让
        # edge-tts 收到 -100% 或触发异常，而是按正常语速处理。
        self.assertEqual(vs.convert_rate_to_percent(0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(0.0), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(None), "+0%")
        self.assertEqual(vs.convert_rate_to_percent(""), "+0%")





class TestTtsPromptSplitting(unittest.TestCase):
    """dlazy 的 TTS 工具对 prompt 有长度上限，超了会整次调用报 400。

    qwen-tts 是 512 字符，一段普通的单段文案就能超过；上限是按次调用算的，
    所以必须先把文稿切开再逐段合成，否则整个任务在 audio 阶段就失败了。
    """

    LIMIT = 512

    def test_short_text_is_not_split(self):
        self.assertEqual(vs._split_for_tts("只有一句话。", self.LIMIT), ["只有一句话。"])

    def test_empty_text_yields_no_chunks(self):
        self.assertEqual(vs._split_for_tts("", self.LIMIT), [])
        self.assertEqual(vs._split_for_tts("   ", self.LIMIT), [])

    def test_chunks_stay_within_limit(self):
        text = "这是一句用来测试的话。" * 80
        chunks = vs._split_for_tts(text, self.LIMIT)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), self.LIMIT)

    def test_split_preserves_content(self):
        text = "First sentence here. Second sentence follows. " * 20
        chunks = vs._split_for_tts(text, self.LIMIT)
        # 只在空白处收尾，所以去掉空白后内容必须完全一致。
        self.assertEqual(
            "".join(chunks).replace(" ", ""), text.replace(" ", "").strip()
        )

    def test_run_on_sentence_is_hard_cut(self):
        # 没有任何标点的超长串：切不到句界也不能整段送出去。
        text = "啊" * (self.LIMIT * 2 + 30)
        chunks = vs._split_for_tts(text, self.LIMIT)
        self.assertEqual(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(len(c), self.LIMIT)
        self.assertEqual("".join(chunks), text)

    def test_synthesize_issues_one_call_per_chunk(self):
        text = "这是一句用来测试的话。" * 80
        calls = []

        def fake_run_tool(model, payload, **kwargs):
            calls.append(payload["prompt"])
            return {"urls": [f"https://example.invalid/{len(calls)}.wav"]}

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "audio.mp3")
            with patch.object(vs.dlazy_client, "prompt_limit", return_value=self.LIMIT), \
                 patch.object(vs.dlazy_client, "run_tool", side_effect=fake_run_tool), \
                 patch.object(vs.dlazy_client, "download",
                              side_effect=lambda url, path: Path(path).write_bytes(b"x")), \
                 patch.object(vs, "_concat_audio") as concat:
                vs._synthesize("qwen-tts", text, "Cherry", out)

            self.assertGreater(len(calls), 1)
            for prompt in calls:
                self.assertLessEqual(len(prompt), self.LIMIT)
            # 分块时必须走拼接，而不是只留下最后一段。
            concat.assert_called_once()
            self.assertEqual(len(concat.call_args[0][0]), len(calls))

    def test_synthesize_without_declared_limit_sends_one_call(self):
        calls = []

        def fake_run_tool(model, payload, **kwargs):
            calls.append(payload["prompt"])
            return {"urls": ["https://example.invalid/1.wav"]}

        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "audio.mp3")
            with patch.object(vs.dlazy_client, "prompt_limit", return_value=None), \
                 patch.object(vs.dlazy_client, "run_tool", side_effect=fake_run_tool), \
                 patch.object(vs.dlazy_client, "download",
                              side_effect=lambda url, path: Path(path).write_bytes(b"x")):
                vs._synthesize("elevenlabs-tts", "随便一段话。" * 200, "v", out)

            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    # python -m unittest test.services.test_voice.TestVoiceService.test_azure_tts_v1
    # python -m unittest test.services.test_voice.TestVoiceService.test_azure_tts_v2
    unittest.main()
