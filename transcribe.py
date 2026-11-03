#!/usr/bin/env python3
"""本地语音转录（含说话人分离）。

管线由 funasr 内部完成：fsmn-vad 分段 -> SenseVoice ASR -> cam++ 说话人聚类。

三个 CLI 给不了、必须自己控制的参数：
  * use_itn=True  —— SenseVoice 输出标点的开关（funasr 1.4.20 CLI 无此参数）
  * ncpu=10       —— 线程扫描 1~14 的最优值，>=12 因异构核慢核拖累而崩塌
  * batch_size_s  —— 2h+ 音频的批量上限，防止内存失控

用法见同目录 `asr` 包装脚本。
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import time

# 模型权重放 XDG 持久数据目录，而不是 ~/.cache —— 后者会被「清理缓存」类工具
# 当垃圾删掉，而这 930MB 是核心资产。modelscope 读 MODELSCOPE_CACHE 环境变量。
os.environ.setdefault(
    "MODELSCOPE_CACHE", os.path.expanduser("~/.local/share/asr/modelscope"))

# funasr/modelscope 启动时刷大量 INFO 日志，只留 WARNING 以上
logging.getLogger().setLevel(logging.WARNING)
for _name in ("modelscope", "modelscope_hub", "funasr",
              "modelscope_hub.download", "modelscope.dataset"):
    logging.getLogger(_name).setLevel(logging.WARNING)
os.environ.setdefault("TQDM_DISABLE", "1")

DEFAULT_NCPU = 10     # 线程扫描实测最优（见 bench_threads.py）
MAX_CHUNK_MS = 30000  # VAD 单段上限


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def fmt_ts(ms) -> str:
    s = int(ms) // 1000
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_srt(ms) -> str:
    ms = int(round(float(ms)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def main() -> None:
    ap = argparse.ArgumentParser(description="本地语音转录（含说话人分离）")
    ap.add_argument("audio", help="音频/视频文件")
    ap.add_argument("--out", default=None, help="输出前缀，默认与输入同名")
    ap.add_argument("--limit", type=float, default=0.0,
                    help="只转前 N 秒（调试用），0=全量")
    ap.add_argument("--language", default="zh", help="zh/en/auto")
    ap.add_argument("--ncpu", type=int, default=DEFAULT_NCPU,
                    help=f"torch 线程数，默认 {DEFAULT_NCPU}（实测最优）")
    ap.add_argument("--no-spk", action="store_true", help="关闭说话人分离")
    ap.add_argument("--no-srt", action="store_true", help="不生成 srt")
    ap.add_argument("--keep-work", action="store_true",
                    help="保留中间 wav（默认用完删除）")
    args = ap.parse_args()

    # WebUI 模式下打开 funasr 的 tqdm，前端可从中截取百分比
    pbar = os.environ.get("ASR_UI") != "1"

    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    if args.out is None:
        args.out = os.path.splitext(os.path.abspath(args.audio))[0]
    workdir = args.out + "_work"
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    # ---------- 1. 解码 ----------
    src = args.audio
    if args.limit > 0:
        clipped = os.path.join(
            workdir, "src_clip" + (os.path.splitext(src)[1] or ".wav"))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src,
                        "-t", str(args.limit), clipped], check=True)
        src = clipped
        log(f"clipped source to first {args.limit:.0f}s")

    wav = os.path.join(workdir, "audio16k.wav")
    log("decode -> 16k mono wav")
    # -err_detect ignore_err：源文件有坏帧时不中断（帧完整性另行验证）
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-err_detect", "ignore_err",
                    "-i", src, "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", wav], check=True)
    src_ms = probe_duration(src) * 1000
    total_ms = probe_duration(wav) * 1000
    log(f"source {src_ms/1000:.1f}s -> decoded {total_ms/1000:.1f}s "
        f"(diff {(src_ms-total_ms)/src_ms*100:.2f}%)")
    log(f"total duration = {total_ms/1000:.1f}s ({total_ms/60000:.1f} min)")

    # ---------- 2. 模型 ----------
    t = time.time()
    kw = dict(model="iic/SenseVoiceSmall", device="cpu", ncpu=args.ncpu,
              vad_model="fsmn-vad",
              vad_kwargs={"max_single_segment_time": MAX_CHUNK_MS},
              disable_update=True, hub="modelscope", disable_pbar=not pbar)
    if not args.no_spk:
        kw["spk_model"] = "cam++"
    asr = AutoModel(**kw)
    log(f"models loaded {time.time()-t:.1f}s (ncpu={args.ncpu}"
        + ("" if args.no_spk else ", +spk") + ")")

    # ---------- 3. 推理 ----------
    t = time.time()
    res = asr.generate(input=wav, cache={}, language=args.language,
                       use_itn=True, batch_size_s=60,
                       output_timestamp=True, disable_pbar=not pbar)
    inf_s = time.time() - t
    r = res[0]
    log(f"inference {inf_s:.1f}s (RTF={inf_s/(total_ms/1000):.3f})")

    # ---------- 4. 组装 ----------
    sentences = r.get("sentence_info") or []
    if not sentences:
        raise SystemExit("模型没有返回 sentence_info")

    seg_out, body_lines, srt_blocks = [], [], []
    speakers = set()
    for s in sentences:
        txt = rich_transcription_postprocess(
            str(s.get("sentence") or s.get("text") or "")).strip()
        if not txt:
            continue
        a = float(s.get("start", 0))
        b = float(s.get("end", a))
        spk = s.get("spk")
        rec = {"start_ms": a, "end_ms": b, "text": txt}
        if spk is not None:
            rec["speaker"] = spk
            speakers.add(spk)
        seg_out.append(rec)
        prefix = f"[说话人{spk}] " if spk is not None else ""
        body_lines.append(f"[{fmt_ts(a)} - {fmt_ts(b)}] {prefix}{txt}")
        srt_blocks.append(f"{len(srt_blocks)+1}\n"
                           f"{fmt_srt(a)} --> {fmt_srt(b)}\n{prefix}{txt}\n")

    speech_ms = sum(x["end_ms"] - x["start_ms"] for x in seg_out)
    coverage = speech_ms / total_ms * 100
    log(f"{len(seg_out)} segments, speech={speech_ms/1000:.0f}s, "
        f"coverage={coverage:.1f}%, speakers={sorted(speakers) or '单人/未检出'}")

    # ---------- 5. 输出 ----------
    with open(args.out + ".txt", "w", encoding="utf-8") as f:
        f.write("\n".join(body_lines) + "\n")
    if not args.no_srt:
        with open(args.out + ".srt", "w", encoding="utf-8") as f:
            f.write("\n".join(srt_blocks))
    with open(args.out + ".json", "w", encoding="utf-8") as f:
        json.dump({"source": args.audio,
                   "duration_s": total_ms / 1000,
                   "speech_s": speech_ms / 1000,
                   "coverage": coverage,
                   "ncpu": args.ncpu,
                   "speakers": sorted(speakers),
                   "inference_seconds": round(inf_s, 1),
                   "segments": seg_out}, f, ensure_ascii=False, indent=1)

    log(f"wrote {args.out}.txt / .json"
        + ("" if args.no_srt else " / .srt"))

    if not args.keep_work:
        shutil.rmtree(workdir, ignore_errors=True)
        log(f"cleaned {workdir}")


if __name__ == "__main__":
    main()