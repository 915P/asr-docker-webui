# 同济镜像站前缀。换网络环境时可用:
#   docker build --build-arg BASE_IMAGE=python:3.12-slim -t asr:latest .
ARG BASE_IMAGE=docker.tongji.edu.cn/library/python:3.12-slim
FROM ${BASE_IMAGE}

# Debian 官方源在国内直连极慢（实测 5~8 秒/包），换 USTC 镜像
# trixie 用 deb822 格式，配置在 sources.list.d/debian.sources
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] && sed -i \
            -e 's|http://deb.debian.org|http://mirrors.ustc.edu.cn|g' \
            -e 's|http://security.debian.org|http://mirrors.ustc.edu.cn|g' \
            -e 's|https://deb.debian.org|https://mirrors.ustc.edu.cn|g' \
            -e 's|https://security.debian.org|https://mirrors.ustc.edu.cn|g' \
            "$f"; \
    done; \
    apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://mirrors.ustc.edu.cn/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.ustc.edu.cn \
    ASR_HOME=/app \
    ASR_SCRIPT=/app/transcribe.py \
    ASR_PYTHON=python \
    ASR_JOBS=/home/asr/jobs \
    ASR_UI_HOST=0.0.0.0 \
    ASR_UI_PORT=7870 \
    MODELSCOPE_CACHE=/home/asr/.local/share/asr/modelscope

WORKDIR /app

# ---- 依赖层：只在 requirements.txt 变动时重建 ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 运行用户 uid=1000，与常见 Linux 桌面账号一致 -> 挂载卷写出的文件属主正确
RUN useradd -m -u 1000 asr
USER asr
ENV HOME=/home/asr

# ---- 模型权重层：从宿主本地复制，不走网络 ----
# 构建时需要:
#   docker build --build-context models=$HOME/.local/share/asr/modelscope -t asr:latest .
# 宿主 ~/.local/share/asr/modelscope/models/iic--* (929MB) 直接进镜像，
# 省掉 930MB 下载。MODELSCOPE_CACHE 指向 /home/asr/.local/share/asr/modelscope。
COPY --from=models --chown=1000:1000 . /home/asr/.local/share/asr/modelscope/

# ---- 代码层放最后：改动只重建这一层 ----
COPY --chown=1000:1000 transcribe.py webui.py entrypoint.sh ./
RUN chmod +x entrypoint.sh && mkdir -p "$ASR_JOBS"

EXPOSE 7870
# 默认起 WebUI；`docker exec asr-webui python transcribe.py <音频>` 可当 CLI 用
ENTRYPOINT ["/app/entrypoint.sh"]