#!/bin/bash
# ASR 转录引擎 —— MinerU 同款管理方式
#
#   bash ~/sensevoice-test/run_webui.sh   启动（容器不存在则创建，存在则 start）
#   docker stop  asr-webui                停止
#   docker start asr-webui                启动
#   docker logs -f asr-webui              看日志
#
# 首次使用前需要先构建镜像（模型从宿主本地复制进镜像，不走网络）:
#   docker build --build-context models=$HOME/.local/share/asr/modelscope \
#       -t asr:latest ~/sensevoice-test
set -euo pipefail

NAME=asr-webui
IMAGE=asr:latest
PORT=7870

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "镜像 $IMAGE 不存在，开始构建..."
    docker build \
        --build-context models="$HOME/.local/share/asr/modelscope" \
        -t "$IMAGE" "$(dirname "$0")"
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker start "$NAME" >/dev/null
    echo "已启动容器 $NAME"
else
    # 任务目录挂 tmpfs：只占内存不占磁盘，容器 stop / 关机时由内核自动清空，
    # 不需要任何清理任务。mode=1777 必须给（tmpfs 默认 root 属主，uid=1000 无法写入）。
    docker run -d --name "$NAME" \
        --restart unless-stopped \
        -p "$PORT:$PORT" \
        --tmpfs /home/asr/jobs:rw,size=4g,mode=1777 \
        "$IMAGE" >/dev/null
    echo "已创建并启动容器 $NAME"
fi

sleep 3
printf "健康检查: "
curl -s -m 5 "http://127.0.0.1:$PORT/api/health" || printf "无响应（首次启动可能需多等几秒）"
printf "\n  http://127.0.0.1:$PORT\n"