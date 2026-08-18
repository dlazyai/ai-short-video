# ai-short-video

[English](./README.md) | [简体中文](./README_CN.md)

输入一个选题，产出一支成片——文案、素材、配音、字幕、配乐全包，**只用一个 API 密钥**。

所有模型调用都走 [dlazy](https://dlazy.com)。不用下载本地模型，不用准备 GPU，
也不用同时管理一堆各家厂商的密钥。

## 它做什么

给一个选题（或你自己写好的文案），流水线依次跑：

1. **文案**——LLM 写旁白，再从中提取驱动画面的检索关键词
2. **素材**——每个关键词生成一个片段，按你选的画幅出片
3. **配音**——TTS 渲染旁白
4. **字幕**——把渲染好的旁白转写回文字，时间轴来自真实音频而不是估算
5. **配乐**——纯器乐垫底，可生成也可从免版税曲库检索
6. **合成**——片段、旁白、字幕、配乐剪成一支视频

Streamlit 工作台、命令行、HTTP 接口三种用法。

## 环境要求

- Python 3.11+
- PATH 里有 ffmpeg **和 ffprobe**（pydub 读时长要用 ffprobe；有些便携版只带
  `ffmpeg.exe`，而 `app.ffmpeg_path` 只指向 ffmpeg，不覆盖 ffprobe）
- 一个 dlazy API 密钥——在
  [dlazy.com/dashboard/organization/api-key](https://dlazy.com/dashboard/organization/api-key) 获取

## 安装

```bash
git clone https://github.com/dlazyai/ai-short-video.git
cd ai-short-video
uv sync                       # 或: pip install -r requirements.txt
cp config.example.toml config.toml
```

启动工作台：

```bash
streamlit run ./webui/Main.py
```

或者用 Docker：

```bash
docker compose up -d          # 工作台 :8501，接口 :8080
```

## 配置

打开侧边栏，把 dlazy 密钥粘进去，配置就结束了。下面的模型选择器会按你的账号
过滤，只列出这个密钥真正能跑的模型：

| 设置项 | 可选 |
| --- | --- |
| 大语言模型 | `claude-sonnet-5`、`qwen3.8-max`、`kimi-k3` |
| 语音识别 | `fun-asr`、`elevenlabs-stt` |
| 配音 | `qwen-tts`、`doubao-tts`、`elevenlabs-tts` |
| 素材生成 | `seedance-2.0-fast`、`seedance-2.0`、`seedance-2.5`、`veo-3.1-fast`、`kling-v3`、`wan2.7` |
| 配乐 | `elevenlabs-music`、`suno-music`、`search_audio` |

其余参数都在 `config.toml` 里，设置页会替你写入。

## 成本与速度

素材生成是最贵的一环：按片计费，且一片要几分钟而不是几秒。所以流水线会按旁白
时长**精确算出需要几片**而不是先多取再截断，并按关键词缓存片段，重试任务不会
重复付费。

两个省钱的办法：

- 选快的素材模型（默认就是 `seedance-2.0-fast`）。
- 把 `video_source` 设成 `local`，让 `material_directory` 指向你自己的片源。
  不生成任何素材，流水线其余部分照常工作。

配乐选 `search_audio` 是检索免版税曲库而不是生成，比 `elevenlabs-music` 和
`suno-music` 便宜得多。

## 命令行

```bash
python cli.py --video-subject "复利是怎么回事"
```

其余参数见 `python cli.py --help`。

## 已知限制

把所有模型收敛到一家之后，这个 fork 确实放弃了一些东西，如实列在这里：

- **素材是生成的，不是搜库存片。** 画面比库存片更贴合文案，但更慢也要计费。
  用本地素材仍然免费。
- **转写只支持英文和中文。** 文案的*目标*语言不受限制。
- **没有音色克隆。** 配音只能用各模型自带的预置音色。
- **没有一键分发。** 成片是下载下来，不再直接发布到平台。

## 致谢

基于 [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)（作者
Harry，MIT 许可）构建。流水线设计出自上游，本 fork 只是把模型层换成了 dlazy。
完整改动清单见 [NOTICE.md](NOTICE.md)。

## 许可证

MIT——见 [LICENSE](LICENSE)。
