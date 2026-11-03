# 本地语音转录 — 部署与运维指南

> 引擎以 **Docker 容器**方式运行（镜像 `asr:latest`，容器名 `asr-webui`），`docker start/stop` 即可管理，与常见本地 AI 服务同一套心智模型。
> 模型（**SenseVoice-Small** 识别 + **fsmn-vad** 切段 + **cam++** 说话人分离，纯 CPU）已烤进镜像，不依赖宿主任何文件。
> 实测速度：**2 小时 16 分的录音约 9 分钟转完**（约 21 倍实时）。

---

## 一、启动引擎

打开终端，执行：

```bash
docker start asr-webui
```

等待约 5~10 秒，然后验证：

```bash
curl http://127.0.0.1:7870/api/health
# 返回 {"ok":true,"jobs":0} 即就绪
```

配套的几个命令：

```bash
docker stop  asr-webui     # 停止
docker logs -f asr-webui   # 看日志
docker ps                  # 确认状态
```

> ⚠️ 端口是 **7870**（10 的倍数）——特意避开 7860，那是不少本地 WebUI 的默认端口，这样两个服务可以同时开着。

**本地只留 Docker**：不留源码副本，跑起来只需要镜像和容器。要改代码或重建镜像时从 GitHub 取回，见第五节。

## 二、打开 WebUI

浏览器访问：

```
http://127.0.0.1:7870
```

**基本用法**：
1. 拖入音频 / 视频（mp3、wav、m4a、mp4、flac 都行）
2. 勾选 **说话人分离**，选语种（中文 / 自动 / 英文）
3. 可选：填「只转前 N 秒」先试跑一段看质量
4. 点 **开始转录** → 看进度条和实时日志
5. 完成后在线看全文，下载 `.txt` / `.srt` / `.json`

> 进度条按「音频时长 × 实测 RTF」推算（整文件推理只有 1 个 tqdm 迭代，拿不到真实百分比），所以是预估值，倒计时比百分比更准。

## 三、命令行用法

不需要界面时，用一次性容器处理宿主文件（跑完自动删除，不留垃圾）：

```bash
docker run --rm -v 音频所在目录:/data asr:latest /data/录音.mp3
# 输出写回同一目录：录音.txt / .json / .srt
```

完整示例：

```bash
cd ~/Music
docker run --rm -v "$PWD":/data asr:latest /data/录音.mp3            # 全量
docker run --rm -v "$PWD":/data asr:latest /data/录音.mp3 --limit 120   # 试跑前 120 秒
docker run --rm -v "$PWD":/data asr:latest /data/录音.mp3 --no-spk       # 关说话人分离
docker run --rm -v "$PWD":/data asr:latest /data/录音.mp3 --ncpu 4       # 少占 CPU
docker run --rm -v "$PWD":/data asr:latest /data/录音.mp3 --language auto
```

> 产出文件属主是 **uid 1000**，正好等于常见桌面账号，**不用 sudo chown**。

**输出格式**：

| 文件 | 内容 |
|------|------|
| `.txt` | `[00:12:34 - 00:12:40] [说话人0] 文本` |
| `.json` | 结构化：总时长、覆盖率、说话人列表、逐段时间轴 |
| `.srt` | 字幕，可直接进剪辑软件 / 播放器 |

**速度参考**（`ncpu=10`）：

| 音频时长 | 转录耗时 |
|---------|---------|
| 60 秒 | 约 7 秒 |
| 10 分钟 | 约 47 秒 |
| 2 小时 16 分 | 约 8 分 43 秒 |

## 四、停止引擎

```bash
docker stop asr-webui
```

容器设了 `--restart unless-stopped`，**开机后 Docker 会自动把它带起来**。手动 `docker stop` 过的话，重启后仍保持停止。

**停止即清理**：任务目录挂在 tmpfs 上（内存），`docker stop`、重启、**整机关机时自动清空**，磁盘上不留任何上传文件——所以停止前记得把结果下载走。

## 五、如果容器 / 镜像没了

**0. 先取回源码**（本地不留副本，仓库在 GitHub）：

```bash
git clone git@github.com:915P/asr-docker-webui.git
cd asr-docker-webui
```

**1. 重建镜像**（模型从宿主本地复制，不走网络，约 10 分钟）：

```bash
docker build \
    --build-context models=$HOME/.local/share/asr/modelscope \
    -t asr:latest .
```

**2. 重新创建容器**（已有镜像时直接跑）：

```bash
docker run -d --name asr-webui --restart unless-stopped \
  -p 7870:7870 \
  --tmpfs /home/asr/jobs:rw,size=4g,mode=1777 \
  asr:latest
```

> **`--tmpfs` 必须带**：任务目录挂内存，`docker stop`、重启、**整机关机时由内核自动清空**——不需要任何清理任务，容器可写层也不会膨胀（实测不加它，3 次上传就把可写层撑到 789 MB）。
> `mode=1777` 不能省：tmpfs 默认 root 属主，容器以 uid 1000 运行，漏了会 `PermissionError`。
> `size=4g` 是内存上限保护。**先在 WebUI 里下载结果，再 `docker stop`**，否则任务一起消失。

或者用仓库里的包装脚本（效果相同，多做一步健康检查）：

```bash
bash ./run_webui.sh
```

