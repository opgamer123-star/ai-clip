import os, re, json, uuid, time, shutil, subprocess, threading
from pathlib import Path
import requests
from fastapi import FastAPI, Form, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path("/tmp/jobs"); BASE.mkdir(exist_ok=True)
JOBS, LOCK = {}, threading.Lock()
CODE = os.getenv("ACCESS_CODE", "")
GROQ = os.getenv("GROQ_API_KEY", "")
PROVIDERS = [
    ("groq", "https://api.groq.com/openai/v1/chat/completions", GROQ, os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", os.getenv("GEMINI_API_KEY", ""), os.getenv("GEMINI_MODEL", "gemini-2.0-flash")),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", os.getenv("OPENROUTER_API_KEY", ""), os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
]
app = FastAPI()

def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode: raise RuntimeError((r.stderr or "")[-300:])

def upd(j, msg, pct): JOBS[j].update(msg=msg, pct=pct)

def get_video(j, d, url, uploaded):
    if uploaded: return uploaded
    upd(j, "Link se video download ho raha hai", 5)
    try:
        run(["yt-dlp", "-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4",
             "--no-playlist", "-o", str(d / "src.%(ext)s"), url])
    except Exception:
        raise RuntimeError("Link download nahi hua (server ko block kiya gaya). Video file upload karo.")
    return next(d.glob("src.*"))

def transcribe(j, d, src):
    if not GROQ: raise RuntimeError("GROQ_API_KEY set nahi hai")
    upd(j, "Audio nikal raha hai", 15)
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
         "-f", "segment", "-segment_time", "600", "-reset_timestamps", "1", "a%03d.mp3"], cwd=d)
    words, segs = [], []
    parts = sorted(d.glob("a*.mp3"))
    for i, p in enumerate(parts):
        upd(j, f"Transcript ban raha hai ({i+1}/{len(parts)})", 20 + 25 * i // len(parts))
        data = [("model", "whisper-large-v3-turbo"), ("response_format", "verbose_json"),
                ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
        for t in range(4):
            with open(p, "rb") as f:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {GROQ}"}, data=data, files={"file": f}, timeout=300)
            if r.status_code != 429: break
            time.sleep(25 * (t + 1))
        if r.status_code != 200: raise RuntimeError("Transcript fail: " + r.text[:200])
        o, js = i * 600, r.json()
        words += [{"w": w["word"], "s": w["start"] + o, "e": w["end"] + o} for w in js.get("words", [])]
        segs += [{"s": s["start"] + o, "e": s["end"] + o, "t": s["text"].strip()} for s in js.get("segments", [])]
    if not segs: raise RuntimeError("Video mein bolne wali awaaz nahi mili")
    return words, segs

def ask_llm(prompt):
    for name, url, key, model in PROVIDERS:
        if not key: continue
        try:
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=90,
                              json={"model": model, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
    return None

def snap(c, segs, mn, mx):
    s0 = min(segs, key=lambda x: abs(x["s"] - c["start"]))["s"]
    ends = [x["e"] for x in segs if mn * 0.8 <= x["e"] - s0 <= mx]
    e0 = min(ends, key=lambda e: abs(e - c["end"])) if ends else s0 + min(mx, 45)
    return {**c, "start": s0, "end": e0}

def pick(j, segs, n, mn, mx):
    upd(j, "Best moments dhoondh raha hai", 50)
    cands, win, size, last = [], [], 0, 0
    wins = []
    for s in segs:
        win.append(s); size += len(s["t"])
        if s["e"] - win[0]["s"] > 600: wins.append(win); win = []
    if win: wins.append(win)
    for w in wins:
        txt = "\n".join(f"[{int(s['s'])}] {s['t']}" for s in w)
        out = ask_llm(f"You pick viral short-video clips from a transcript. Each line starts with its start time in seconds. "
                      f"Choose up to 4 self-contained moments of {mn}-{mx} seconds with a strong hook, emotion, surprise or news value. "
                      f'Return ONLY a JSON list: [{{"start":sec,"end":sec,"title":"short title in the transcript language","score":1-10}}]\n\n{txt}')
        m = re.search(r"\[.*\]", out or "", re.S)
        try:
            for c in json.loads(m.group(0)):
                cands.append(snap({"start": float(c["start"]), "end": float(c["end"]),
                                   "title": str(c.get("title", ""))[:80], "score": float(c.get("score", 5))}, segs, mn, mx))
        except Exception:  # LLM fail: speech-density fallback
            for i in range(0, len(w), 6):
                g = w[i:i + 6]
                if not g: continue
                c = snap({"start": g[0]["s"], "end": g[0]["s"] + (mn + mx) / 2, "title": g[0]["t"][:60],
                          "score": sum(len(x["t"]) for x in g) / max(1, g[-1]["e"] - g[0]["s"]) / 6}, segs, mn, mx)
                cands.append(c)
    cands = [c for c in cands if mn * 0.7 <= c["end"] - c["start"] <= mx + 5]
    cands.sort(key=lambda c: -c["score"])
    chosen = []
    for c in cands:
        if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in chosen): chosen.append(c)
        if len(chosen) >= n: break
    if not chosen: raise RuntimeError("Koi clip nahi bana, video chhoti ya bina awaaz ki ho sakti hai")
    return sorted(chosen, key=lambda c: c["start"])

def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def make_ass(words, start, end, path):
    ws = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05]
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 720\nPlayResY: 1280\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            "Style: D,Noto Sans,62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,5,2,2,40,40,330,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    ev = []
    for i in range(0, len(ws), 3):
        g = ws[i:i + 3]
        for j, w in enumerate(g):
            t0 = max(w["s"] - start, 0)
            t1 = (g[j + 1]["s"] if j + 1 < len(g) else w["e"]) - start
            txt = " ".join(("{\\c&H00D7FF&}" + x["w"].strip() + "{\\c&HFFFFFF&}") if k == j else x["w"].strip() for k, x in enumerate(g))
            ev.append(f"Dialogue: 0,{ts(t0)},{ts(max(t1, t0 + 0.05))},D,,0,0,0,,{txt}")
    Path(path).write_text(head + "\n".join(ev), encoding="utf-8")

def render(d, src, c, words, i, mode):
    make_ass(words, c["start"], c["end"], d / f"c{i}.ass")
    a = f"ass=c{i}.ass"
    vf = (f"crop=ih*9/16:ih,scale=720:1280,{a}" if mode == "crop" else
          "split[a][b];[a]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,boxblur=20:5[bg];"
          f"[b]scale=720:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,{a}")
    run(["ffmpeg", "-y", "-ss", str(c["start"]), "-i", str(src), "-t", str(c["end"] - c["start"]), "-vf", vf,
         "-af", "loudnorm=I=-16:TP=-1.5", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", f"clip{i+1}.mp4"], cwd=d)

def work(j, url, uploaded, n, mn, mx, mode):
    d = BASE / j
    with LOCK:
        try:
            upd(j, "Shuru ho raha hai", 2)
            src = get_video(j, d, url, uploaded)
            words, segs = transcribe(j, d, src)
            chosen = pick(j, segs, n, mn, mx)
            out = []
            for i, c in enumerate(chosen):
                upd(j, f"Clip {i+1}/{len(chosen)} edit ho raha hai", 55 + 43 * i // len(chosen))
                render(d, src, c, words, i, mode)
                out.append({"url": f"/files/{j}/clip{i+1}.mp4", "title": c["title"], "score": round(c["score"], 1),
                            "start": int(c["start"]), "end": int(c["end"])})
            for f in d.glob("*"):
                if f.suffix in (".mp3", ".ass") or f.name.startswith("src"): f.unlink()
            JOBS[j].update(status="done", msg="Clips ready", pct=100, clips=out)
        except Exception as e:
            JOBS[j].update(status="error", msg=str(e)[:300])

@app.post("/api/jobs")
async def create(url: str = Form(""), n: int = Form(5), minlen: int = Form(20), maxlen: int = Form(45),
                 mode: str = Form("blur"), code: str = Form(""), file: UploadFile = File(None)):
    if CODE and code != CODE: raise HTTPException(403, "Access code galat hai")
    for p in BASE.glob("*"):  # purane jobs saaf
        if time.time() - p.stat().st_mtime > 3 * 3600: shutil.rmtree(p, ignore_errors=True)
    if not url.strip() and not (file and file.filename): raise HTTPException(400, "Link ya video file do")
    j = uuid.uuid4().hex[:10]; d = BASE / j; d.mkdir()
    uploaded = None
    if file and file.filename:
        uploaded = d / ("src" + Path(file.filename).suffix)
        with open(uploaded, "wb") as f: shutil.copyfileobj(file.file, f)
    JOBS[j] = {"status": "running", "msg": "Queue mein hai", "pct": 0, "clips": []}
    threading.Thread(target=work, args=(j, url.strip(), uploaded, max(1, min(n, 10)), minlen, max(maxlen, minlen + 5), mode), daemon=True).start()
    return {"id": j}

@app.get("/api/jobs/{j}")
def status(j: str):
    if j not in JOBS: raise HTTPException(404, "Job nahi mila")
    return JOBS[j]

@app.get("/")
def home(): return FileResponse("index.html")

app.mount("/files", StaticFiles(directory=BASE), name="files")
