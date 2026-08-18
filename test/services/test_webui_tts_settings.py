import json
import os
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import voice


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
LOCALES = ("de", "en", "es", "id", "pt", "ru", "tr", "vi", "zh")

# 每个服务商只维护一个官方入口。Chatterbox 是自托管服务，没有统一的 Key
# 领取平台，因此链接到实际使用的兼容服务配置说明，避免误导用户注册第三方账号。
TTS_API_KEY_LABELS = {
    "Speech Key": "portal.azure.com",
    "SiliconFlow API Key": "cloud.siliconflow.cn/account/ak",
    "Gemini API Key": "aistudio.google.com/app/apikey",
    "MiMo API Key": "mimo.mi.com/docs/",
    "MiniMax TTS API Key": "platform.minimaxi.com",
    "ElevenLabs API Key": "elevenlabs.io/app/settings/api-keys",
    "Chatterbox API Key": "github.com/travisvn/chatterbox-tts-api",
}

TTS_PROVIDER_WIDGETS = {
    "azure-tts-v2": ("azure_speech_key_input", "Speech Key"),
    "siliconflow": ("siliconflow_api_key_input", "SiliconFlow API Key"),
    "gemini-tts": ("gemini_tts_api_key_input", "Gemini API Key"),
    "mimo-tts": ("mimo_tts_api_key_input", "MiMo API Key"),
    "minimax-tts": ("minimax_tts_api_key_input", "MiniMax TTS API Key"),
    "elevenlabs": ("elevenlabs_api_key_input", "ElevenLabs API Key"),
    "chatterbox": ("chatterbox_api_key_input", "Chatterbox API Key"),
}


def _load_translation(locale: str) -> dict:
    """直接读取语言文件，确保断言覆盖用户实际看到的最终 Markdown 标签。"""
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data["Translation"]


def _widget_by_key(elements, key: str):
    """Streamlit 控件标签会翻译，使用稳定业务 key 定位真实输入框。"""
    return next(
        item
        for item in elements
        if str(getattr(item, "key", "")) == key
        or str(getattr(item, "key", "")).startswith(f"{key}_")
    )
















