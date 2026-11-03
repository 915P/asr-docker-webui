# asr —— 本地语音转录（CPU / Docker / WebUI）

把音频、视频转成**带时间戳和说话人标签**的中文文本。纯 CPU，不需要显卡，普通笔记本 2 小时 16 分的录音约 9 分钟转完（约 21 倍实时）。

```text
[00:00:03 - 00:00:25] [说话人0] 那么先讲一点小小的感想啊……
[01:52:07 - 01:52:30] [说话人1] 特别留下来一段时间给我们现场的师生……
```

## 特性

- **说话人分离**：自动区分主讲人、主持人、提问者，全篇只在真实换人时切换
- **自带标点**：中文逗号句号问号齐全，不需要额外的标点模型
- **WebUI + CLI 双入口**：浏览器拖拽上传，或一行命令处理目录里的文件
- **三格式输出**：`.txt`（阅读）、`.srt`（字幕）、`.json`（结构化，含覆盖率与逐段时间轴）
- **Docker 化**：模型烤进镜像，`docker start/stop` 即可管理，不污染宿主环境

## 快速开始

### 1. 构建镜像

```bash
docker build \
    --build-context models=$HOME/.local/share/asr/modelscope \
    -t asr:latest .
```

> **`--build-context` 不能省。** `.dockerignore` 是白名单（防止虚拟环境被打进 build context），宿主上的模型权重在 context 之外，必须通过命名 build context 引入。
>
> 如果宿主还没有模型，先下载（约 930 MB，之后重建镜像都是本地复制）：
> ```bash
> export MODELSCOPE_CACHE=$HOME/.local/share/asr/modelscope
> modelscope download --model iic/SenseVoiceSmall
> modelscope download --model iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
> modelscope download --model iic/speech_campplus_sv_zh-cn_16k-common
> ```
> 必须用**完整模型 ID**，写别名（如 `iic/fsmn-vad`）会报 `record not found`。

### 2. WebUI

```bash
docker run -d --name asr-webui --restart unless-stopped -p 7870:7870 asr:latest
curl http://127.0.0.1:7870/api/health     # {"ok":true,"jobs":0}
```

打开 <http://127.0.0.1:7870>，拖入音频 → 选选项 → 开始转录，完成后在线看全文并下载三种格式。

### 3. 命令行

```bash
docker run --rm -v 音频所在目录:/data asr:latest /data/录音.mp3
```

常用参数：

| 参数 | 作用 |
|------|------|
| `--limit N` | 只转前 N 秒（先试跑看质量） |
| `--out 前缀` | 指定输出路径前缀（默认与音频同名同目录） |
| `--no-spk` | 关闭说话人分离，更快 |
| `--language zh\|auto\|en` | 语种，默认中文 |
| `--ncpu N` | 线程数，默认 10 |
| `--keep-work` | 保留中间 wav |

挂载目录里写出的文件属主为 **uid 1000**，与常见桌面账号一致，不需要 `chown`。

## 管线

```text
音频 ──ffmpeg──▶ 16k 单声道 wav
        │
        ├─ fsmn-vad      切段（max_single_segment_time=30000ms，避免长段丢字）
        ├─ SenseVoiceSmall  识别（CTC 非自回归，use_itn=True 才出标点）
        └─ cam++         声纹向量 → 聚类 → 说话人标签
        │
        └─ 按 sentence_info 的 start/end/sentence/spk 组装 → txt/json/srt
```

## 实测结论（供调参参考）

| 项目 | 结论 |
|------|------|
| 线程数 | **10 最优**。4=32.7s、8=27.7s、**10=26.7s**、12=36.4s、14=51.6s。异构核（P/E/LP E）上超订会崩塌，**别设成物理核数** |
| VAD 覆盖率 | 正常 **85%~95%**。低于 70% 说明底噪大或音量过低 |
| 标点 | 必须 `use_itn=True`。funasr 官方 CLI 没暴露这个参数，所以它跑出来整篇没标点 |
| 精度 | 无 AVX-512/AMX 也能跑，实测 RTF 0.05~0.06 |
| 不需要 | `ct-punc` 标点模型（SenseVoice 自带，装了白占 1.2 GB） |

## 目录结构

```text
.
├── Dockerfile         镜像定义（apt 源已换 USTC，pip 走 USTC v4 地址）
├── .dockerignore      白名单，只放构建必需的 5 个文件
├── requirements.txt   torch 钉死 +cpu 版本，避免拉进 2.5G CUDA 依赖
├── entrypoint.sh      无参数→WebUI；带参数→转录 CLI
├── transcribe.py      转录主程序（解码 / 推理 / 组装 / 写出）
├── webui.py           FastAPI 单页：上传、进度、结果、下载
└── run_webui.sh       首次创建容器的包装（已存在时只做 docker start + 健康检查）
```

## 依赖与许可

本仓库代码采用 **MIT**（见 `LICENSE`）。以下第三方组件有各自的许可，**均不随本仓库分发**：

| 组件 | 用途 | 许可 |
|------|------|------|
| [FunASR](https://github.com/modelscope/FunASR) | 推理框架 | Apache-2.0 |
| SenseVoice-Small / fsmn-vad / cam++ | 模型权重 | 见 ModelScope 模型页 |
| PyTorch | 运行时 | BSD-3-Clause |
| ffmpeg | 音频解码 | LGPL |

请自行从 ModelScope 下载模型，并遵守对应许可。

## 隐私

转录产物和上传的音频**不会入库、不会进镜像**（`.dockerignore` 白名单 + `.gitignore` 双重排除）。模型与推理全部在本机完成，无任何联网上报。