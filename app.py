import os, re, json, uuid, time, shutil, subprocess, threading
from pathlib import Path
import requests
from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path("/tmp/jobs"); BASE.mkdir(exist_ok=True)
JOBS, LOCK = {}, threading.Lock()
CODE = os.getenv("ACCESS_CODE", "")
GROQ = os.getenv("GROQ_API_KEY", "")
GKEYS = [k.strip() for k in GROQ.split(",") if k.strip()]
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
    raise RuntimeError("Is version mein link download band hai. Video file upload karo.")

def transcribe(j, d, src, roman=False):
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
        if roman: data.append(("prompt", "Aaj hum baat karenge, ye video bahut mast hai, dekho kya hota hai."))
        for t in range(8):
            with open(p, "rb") as f:
                r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                  headers={"Authorization": f"Bearer {GKEYS[t % len(GKEYS)]}"}, data=data, files={"file": f}, timeout=300)
            if r.status_code != 429: break
            time.sleep(min(float(r.headers.get("retry-after", 30)) + 2, 120))
        if r.status_code != 200: raise RuntimeError("Transcript fail: " + r.text[:200])
        o, js = i * 600, r.json()
        words += [{"w": w["word"], "s": w["start"] + o, "e": w["end"] + o} for w in js.get("words", [])]
        segs += [{"s": s["start"] + o, "e": s["end"] + o, "t": s["text"].strip()} for s in js.get("segments", [])]
    if not segs: return [], []
    return words, segs

def ask_llm(prompt):
    for name, url, keys, model in PROVIDERS:
        for key in [k.strip() for k in keys.split(",") if k.strip()]:
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

def pick(j, segs, n, mn, mx, style="viral", custom=""):
    extra = f"The user also asked for: {custom}. Prefer moments that match this. " if custom else ""
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
                      f"Choose up to 4 self-contained moments of {mn}-{mx} seconds {STY[style]}. {extra}"
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

