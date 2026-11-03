#!/usr/bin/env python3
"""本地语音转录 WebUI（FastAPI + 单页）。

复用 transcribe.py 作为子进程执行，不改造引擎：捕获其 stdout/stderr 作为日志与
进度来源。tqdm 用 \\r 刷新而不是 \\n，所以必须按两种分隔符切分，否则进度会卡住。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# 路径可配，未设置时默认用本脚本所在目录（即源码/构建上下文目录）
BASE = Path(os.environ.get("ASR_HOME", Path(__file__).resolve().parent))
JOBS_DIR = Path(os.environ.get("ASR_JOBS", str(BASE / "webui_jobs")))
PYTHON = Path(os.environ.get("ASR_PYTHON", sys.executable))
SCRIPT = Path(os.environ.get("ASR_SCRIPT", str(BASE / "transcribe.py")))

# 模型权重放 XDG 持久数据目录，避免被「清理缓存」工具删掉；
# 这里设置后，子进程（transcribe.py）会继承到。
os.environ.setdefault(
    "MODELSCOPE_CACHE", os.path.expanduser("~/.local/share/asr/modelscope"))
MAX_CONCURRENT = 1          # 单并发：线程扫描已证明超订会互相拖累

JOBS: dict = {}
LOCK = threading.Lock()
SLOT = threading.Semaphore(MAX_CONCURRENT)

app = FastAPI(title="本地语音转录")
JOBS_DIR.mkdir(exist_ok=True)

# 启动时清理 7 天前的旧任务，避免 webui_jobs 无限膨胀
_KEEP = 7 * 86400
for _d in JOBS_DIR.iterdir() if JOBS_DIR.is_dir() else []:
    try:
        if _d.is_dir() and time.time() - _d.stat().st_mtime > _KEEP:
            shutil.rmtree(_d, ignore_errors=True)
    except OSError:
        pass

TQDM_PCT = re.compile(rb"(\d{1,3})%")


def _handle_line(job: dict, raw: bytes) -> None:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return
    job["lines"].append(text)

    # 阶段里程碑
    if "decode ->" in text:
        job["pct"] = max(job["pct"], 5)
        job["stage"] = "解码中"
    elif "total duration =" in text:
        m = re.search(r"total duration = ([\d.]+)s", text)
        if m:
            job["audio_s"] = float(m.group(1))
    elif "models loaded" in text:
        job["pct"] = max(job["pct"], 12)
        job["stage"] = "推理中（按实测 RTF 预估）"
        job["inf_start"] = time.time()
    elif "segments, speech=" in text:
        job["pct"] = max(job["pct"], 95)
        job["stage"] = "生成文本"
    elif "wrote " in text:
        job["pct"] = max(job["pct"], 100)
        job["stage"] = "完成"

    # tqdm 百分比：映射到 12%~95% 区间（推理是耗时大头）
    m = TQDM_PCT.search(raw)
    if m:
        v = int(m.group(1))
        if 0 <= v <= 100:
            job["pct"] = max(job["pct"], round(12 + v * 0.83))


# 实测 RTF：含说话人分离的全量跑为 0.061，留 15% 余量
ESTIMATED_RTF = 0.07


def _worker(jid: str) -> None:
    job = JOBS[jid]
    SLOT.acquire()
    with LOCK:
        if job.get("cancelled"):
            SLOT.release()
            job.update(status="error", done=True, pct=0)
            return
        job["status"] = "running"
        job["started"] = time.time()

    env = os.environ.copy()
    env["ASR_UI"] = "1"
    cmd = [str(PYTHON), str(SCRIPT), str(job["input"]), "--out", str(job["out"])]
    if not job["spk"]:
        cmd.append("--no-spk")
    if job["language"]:
        cmd += ["--language", job["language"]]
    if job["limit"]:
        cmd += ["--limit", str(job["limit"])]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        with LOCK:
            job["proc"] = proc
        buf = b""
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for p in parts:
                _handle_line(job, p)
                job["pct"] = job["pct"]
        if buf:
            _handle_line(job, buf)
        rc = proc.wait()
        with LOCK:
            job["exit"] = rc
            job["status"] = "done" if rc == 0 else "error"
            if rc != 0:
                job["pct"] = 0
    except Exception as e:  # noqa: BLE001
        with LOCK:
            job["status"] = "error"
            job["lines"].append(f"异常: {e}")
    finally:
        with LOCK:
            job["done"] = True
            job["elapsed"] = round(time.time() - job["started"], 1)
        SLOT.release()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), spk: str = Form("1"),
                 language: str = Form("zh"), limit: str = Form("")):
    if not file.filename:
        return JSONResponse({"error": "未选择文件"}, status_code=400)
    jid = uuid.uuid4().hex[:8]
    jdir = JOBS_DIR / jid
    jdir.mkdir(parents=True, exist_ok=True)
    stem = Path(file.filename).stem
    dest = jdir / (Path(file.filename).name or "audio")
    with dest.open("wb") as f:
        while True:
            block = await file.read(1 << 20)
            if not block:
                break
            f.write(block)

    try:
        lim = float(limit) if limit.strip() else 0.0
    except ValueError:
        lim = 0.0

    JOBS[jid] = {"id": jid, "name": file.filename, "input": str(dest),
                 "out": str(jdir / stem), "spk": spk == "1",
                 "language": language, "limit": lim,
                 "status": "queued", "pct": 0, "lines": [], "done": False,
                 "exit": None, "started": time.time(), "created": time.time(),
                 "stage": "排队中", "audio_s": 0.0, "inf_start": None}
    threading.Thread(target=_worker, args=(jid,), daemon=True).start()
    return {"id": jid}


@app.get("/api/job/{jid}")
def job(jid: str):
    j = JOBS.get(jid)
    if not j:
        return JSONResponse({"error": "not found"}, status_code=404)
    with LOCK:
        pct, stage = j["pct"], j.get("stage", "—")
        # 整文件输入下 generate() 只有 1 个 tqdm 迭代，拿不到中间百分比，
        # 改用「已知音频时长 × 实测 RTF」推算，让进度条在推理阶段平滑走动。
        if (j["status"] == "running" and j.get("inf_start")
                and j.get("audio_s") and pct < 95):
            est = j["audio_s"] * ESTIMATED_RTF
            frac = (time.time() - j["inf_start"]) / max(est, 1)
            pct = max(pct, min(94, round(12 + frac * 83)))
            if frac < 1:
                stage = (f"推理中 · 预计还需 "
                         f"{max(0, est - (time.time()-j['inf_start'])):.0f}s")
            else:
                stage = "推理中 · 已超过预估，仍在进行"
        return {"id": jid, "status": j["status"], "pct": pct,
                "stage": stage,
                "lines": j["lines"][-400:], "done": j["done"],
                "elapsed": round(time.time() - j["started"], 1)
                if not j["done"] else j.get("elapsed"),
                "exit": j["exit"], "name": j["name"]}


@app.post("/api/cancel/{jid}")
def cancel(jid: str):
    j = JOBS.get(jid)
    if not j:
        return JSONResponse({"error": "not found"}, status_code=404)
    with LOCK:
        j["cancelled"] = True
        p = j.get("proc")
    if p and p.poll() is None:
        p.terminate()
        return {"ok": True, "msg": "已终止"}
    return {"ok": True, "msg": "已标记取消"}


@app.get("/api/result/{jid}")
def result(jid: str):
    j = JOBS.get(jid)
    if not j:
        return JSONResponse({"error": "not found"}, status_code=404)
    p = Path(j["out"] + ".txt")
    if not p.exists():
        return JSONResponse({"error": "尚无结果"}, status_code=404)
    return {"text": p.read_text(encoding="utf-8")}


@app.get("/download/{jid}/{fmt}")
def download(jid: str, fmt: str):
    j = JOBS.get(jid)
    if not j or fmt not in ("txt", "json", "srt"):
        return JSONResponse({"error": "not found"}, status_code=404)
    p = Path(j["out"] + "." + fmt)
    if not p.exists():
        return JSONResponse({"error": "尚无该文件"}, status_code=404)
    base = Path(j["name"]).stem
    return FileResponse(p, filename=f"{base}.{fmt}",
                        media_type="application/octet-stream")


PAGE = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地语音转录</title>
<style>
*{box-sizing:border-box}
body{margin:0;font:15px/1.6 -apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;
background:#0f1115;color:#e6e6e6}
.wrap{max-width:940px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:#8a8f98;font-size:13px;margin-bottom:22px}
.card{background:#171a21;border:1px solid #262a33;border-radius:12px;
padding:18px;margin-bottom:16px}
#drop{border:2px dashed #3a4150;border-radius:10px;padding:26px;text-align:center;
color:#9aa2b1;cursor:pointer;transition:.15s}
#drop.on{border-color:#4f8cff;background:#1a2233;color:#cfe0ff}
label{display:inline-block;font-size:13px;color:#9aa2b1;margin:8px 14px 0 0}
input[type=number],select{background:#0f1115;color:#e6e6e6;border:1px solid #333a47;
border-radius:6px;padding:6px 8px;font-size:14px}
button{background:#2f6fed;color:#fff;border:0;border-radius:8px;padding:10px 20px;
font-size:15px;cursor:pointer}
button:disabled{background:#333a47;color:#8a8f98;cursor:not-allowed}
button.ghost{background:#242932;color:#c6cbd4;padding:7px 13px;font-size:13px}
.bar{height:10px;background:#242932;border-radius:6px;overflow:hidden;margin:10px 0 6px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#2f6fed,#59a5ff);
width:0;transition:width .5s}
.meta{display:flex;justify-content:space-between;font-size:13px;color:#8a8f98}
#log{background:#0b0d11;border:1px solid #22262f;border-radius:8px;padding:10px;
height:230px;overflow:auto;font:12px/1.55 ui-monospace,Menlo,Consolas,monospace;
color:#9fb0c4;white-space:pre-wrap;word-break:break-all;margin-top:12px}
#out{background:#0b0d11;border:1px solid #22262f;border-radius:8px;padding:12px;
height:340px;overflow:auto;font-size:14px;white-space:pre-wrap;word-break:break-word;
display:none}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:14px}
.dl{display:none;gap:8px;flex-wrap:wrap;margin-top:12px}
.dl a{background:#242932;color:#c6cbd4;text-decoration:none;padding:7px 14px;
border-radius:7px;font-size:13px}
.dl a:hover{background:#2f3745}
.err{color:#ff7b72}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:20px;
background:#242932;color:#9aa2b1}
</style></head><body><div class="wrap">
<h1>本地语音转录</h1>
<div class="sub">SenseVoice + cam++ 说话人分离 · CPU · 约 21x 实时速度</div>

<div class="card">
  <div id="drop">点击选择音频/视频文件，或拖拽到这里<br>
    <span style="font-size:12px">mp3 / wav / m4a / mp4 / flac …</span></div>
  <input type="file" id="f" accept="audio/*,video/*" style="display:none">
  <div id="fname" style="font-size:13px;color:#8a8f98;margin-top:8px"></div>

  <div style="margin-top:10px">
    <label><input type="checkbox" id="spk" checked> 说话人分离</label>
    <label>语种
      <select id="lang"><option value="zh" selected>中文</option>
      <option value="auto">自动</option><option value="en">英文</option></select></label>
    <label>只转前 <input type="number" id="limit" min="0" step="10" placeholder="全部"
      style="width:74px"> 秒（试跑）</label>
  </div>

  <div class="row">
    <button id="go">开始转录</button>
    <button id="stop" class="ghost" disabled>取消</button>
    <span class="tag" id="stat">待命</span>
  </div>

  <div class="bar"><i id="bar"></i></div>
  <div class="meta"><span id="stage">—</span><span id="clk">0s</span></div>
  <div id="log"></div>
</div>

<div class="card" id="rescard" style="display:none">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b>转录结果</b>
    <button class="ghost" id="clr">收起</button>
  </div>
  <div id="out"></div>
  <div class="dl" id="dl">
    <a id="d1" download>下载 .txt</a><a id="d2" download>下载 .srt</a>
    <a id="d3" download>下载 .json</a>
  </div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let cur=null, timer=null, t0=0;
const drop=$('drop'), fi=$('f');
drop.onclick=()=>fi.click();
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();
  drop.classList.add('on')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();
  drop.classList.remove('on')}));
drop.addEventListener('drop',ev=>{ if(ev.dataTransfer.files.length){ fi.files=ev.dataTransfer.files; show(); }});
fi.onchange=show;
function show(){ $('fname').textContent = fi.files[0] ? '已选：'+fi.files[0].name : ''; }

$('clr').onclick=()=>{ $('rescard').style.display='none'; };

$('go').onclick=async()=>{
  if(!fi.files[0]) { $('stat').textContent='请先选择文件'; return; }
  const fd=new FormData();
  fd.append('file', fi.files[0]);
  fd.append('spk', $('spk').checked?'1':'0');
  fd.append('language', $('lang').value);
  fd.append('limit', $('limit').value||'');
  $('go').disabled=true; $('stop').disabled=false;
  $('log').textContent=''; $('bar').style.width='0%';
  $('rescard').style.display='none'; $('stage').textContent='上传中…';
  t0=Date.now();
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const j=await r.json();
    if(j.error){ err(j.error); return; }
    cur=j.id; $('stat').textContent='排队中';
    timer=setInterval(poll,700);
  }catch(e){ err('上传失败: '+e); }
};

$('stop').onclick=async()=>{ if(cur) await fetch('/api/cancel/'+cur,{method:'POST'}); };

function err(m){ clearInterval(timer); $('go').disabled=false; $('stop').disabled=true;
  $('stat').innerHTML='<span class="err">'+m+'</span>'; $('stage').textContent='失败'; }

async function poll(){
  if(!cur) return;
  let j; try{ const r=await fetch('/api/job/'+cur); j=await r.json(); }
  catch(e){ return; }
  $('bar').style.width=(j.pct||0)+'%';
  $('clk').textContent=(j.elapsed||0)+'s';
  if(j.stage) $('stage').textContent=j.stage;
  const lg=$('log');
  lg.textContent=j.lines.join('\n'); lg.scrollTop=lg.scrollHeight;
  if(j.status==='running') $('stat').textContent='转录中';
  if(j.done){
    clearInterval(timer); $('go').disabled=false; $('stop').disabled=true;
    if(j.status==='error'){ err('失败（退出码 '+j.exit+'）'); return; }
    $('stat').textContent='完成 · '+j.elapsed+'s';
    $('stage').textContent='完成，读取结果…';
    const rr=await fetch('/api/result/'+cur); const d=await rr.json();
    if(d.error){ err(d.error); return; }
    $('out').textContent=d.text;
    $('rescard').style.display='block';
    $('d1').href='/download/'+cur+'/txt';
    $('d2').href='/download/'+cur+'/srt';
    $('d3').href='/download/'+cur+'/json';
    $('dl').style.display='flex';
    $('stage').textContent='完成';
    $('bar').style.width='100%';
    cur=null;
  }
}
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS)}


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("ASR_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("ASR_UI_PORT", "7870"))
    uvicorn.run(app, host=host, port=port, log_level="warning")