> ⚠️ **`--build-context` 这个参数不能省**。`COPY` 只能读 build context 内的文件，而 `.dockerignore` 是白名单（只放构建必需的 5 个文件，防止无关内容混入），模型在 `~/.local/share/asr/` 属于 context 之外。命名 build context 绕开了这个边界，且**不受 `.dockerignore` 约束**。
>
> 模型完整 ID 必须是：`iic/SenseVoiceSmall`、`iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`、`iic/speech_campplus_sv_zh-cn_16k-common`。写 `iic/fsmn-vad` 这类别名会报 `record not found`（别名只在 funasr 代码内部有效）。

**3. 全新机器**（连模型都没有）：

```bash
export MODELSCOPE_CACHE=~/.local/share/asr/modelscope
modelscope download --model iic/SenseVoiceSmall
modelscope download --model iic/speech_fsmn_vad_zh-cn-16k-common-pytorch
modelscope download --model iic/speech_campplus_sv_zh-cn_16k-common
```

> 为什么放 `~/.local/share/asr/` 而不是 `~/.cache/`：`~/.cache` 归类为**可丢弃缓存**，任何「清理缓存」工具都可能把它扫掉，而这 929MB 是核心资产。

## 六、排错

```bash
docker ps                                  # 确认容器在跑
docker logs --tail 30 asr-webui            # 看日志
curl http://127.0.0.1:7870/api/health      # 确认存活
ss -tln | grep 7870                        # 确认端口在听
```

| 现象 | 处理 |
|------|------|
| 7870 打不开 | `docker ps` 看容器是否在跑，不在则 `docker start asr-webui` |
| 提示端口被占用 | **7860 起的常见端口常被其它本地服务占用**；用 `ss -tln \| grep 7870` 确认谁占了 7870 |
| 启动即退出 | `docker logs asr-webui` 看最后几行 |
| 镜像不存在 | 见「五、1」，**别忘了 `--build-context models=...`** |
| 容器内重新下载模型 | 说明 `MODELSCOPE_CACHE` 指向不对，应为 `/home/asr/.local/share/asr/modelscope` |
| 转录结果整篇没有标点 | 少了 `use_itn=True`（funasr 官方 CLI 就有这毛病），别改 `transcribe.py` 里的这个参数 |
| 想确认没漏内容 | 看日志里的 `coverage=`（VAD 覆盖率），正常在 **85%~95%**；低于 70% 说明噪声大或音量过低 |
| 转录明显变慢 | `ncpu` 默认应为 **10**，设成 14 反而慢 2~3 倍（异构核拖累） |

## 七、文件位置速查

| 内容 | 路径 |
|------|------|
| **WebUI 地址** | `http://127.0.0.1:7870` |
| 镜像 / 容器 | `asr:latest` / `asr-webui` |
| **源码仓库** | https://github.com/915P/asr-docker-webui （public / `master`） |
| 源码（本地） | **不留副本**——本地只保留镜像与容器，要用时 `git clone git@github.com:915P/asr-docker-webui.git` |
| 启动脚本 | 仓库内 `run_webui.sh`（取回源码后 `bash ./run_webui.sh`） |
| 模型（宿主，929 MB） | `~/.local/share/asr/modelscope/models/iic--*` ← 构建时 COPY 进镜像 |
| 模型（容器内） | `/home/asr/.local/share/asr/modelscope/` |
| WebUI 上传的任务 | 容器内 `/home/asr/jobs/` |
| 归档的转录结果 | `~/.local/share/asr/outputs/`（`transcript_spk.txt` / `.srt` / `.json` 等） |
| 归档的 WebUI 历史任务 | `~/.local/share/asr/jobs/`（上传的音频 + 结果） |

### 体积口径（Docker 自己给了三个数）

| 口径 | 值 | 含义 |
|------|-----|------|
| `docker history` 层加总 / 容器 virtual | **3.47 GB** | 实际被使用的镜像内容 |
| `docker inspect .Size` / `docker images` | 5.11 GB | containerd 存储层的记账值 |

层构成：基础镜像+ffmpeg **457MB**、pip 环境 **1.9GB**（torch 809M + llvmlite 173M + transformers 120M + scipy 111M）、模型 **973MB**。

> 构建慢的两个原因（已处理）：
> - **apt**：Debian 官方源直连 5~8 秒/包 → Dockerfile 里换成 `mirrors.ustc.edu.cn`，38 秒装完 205 个包（457MB）
> - **pip**：已设为 USTC `https://mirrors.ustc.edu.cn/pypi/simple/`
>
> ⚠️ pip **必须用 v4 地址**。宿主 `~/.config/pip/pip.conf` 里配的是 `ipv6.mirrors.ustc.edu.cn`，但**容器没有 IPv6**，照搬会 `URLError: Network is unreachable`。实测对照：
> ```
> FAIL  https://ipv6.mirrors.ustc.edu.cn/pypi/simple/
> OK    https://mirrors.ustc.edu.cn/pypi/simple/
> ```
>
> ⚠️ **别跑 `docker image prune -a`**：它按"无容器引用"判定，会把**所有**只在一次性 `docker run` 里用过的镜像一起删掉（比如你偶尔手动跑一次的推理服务镜像）。要清理只按具体 tag 点名。
>
> 关于**开源**：仓库已发布 → **https://github.com/915P/asr-docker-webui** （public，MIT）。
> 已验证：从 GitHub 全新 `git clone` 得到 10 个文件、构建所需的 7 个文件齐全、与原目录清单完全一致。
> 隐私：硬编码的个人路径已改成相对路径，转录产物和上传音频被 `.gitignore` 挡住，模型不随仓库分发（README 列了第三方许可）。
> 本机只保留：Docker 镜像 + 容器 + `~/.local/share/asr/` 下的模型与归档，**源码不留副本**，要改就 `git clone`。