def energy_pick(j, d, src, n, mn, mx):
    upd(j, "Awaaz aur scene cuts analyze ho rahe hain", 20)
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-af", "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=rms.txt", "-f", "null", "-"], cwd=d)
    upd(j, "Scene cuts dhoondh raha hai", 35)
    run(["ffmpeg", "-y", "-i", str(src), "-an", "-vf", "fps=4,scale=320:-2,select='gt(scene,0.3)',metadata=print:file=scenes.txt",
         "-f", "null", "-"], cwd=d)
    lv = {}
    for t, v in re.findall(r"pts_time:([\d.]+)\s*\nlavfi\.astats\.Overall\.RMS_level=(\S+)", (d / "rms.txt").read_text()):
        try: lv[int(float(t))] = max(float(v), -60)
        except ValueError: lv[int(float(t))] = -60
    cuts = sorted(float(t) for t in re.findall(r"pts_time:([\d.]+)", (d / "scenes.txt").read_text()))
    if not lv: raise RuntimeError("Video mein audio nahi mili")
    total, L = max(lv) + 1, (mn + mx) // 2
    if total < mn: raise RuntimeError("Video clip length se chhoti hai")
    cands = []
    for s0 in range(0, max(1, int(total - L) + 1), 2):
        w = sorted(((lv.get(x, -60) + 60) / 60 for x in range(s0, s0 + L)), reverse=True)
        loud = sum(w[:max(1, len(w) * 3 // 10)]) / max(1, len(w) * 3 // 10)
        cd = sum(1 for c in cuts if s0 <= c < s0 + L) / L
        near = [c for c in cuts if abs(c - s0) <= 3]
        st = min(near, key=lambda c: abs(c - s0)) if near else s0
        cands.append({"start": st, "end": min(st + L, total), "score": 10 * (0.6 * loud + 0.4 * min(cd / 0.3, 1))})
    cands.sort(key=lambda c: -c["score"])
    out = []
    for c in cands:
        if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in out): out.append(c)
        if len(out) >= n: break
    out.sort(key=lambda c: c["start"])
    for k, c in enumerate(out): c["title"] = f"Moment {k + 1}"
    return out

STY = {"viral": "with a strong hook, emotion or surprise", "funny": "that are the funniest or most entertaining",
       "insight": "with the most useful, insightful or educational point", "news": "with the most newsworthy facts or announcements"}

HL = {"gold": "&H00D7FF&", "green": "&H00FF00&", "red": "&H3C3CFF&", "cyan": "&HFFFF00&"}

def make_ass(words, c, path, o):
    W, H, cap = o["W"], o["H"], o["cap"]
    start, end = c["start"], c["end"]
    fs = int(min(W, H) * 0.087 * {"s": 0.8, "m": 1, "l": 1.2}[o["size"]])
    al, mv = {"bottom": (2, int(H * (0.26 if o["aspect"] == "9:16" else 0.1))), "center": (5, 0), "top": (8, int(H * 0.2))}[o["pos"]]
    bs, ol, oc = (3, 10, "&HB0000000") if cap == "box" else (1, 5, "&H00000000")
    hl = HL[o["hl"]]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: D,Noto Sans,{fs},&H00FFFFFF,&H00FFFFFF,{oc},&H80000000,1,0,0,0,100,100,0,0,{bs},{ol},2,{al},40,40,{mv},1\n"
            f"Style: H,Noto Sans,{int(min(W, H) * 0.074)},&H00000000,&H00000000,&H0000D7FF,&H00000000,1,0,0,0,100,100,0,0,3,14,0,8,50,50,{int(H * 0.07)},1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")

    def clean(x):
        t = re.sub(r"[{}\\]", "", re.sub(r"[,.;:।]+$", "", x["w"].strip()))
        return t.upper() if o["letters"] == "upper" else t

    ev = []
    ht = o["hooktext"] or c["title"]
    if o["hook"] and ht:
        t = re.sub(r"[{}\\\n]", " ", ht)
        ev.append(f"Dialogue: 1,{ts(0)},{ts(min(3.5, end - start))},H,,0,0,0,,{{\\fscx88\\fscy88\\t(0,120,\\fscx100\\fscy100)}}{t}")
    ws = [w for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05] if cap != "none" else []
    k = o["wpl"]
    pop = "{\\fscx88\\fscy88\\t(0,80,\\fscx100\\fscy100)}"
    for i in range(0, len(ws), k):
        g = ws[i:i + k]
        if cap == "white":
            ev.append(f"Dialogue: 0,{ts(max(g[0]['s'] - start, 0))},{ts(g[-1]['e'] - start)},D,,0,0,0,,{' '.join(clean(x) for x in g)}")
            continue
        for j, w in enumerate(g):
            t0 = max(w["s"] - start, 0)
            t1 = (g[j + 1]["s"] if j + 1 < len(g) else w["e"]) - start
            txt = pop + " ".join(("{\\c" + hl + "}" + clean(x) + "{\\c&HFFFFFF&}") if m == j else clean(x) for m, x in enumerate(g))
            ev.append(f"Dialogue: 0,{ts(t0)},{ts(max(t1, t0 + 0.05))},D,,0,0,0,,{txt}")
    Path(path).write_text(head + "\n".join(ev), encoding="utf-8")

def render(d, src, c, words, i, o):
    W, H = o["W"], o["H"]
    make_ass(words, c, d / f"c{i}.ass", o)
    if o["layout"] == "crop":
        lay = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        lay = (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:5[bg];"
               f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    z = f",scale=w='trunc({W}*(1+0.07*abs(sin(t*0.9)))/2)*2':h=-2:eval=frame,crop={W}:{H}" if o["zoom"] else ""
    dur = c["end"] - c["start"]
    game = o["kind"] == "game"
    g = f",eq=contrast=1.06:saturation=1.25,unsharp=5:5:0.5,fade=t=in:st=0:d=0.25,fade=t=out:st={dur - 0.3:.2f}:d=0.3" if game else ""
    af = "loudnorm=I=-16:TP=-1.5" + (f",afade=t=in:st=0:d=0.2,afade=t=out:st={dur - 0.3:.2f}:d=0.3" if game else "")
    fc = f"[0:v]{lay}{z}{g},ass=c{i}.ass" + ("[v0]" if o["bar"] else "[v]")
    if o["bar"]:
        fc += (f";color=c=0xFFD700:s={W}x{max(6, H // 120)}:r=30:d={dur:.2f}[bar];"
               f"[v0][bar]overlay=x='-W+W*t/{dur:.2f}':y=H/40:shortest=1[v]")
    run(["ffmpeg", "-y", "-ss", str(c["start"]), "-i", str(src), "-t", f"{dur:.2f}", "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a:0?", "-af", af, "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", f"clip{i+1}.mp4"], cwd=d)

def meta(chosen, segs, topic):
    items = []
    for i, c in enumerate(chosen):
        txt = " ".join(s["t"] for s in segs if s["s"] >= c["start"] and s["e"] <= c["end"])[:400] if segs else ""
        items.append(f"{i + 1}. {txt or topic}")
    out = ask_llm("For each numbered short video clip below write a catchy title (max 60 chars) and 5 hashtags, in the same language as the clip text. "
                  'Return ONLY a JSON list in the same order: [{"title":"...","tags":"#a #b"}]\n\nTopic: ' + topic + "\n" + "\n".join(items))
    try:
        arr = json.loads(re.search(r"\[.*\]", out, re.S).group(0))
        for c, a in zip(chosen, arr): c["meta"] = f'{a.get("title", "")}\n{a.get("tags", "")}'.strip()
    except Exception:
        pass

def work(j, o):
    d = BASE / j
    with LOCK:
        try:
            upd(j, "Shuru ho raha hai", 2)
            src = next(d.glob("src.*"))
            segs = []
            if o["kind"] == "game":
                words, chosen = [], energy_pick(j, d, src, o["n"], o["mn"], o["mx"])
            else:
                words, segs = transcribe(j, d, src, o["roman"])
                chosen = pick(j, segs, o["n"], o["mn"], o["mx"], o["style"], o["custom"]) if segs else energy_pick(j, d, src, o["n"], o["mn"], o["mx"])
            upd(j, "Titles aur hashtags bana raha hai", 52)
            meta(chosen, segs, o["hooktext"] or o["custom"] or "gaming video")
            out = []
            for i, c in enumerate(chosen):
                upd(j, f"Clip {i+1}/{len(chosen)} edit ho raha hai", 55 + 43 * i // len(chosen))
                render(d, src, c, words, i, o)
                out.append({"url": f"/files/{j}/clip{i+1}.mp4", "title": c["title"], "score": round(c["score"], 1),
                            "start": int(c["start"]), "end": int(c["end"]), "meta": c.get("meta", "")})
            for f in d.glob("*"):
                if f.suffix in (".mp3", ".ass", ".txt") or f.name.startswith("src"): f.unlink()
            JOBS[j].update(status="done", msg="Clips ready", pct=100, clips=out)
        except Exception as e:
            JOBS[j].update(status="error", msg=str(e)[:300])

UP = {}
def check(code):
    if CODE and code != CODE: raise HTTPException(403, "Access code galat hai")

@app.post("/api/upload/init")
def up_init(name: str = Form(""), code: str = Form("")):
    check(code)
    for p in BASE.glob("*"):
        if time.time() - p.stat().st_mtime > 3 * 3600: shutil.rmtree(p, ignore_errors=True)
    j = uuid.uuid4().hex[:10]; (BASE / j).mkdir()
    ext = Path(name).suffix.lower()
    ext = ext if ext in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi") else ".mp4"
    UP[j] = {"ext": ext, "n": 0}
    (BASE / j / f"src{ext}").write_bytes(b"")
    return {"id": j}

@app.post("/api/upload/{j}")
async def up_chunk(j: str, request: Request, i: int):
    u = UP.get(j)
    if not u: raise HTTPException(404, "Upload nahi mila")
    if i < u["n"]: return {"ok": True}
    if i > u["n"]: raise HTTPException(409, "Chunk order galat")
    with open(BASE / j / f"src{u['ext']}", "ab") as f: f.write(await request.body())
    u["n"] += 1
    return {"ok": True}

@app.post("/api/jobs")
def create(upload_id: str = Form(...), n: int = Form(5), minlen: int = Form(20), maxlen: int = Form(45),
           style: str = Form("viral"), layout: str = Form("blur"), aspect: str = Form("9:16"), res: int = Form(720),
           cap: str = Form("gold"), pos: str = Form("bottom"), wpl: int = Form(3), hook: int = Form(1),
           zoom: int = Form(0), prompt: str = Form(""), kind: str = Form("talk"), hooktext: str = Form(""), letters: str = Form("upper"), hl: str = Form("gold"),
           size: str = Form("m"), script: str = Form("auto"), bar: int = Form(0), code: str = Form("")):
    check(code)
    if upload_id not in UP: raise HTTPException(404, "Pehle video upload karo")
    ar = {"9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "16:9": (16, 9)}
    aspect = aspect if aspect in ar else "9:16"
    res = res if res in (480, 720, 1080) else 720
    ev = lambda x: int(x // 2 * 2)
    W, H = (ev(res * 16 / 9), res) if aspect == "16:9" else (res, ev(res * ar[aspect][1] / ar[aspect][0]))
    o = dict(n=max(1, min(n, 10)), mn=minlen, mx=max(maxlen, minlen + 5), style=style if style in STY else "viral",
             layout="crop" if layout == "crop" else "blur", aspect=aspect, W=W, H=H,
             cap=cap if cap in ("gold", "white", "box", "none") else "gold",
             pos=pos if pos in ("bottom", "center", "top") else "bottom", wpl=max(1, min(wpl, 4)), hook=bool(hooktext.strip()) if kind == "game" else (bool(hook) or bool(hooktext.strip())), zoom=bool(zoom),
             kind="game" if kind == "game" else "talk", letters="normal" if letters == "normal" else "upper",
             hl=hl if hl in HL else "gold", size=size if size in ("s", "m", "l") else "m", roman=(script == "roman"), bar=bool(bar), hooktext=re.sub(r"[\U00010000-\U0010ffff{}\\\n]", " ", hooktext).strip()[:60], custom=" ".join(prompt.split())[:300])
    JOBS[upload_id] = {"status": "running", "msg": "Queue mein hai", "pct": 0, "clips": []}
    threading.Thread(target=work, args=(upload_id, o), daemon=True).start()
    return {"id": upload_id}

@app.get("/api/jobs/{j}")
def status(j: str):
    if j not in JOBS: raise HTTPException(404, "Job nahi mila")
    return JOBS[j]

@app.get("/api/jobs/{j}/zip")
def zipall(j: str):
    import zipfile
    d = BASE / j
    with zipfile.ZipFile(d / "all.zip", "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(d.glob("clip*.mp4")): zf.write(f, f.name)
    return FileResponse(d / "all.zip", filename="clips.zip")

@app.get("/")
def home(): return FileResponse("index.html")

app.mount("/files", StaticFiles(directory=BASE), name="files")
