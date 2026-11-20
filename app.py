import os, re, sys, json, base64, queue, uuid, time, shutil, tempfile, subprocess, threading, zipfile
from pathlib import Path
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import requests

class HTTPError(Exception):
    def __init__(self, code, detail): super().__init__(detail); self.code, self.detail = code, detail
HTTPException = HTTPError

_env = Path(__file__).with_name(".env")
if _env.exists():
    for _l in _env.read_text().splitlines():
        if "=" in _l and not _l.strip().startswith("#"):
            _k, _v = _l.split("=", 1); os.environ.setdefault(_k.strip(), _v.strip())
BASE = Path(os.getenv("JOBS_DIR", str(Path(tempfile.gettempdir()) / "jobs"))); BASE.mkdir(parents=True, exist_ok=True)
COOKIES = None
if os.getenv("YT_COOKIES"):
    COOKIES = BASE / "cookies.txt"; COOKIES.write_text(os.environ["YT_COOKIES"])
JOBS, JD, UP, LOCK = {}, {}, {}, threading.Lock()
Q = queue.Queue()
KEEP = float(os.getenv("KEEP_HOURS", "3")) * 3600
def _worker():
    while True:
        fn, args = Q.get()
        try: fn(*args)
        finally: Q.task_done()
threading.Thread(target=_worker, daemon=True).start()
CODE = os.getenv("ACCESS_CODE", "")
GROQ = os.getenv("GROQ_API_KEY", "")
GKEYS = [k.strip() for k in GROQ.split(",") if k.strip()]
PROVIDERS = [
    ("groq", "https://api.groq.com/openai/v1/chat/completions", GROQ, os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", os.getenv("GEMINI_API_KEY", ""), os.getenv("GEMINI_MODEL", "gemini-2.0-flash")),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", os.getenv("OPENROUTER_API_KEY", ""), os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")),
]
STY = {"viral": "with a strong hook, emotion or surprise", "funny": "that are the funniest or most entertaining",
       "insight": "with the most useful, insightful or educational point", "news": "with the most newsworthy facts or announcements"}
HL = {"gold": "&H00D7FF&", "green": "&H00FF00&", "red": "&H3C3CFF&", "cyan": "&HFFFF00&"}
AR = {"9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "16:9": (16, 9)}

def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode: raise RuntimeError((r.stderr or "")[-300:])

def upd(j, msg, pct): JOBS[j].update(msg=msg, pct=pct)

# ---------- transcript ----------
def groq_transcribe(path, o):
    model = "whisper-large-v3" if o["model"] == "best" else "whisper-large-v3-turbo"
    data = [("model", model), ("response_format", "verbose_json"), ("temperature", "0"),
            ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")]
    if o["lang"] in ("hi", "en"): data.append(("language", o["lang"]))
    if o["roman"]: data.append(("prompt", "Aaj hum baat karenge, ye video bahut mast hai, dekho kya hota hai."))
    for t in range(8):
        with open(path, "rb") as f:
            r = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                              headers={"Authorization": f"Bearer {GKEYS[t % len(GKEYS)]}"}, data=data, files={"file": f}, timeout=300)
        if r.status_code != 429: break
        time.sleep(min(float(r.headers.get("retry-after", 30)) + 2, 120))
    if r.status_code != 200: raise RuntimeError("Transcript fail: " + r.text[:200])
    js = r.json()
    bad = [(s["start"], s["end"]) for s in js.get("segments", []) if s.get("no_speech_prob", 0) > 0.7]
    words = [{"w": w["word"], "s": w["start"], "e": w["end"]} for w in js.get("words", []) if not any(a <= w["start"] <= b for a, b in bad)]
    segs = [{"s": s["start"], "e": s["end"], "t": s["text"].strip()} for s in js.get("segments", []) if s.get("no_speech_prob", 0) <= 0.7]
    return words, segs

def transcribe(j, d, src, o):
    if not GKEYS: raise RuntimeError("GROQ_API_KEY set nahi hai")
    upd(j, "Audio nikal raha hai", 10)
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
         "-f", "segment", "-segment_time", "600", "-reset_timestamps", "1", "a%03d.mp3"], cwd=d)
    words, segs = [], []
    parts = sorted(d.glob("a[0-9][0-9][0-9].mp3"))
    for i, p in enumerate(parts):
        upd(j, f"Transcript ban raha hai ({i+1}/{len(parts)})", 15 + 30 * i // len(parts))
        w, s = groq_transcribe(p, o)
        words += [{**x, "s": x["s"] + i * 600, "e": x["e"] + i * 600} for x in w]
        segs += [{**x, "s": x["s"] + i * 600, "e": x["e"] + i * 600} for x in s]
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

# ---------- picking moments ----------
def snap(c, segs, mn, mx):
    s0 = min(segs, key=lambda x: abs(x["s"] - c["start"]))["s"]
    ends = [x["e"] for x in segs if mn * 0.8 <= x["e"] - s0 <= mx]
    e0 = min(ends, key=lambda e: abs(e - c["end"])) if ends else s0 + min(mx, 45)
    return {**c, "start": s0, "end": e0}

def seg_text(segs, c, n=350):
    return " ".join(s["t"] for s in segs if s["s"] >= c["start"] and s["e"] <= c["end"])[:n]

def rerank(cands, segs, custom):
    top = sorted(cands, key=lambda c: -c["score"])[:15]
    items = [f"{i}. {seg_text(segs, c)}" for i, c in enumerate(top)]
    out = ask_llm("Rate each short-video clip text from 1 to 10 for how well it would hold viewers: strong first sentence, a complete standalone thought, "
                  "emotion, surprise or useful info" + (f", and how well it matches: {custom}" if custom else "") +
                  '. Return ONLY a JSON list: [{"i":0,"score":7}]\n\n' + "\n".join(items))
    try:
        for r in json.loads(re.search(r"\[.*\]", out, re.S).group(0)):
            c = top[int(r["i"])]; c["ai"] = float(r["score"]); c["score"] = 0.4 * c["score"] + 0.6 * c["ai"]
    except Exception:
        pass
    return top

def choose(cands, n):
    cands = sorted(cands, key=lambda c: -c["score"]); out = []
    for c in cands:
        if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in out): out.append(c)
        if len(out) >= n: break
    return sorted(out, key=lambda c: c["start"])

def pick(j, segs, n, mn, mx, style="viral", custom=""):
    upd(j, "Best moments dhoondh raha hai", 50)
    extra = f"The user also asked for: {custom}. Prefer moments that match this. " if custom else ""
    wins, win = [], []
    for s in segs:
        win.append(s)
        if s["e"] - win[0]["s"] > 600: wins.append(win); win = []
    if win: wins.append(win)
    cands = []
    for w in wins:
        txt = "\n".join(f"[{int(s['s'])}] {s['t']}" for s in w)
        out = ask_llm(f"You pick the best short-video clips from a transcript. Each line starts with its start time in seconds. "
                      f"Choose up to 5 moments of {mn}-{mx} seconds {STY[style]}. {extra}"
                      "Rules: the first sentence must hook the viewer; start exactly where a sentence begins and end after a thought is complete; "
                      "the clip must make sense on its own; skip intros, outros, sponsor reads and filler. "
                      'Return ONLY a JSON list: [{"start":sec,"end":sec,"title":"short catchy title in the transcript language","score":1-10}]\n\n' + txt)
        try:
            for c in json.loads(re.search(r"\[.*\]", out or "", re.S).group(0)):
                cands.append(snap({"start": float(c["start"]), "end": float(c["end"]),
                                   "title": str(c.get("title", ""))[:80], "score": float(c.get("score", 5))}, segs, mn, mx))
        except Exception:  # LLM fail: speech-density fallback
            for i in range(0, len(w), 6):
                g = w[i:i + 6]
                if g:
                    cands.append(snap({"start": g[0]["s"], "end": g[0]["s"] + (mn + mx) / 2, "title": g[0]["t"][:60],
                                       "score": sum(len(x["t"]) for x in g) / max(1, g[-1]["e"] - g[0]["s"]) / 6}, segs, mn, mx))
    cands = [c for c in cands if mn * 0.7 <= c["end"] - c["start"] <= mx + 5]
    if not cands: raise RuntimeError("Koi clip nahi bana, video chhoti ya bina awaaz ki ho sakti hai")
    if len(cands) > n: cands = rerank(cands, segs, custom)
    return choose(cands, n)

def energy_pick(j, d, src, n, mn, mx, K=None):
    upd(j, "Awaaz aur scene cuts analyze ho rahe hain", 20)
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-af", "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=rms.txt", "-f", "null", "-"], cwd=d)
    upd(j, "Scene cuts dhoondh raha hai", 35)
    run(["ffmpeg", "-y", "-skip_frame", "nokey", "-i", str(src), "-an", "-vf", "scale=320:-2,select='gt(scene,0.25)',metadata=print:file=scenes.txt",
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
        k = max(1, len(w) * 3 // 10)
        loud, peak = sum(w[:k]) / k, w[0] - w[-1]
        cd = sum(1 for c in cuts if s0 <= c < s0 + L) / L
        near = [c for c in cuts if abs(c - s0) <= 3]
        st = min(near, key=lambda c: abs(c - s0)) if near else s0
        cands.append({"start": st, "end": min(st + L, total), "score": 10 * (0.5 * loud + 0.25 * min(peak, 1) + 0.25 * min(cd / 0.2, 1))})
    out = choose(cands, K or n)
    for k, c in enumerate(out): c["title"], c["sig"] = f"Moment {k + 1}", c["score"]
    return out

def parse_manual(txt):
    f = lambda x: sum(float(p) * 60 ** i for i, p in enumerate(reversed(x.split(":"))))
    out = []
    for a, b in re.findall(r"(\d+(?::\d+){0,2}(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?::\d+){0,2}(?:\.\d+)?)", txt or ""):
        s, e = f(a), f(b)
        if 3 <= e - s <= 180: out.append({"start": s, "end": e, "title": "", "score": 10})
    return out[:10]

# ---------- link mode + stream signals ----------
def ytdlp(args, cwd=None):
    import importlib.util
    if importlib.util.find_spec("yt_dlp") is None:
        raise RuntimeError("Link mode is server pe band hai (yt-dlp install nahi hai). Video file upload karo, ya app apne PC/phone pe chalao (guide dekho).")
    base = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings"] + (["--cookies", str(COOKIES)] if COOKIES else [])
    js = ["--js-runtimes", "node"] if shutil.which("node") and not shutil.which("deno") else []
    r = subprocess.run(base + js + args, cwd=cwd, capture_output=True, text=True)
    if r.returncode and js and re.search(r"no such option|unrecognized arguments", r.stderr or "", re.I):
        r = subprocess.run(base + args, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        err = r.stderr or ""
        if re.search(r"sign in|not a bot|confirm you", err, re.I):
            raise RuntimeError("YouTube ne is server ko bot samajh ke block kiya. App apne PC/phone pe chalao (guide dekho).")
        raise RuntimeError("Link se download nahi hua: " + err[-200:])
    return r.stdout

def fetch_full(j, d, o):
    upd(j, "Video download ho rahi hai", 5)
    info = json.loads(ytdlp(["--dump-single-json", "--skip-download", o["url"]]))
    if float(info.get("duration") or 0) > 3600:
        raise RuntimeError("Video 1 ghante se lambi hai. Live stream ke liye Stream mode chuno (sirf best hisse download hote hain).")
    ytdlp(["-f", "bv*[height<=720]+ba/b[height<=720]/b", "--merge-output-format", "mp4", "-o", "src.%(ext)s", o["url"]], cwd=d)
    return next(x for x in d.glob("src.*") if x.suffix != ".part")

def read_chat(path):
    times = []
    with open(path, errors="ignore") as fh:  # line by line, badi chat file se RAM na bhare
        for line in fh:
            if "liveChatTextMessageRenderer" in line or "liveChatPaidMessageRenderer" in line:
                m = re.search(r'"videoOffsetTimeMsec":\s*"?(-?\d+)', line)
                if m and int(m.group(1)) >= 0: times.append(int(m.group(1)) / 1000)
    return times

def duration(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    return float(r.stdout.strip() or 0)

def rms_levels(d, src):
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-af", "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,"
         "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=rms.txt", "-f", "null", "-"], cwd=d)
    lv = {}
    for t, v in re.findall(r"pts_time:([\d.]+)\s*\nlavfi\.astats\.Overall\.RMS_level=(\S+)", (d / "rms.txt").read_text()):
        try: lv[int(float(t))] = max(float(v), -60)
        except ValueError: lv[int(float(t))] = -60
    return lv

def norm(arr):
    s = sorted(arr); p50, p98 = s[len(s) // 2], s[min(len(s) - 1, int(len(s) * 0.98))]
    return [min(max((x - p50) / (p98 - p50 + 1e-6), 0), 1) for x in arr]

def build_signal(total, chat, heat, rms):
    T, parts = int(total) + 1, []
    if chat:
        cnt = [0.0] * T
        for t in chat:
            if t < T: cnt[int(t)] += 1
        rate = norm([sum(cnt[max(0, i - 9):i + 1]) for i in range(T)])
        parts.append((0.5, [rate[min(T - 1, i + 12)] for i in range(T)]))  # chat reacts ~12s after the moment
    if heat:
        arr = [0.0] * T
        for h in heat:
            for t in range(int(h["start_time"]), min(T, int(h["end_time"]) + 1)): arr[t] = h["value"]
        parts.append((0.3, norm(arr)))
    if rms:
        parts.append((0.2 if parts else 1.0, norm([(rms.get(i, -60) + 60) / 60 for i in range(T)])))
    if not parts: raise RuntimeError("Koi signal nahi mila (chat, most-replayed ya audio)")
    ws = sum(w for w, _ in parts)
    return [sum(w * a[i] for w, a in parts) / ws for i in range(T)]

def peaks(sig, L, K):
    T, cands = len(sig), []
    for s0 in range(0, max(1, T - L), 2):
        w = sig[s0:s0 + L]
        top = sorted(w, reverse=True)[:max(1, len(w) * 3 // 10)]
        cands.append((sum(top) / len(top), s0, s0 + max(range(len(w)), key=lambda i: w[i])))
    cands.sort(reverse=True); out = []
    for sc, s0, p in cands:
        st = max(0, p - int(L * 0.3))
        c = {"start": float(st), "end": float(min(T - 1, st + L)), "score": 10 * sc}
        if all(c["end"] <= x["start"] or c["start"] >= x["end"] for x in out): out.append(c)
        if len(out) >= K: break
    return out

def stream_flow(j, d, o, src):
    url, chat, heat, rms = o["url"], [], [], {}
    if url:
        upd(j, "Video ki info aa rahi hai", 5)
        info = json.loads(ytdlp(["--dump-single-json", "--skip-download", url]))
        total, heat = float(info.get("duration") or 0), info.get("heatmap") or []
        if "live_chat" in (info.get("subtitles") or {}):
            upd(j, "Live chat replay download ho raha hai (lambi stream mein kuch minute lagte hain)", 8)
            ytdlp(["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", "chat.%(ext)s", url], cwd=d)
            f = next(d.glob("chat*.json"), None)
            chat = read_chat(f) if f else []
        if not chat and not heat:
            upd(j, "Sirf audio download ho rahi hai (loud moments dhoondhne ke liye)", 10)
            ytdlp(["-f", "ba[abr<=64]/worstaudio", "-o", "aud.%(ext)s", url], cwd=d)
            rms = rms_levels(d, next(x for x in d.glob("aud.*") if x.suffix != ".part"))
    else:
        total = duration(src)
        upd(j, "Awaaz ke loud moments dhoondh raha hai", 8)
        rms = rms_levels(d, src)
    L, pad = (o["mn"] + o["mx"]) // 2, 8
    if total < o["mn"]: raise RuntimeError("Video clip length se chhoti hai")
    cands = parse_manual(o["manual"]) or peaks(build_signal(total, chat, heat, rms), L, min(10, o["n"] + 3) if url else min(12, o["n"] * 2))
    words, segs = [], []
    for k, c in enumerate(cands):
        upd(j, f"Moment {k+1}/{len(cands)} check ho raha hai", 15 + 35 * k // len(cands))
        a, b = max(0.0, c["start"] - pad), min(total, c["end"] + pad)
        if url:
            ytdlp(["-f", "bv*[height<=720]+ba/b[height<=720]/b", "--download-sections", f"*{a:.0f}-{b:.0f}", "--merge-output-format", "mp4",
                   "-o", f"sec{k}.%(ext)s", url], cwd=d)
            sec, ss = next(x for x in d.glob(f"sec{k}.*") if x.suffix != ".part"), 0.0
        else:
            sec, ss = src, a
        run(["ffmpeg", "-y", "-ss", f"{ss:.2f}", "-t", f"{b - a:.2f}", "-i", str(sec), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", f"w{k}.mp3"], cwd=d)
        w, s = groq_transcribe(d / f"w{k}.mp3", o)
        w = [{**x, "s": x["s"] + a, "e": x["e"] + a} for x in w]; s = [{**x, "s": x["s"] + a, "e": x["e"] + a} for x in s]
        words += w; segs += s
        c.update(src=sec, abs0=a, abs1=b, title=f"Moment {k + 1}", sig=c["score"])
        if s:
            sn = snap(c, s, o["mn"], o["mx"])
            c["start"], c["end"] = max(a, sn["start"]), min(b, sn["end"])
    if len(cands) > o["n"]: cands = rerank(cands, segs, o["custom"])
    if o["vision"] and has_vision():
        for k, c in enumerate(cands):
            upd(j, f"Gameplay dekh raha hai ({k+1}/{len(cands)})", 52 + 3 * k // len(cands))
            vision_score(d, c.get("src") or src, c, k)
    finalize(cands)
    JOBS[j]["info"] = (f"Chat messages: {len(chat)} | Most replayed: {'haan' if heat else 'nahi'} | "
                       f"Awaaz signal: {'haan' if rms else 'nahi'} | Moments check kiye: {len(cands)}")
    return words, segs, choose(cands, o["n"])

# ---------- vision, facecam detect, trim, parts ----------
def has_vision(): return bool(PROVIDERS[1][2].strip())

def vision_ask(images, prompt):
    p = PROVIDERS[1]
    for key in [k.strip() for k in p[2].split(",") if k.strip()]:
        content = [{"type": "text", "text": prompt}] + [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}} for b in images]
        try:
            r = requests.post(p[1], headers={"Authorization": f"Bearer {key}"}, timeout=90,
                              json={"model": p[3], "temperature": 0.2, "messages": [{"role": "user", "content": content}]})
            if r.status_code == 200: return r.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
    return None

def frames_b64(d, srcf, times, tag, w=384):
    out = []
    for i, t in enumerate(times):
        f = d / f"{tag}{i}.jpg"
        try:
            run(["ffmpeg", "-y", "-ss", f"{max(t, 0):.2f}", "-i", str(srcf), "-frames:v", "1", "-vf", f"scale={w}:-2", "-q:v", "5", str(f)])
            out.append(base64.b64encode(f.read_bytes()).decode()); f.unlink()
        except Exception:
            pass
    return out

def vision_score(d, srcf, c, k):
    dur, ss = c["end"] - c["start"], c["start"] - c.get("abs0", 0.0)
    imgs = frames_b64(d, srcf, [ss + dur * q for q in (0.1, 0.35, 0.6, 0.85)], f"v{k}_")
    if not imgs: return
    out = vision_ask(imgs, "These are 4 frames in order from one short moment of a gameplay or live-stream video. Rate from 1 to 10 how exciting and clip-worthy the moment is "
                     "(action, explosions, chases, crashes, funny or chaotic events and big reactions score high; menus, loading screens, maps, idle walking and empty scenes score low). "
                     'Reply ONLY JSON: {"score":7,"what":"max 12 words on what happens"}')
    try:
        r = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        c["vis"], c["vwhat"] = float(r["score"]), str(r.get("what", ""))[:100]
    except Exception:
        pass

def finalize(cands):
    for c in cands:
        sg = c.get("sig", c["score"]); parts, names = [(0.4, sg)], [f"Signal {sg:.0f}"]
        if "ai" in c: parts.append((0.35, c["ai"])); names.append(f"AI {c['ai']:.0f}")
        if "vis" in c: parts.append((0.25, c["vis"])); names.append(f"Vision {c['vis']:.0f}" + (f" ({c['vwhat']})" if c.get("vwhat") else ""))
        c["score"] = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        c["why"] = " | ".join(names)

def detect_facecam(d, srcf, times):
    imgs = []
    for i, t in enumerate(times):
        f = d / f"fc{i}.jpg"
        try: run(["ffmpeg", "-y", "-ss", f"{max(t, 0):.2f}", "-i", str(srcf), "-frames:v", "1", str(f)]); imgs.append(f)
        except Exception: pass
    boxes = []
    try:
        import cv2
        casc = None
        for p in (os.getenv("FACE_CASCADE", ""), getattr(getattr(cv2, "data", None), "haarcascades", "") + "haarcascade_frontalface_default.xml"):
            if p and os.path.exists(p): casc = cv2.CascadeClassifier(p); break
        if casc is not None:
            for f in imgs:
                im = cv2.imread(str(f)); H, W = im.shape[:2]
                fs = casc.detectMultiScale(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), 1.1, 5, minSize=(W // 40, W // 40))
                if len(fs):
                    x, y, w, h = max(fs, key=lambda r: r[2] * r[3])
                    bw = min(W, w * 2.4); bh = min(H, bw / 1.6)
                    bx = min(max(0, x + w / 2 - bw / 2), W - bw); by = min(max(0, y + h / 2 - bh / 2), H - bh)
                    boxes.append((bx / W, by / H, bw / W, bh / H))
            if len(boxes) >= 2: boxes.sort(); return boxes[len(boxes) // 2]
    except Exception:
        pass
    if has_vision() and imgs:
        out = vision_ask([base64.b64encode(imgs[0].read_bytes()).decode()],
                         "Find the streamer's webcam (facecam) rectangle in this screenshot. Reply ONLY JSON with percentages of the image: "
                         '{"x":70,"y":65,"w":28,"h":30} or {"found":false} if there is no facecam.')
        try:
            r = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
            if r.get("found", True) and float(r["w"]) >= 8 and float(r["h"]) >= 8:
                return float(r["x"]) / 100, float(r["y"]) / 100, float(r["w"]) / 100, float(r["h"]) / 100
        except Exception:
            pass
    return boxes[0] if boxes else None

def keep_ranges(words, c, gap=0.7):
    ws = [w for w in words if w["s"] >= c["start"] - 0.05 and w["e"] <= c["end"] + 0.05]
    if len(ws) < 3: return None
    rng = []
    for w in ws:
        a, b = max(c["start"], w["s"] - 0.1), min(c["end"], w["e"] + 0.18)
        if rng and a - rng[-1][1] <= gap: rng[-1][1] = max(rng[-1][1], b)
        else: rng.append([a, b])
    if len(rng) > 60 or (c["end"] - c["start"]) - sum(b - a for a, b in rng) < 1.0: return None
    return rng

def tmap(rng, t):
    acc = 0.0
    for a, b in rng:
        if t < a: return acc
        if t <= b: return acc + t - a
        acc += b - a
    return acc

def split_parts(chosen, words, P):
    out = []
    for c in chosen:
        dur = c["end"] - c["start"]
        if P <= 0 or dur <= P * 1.35: out.append(c); continue
        k = max(2, round(dur / P)); cuts = [c["start"]]
        ws = [w for w in words if c["start"] < w["s"] < c["end"]]
        for i in range(1, k):
            ideal = c["start"] + dur * i / k
            best, bg = None, -1
            for x, w in enumerate(ws):
                if abs(w["s"] - ideal) <= 6:
                    g = w["s"] - (ws[x - 1]["e"] if x else c["start"])
                    if g > bg: best, bg = w, g
            cuts.append(best["s"] - 0.05 if best else ideal)
        cuts.append(c["end"])
        for i in range(k):
            out.append({**c, "start": cuts[i], "end": cuts[i + 1], "part": f"{i + 1}/{k}", "title": f"{c['title']} - Part {i + 1}/{k}",
                        "hook": c.get("hook") or f"PART {i + 1}/{k}", "outro": f"Part {i + 2} ke liye follow karo" if i + 1 < k else ""})
    return out[:20]

# ---------- captions + render ----------
def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def make_ass(words, c, path, o):
    W, H, cap = o["W"], o["H"], o["cap"]
    start, end = c["start"], c["end"]
    fs = int(min(W, H) * 0.087 * {"s": 0.8, "m": 1, "l": 1.2}[o["size"]])
    al, mv = {"bottom": (2, int(H * (0.26 if o["aspect"] == "9:16" else 0.1))), "center": (5, 0), "top": (8, int(H * 0.2)),
                "seam": (8, max(0, int(H * o["ratio"] - fs * 0.7)))}[o["pos"]]
    bs, ol, oc = (3, 10, "&HB0000000") if cap == "box" else (1, 6 if cap == "pop" else 5, "&H00000000")
    hl = HL[o["hl"]]
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
            f"Style: D,Poppins,{fs},&H00FFFFFF,&H00FFFFFF,{oc},&H80000000,1,0,0,0,100,100,0,0,{bs},{ol},2,{al},40,40,{mv},1\n"
            f"Style: C,Poppins,{int(min(W, H) * 0.04)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,{int(H * 0.035)},1\n"
            f"Style: O,Poppins,{int(min(W, H) * 0.065)},&H00FFFFFF,&H00FFFFFF,&HB0000000,&H80000000,1,0,0,0,100,100,0,0,3,14,0,5,50,50,0,1\n"
            f"Style: H,Poppins,{int(min(W, H) * 0.074)},&H00000000,&H00000000,&H0000D7FF,&H00000000,1,0,0,0,100,100,0,0,3,14,0,8,50,50,{int(H * 0.07)},1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")

    def clean(w):
        t = re.sub(r"[{}\\]", "", re.sub(r"[,.;:।]+$", "", w.strip()))
        return t.upper() if o["letters"] == "upper" else t

    ev = []
    ht = c.get("hook") or o["hooktext"] or c["title"]
    if (o["hook"] or c.get("hook")) and ht:
        t = re.sub(r"[{}\\\n]", " ", ht)
        ev.append(f"Dialogue: 1,{ts(0)},{ts(min(3.5, end - start))},H,,0,0,0,,{{\\fscx88\\fscy88\\t(0,120,\\fscx100\\fscy100)}}{t}")
    ot = c.get("outro") or o["outro"]
    if ot:
        ots = re.sub(r"[{}\\\n]", " ", ot)
        ev.append(f"Dialogue: 2,{ts(max(0, end - start - 2.5))},{ts(end - start)},O,,0,0,0,,{ots}")
    if o["credit"]:
        ev.append(f"Dialogue: 0,{ts(0)},{ts(end - start)},C,,0,0,0,,{o['credit']}")
    ws = [{"t": clean(w["w"]), "s": w["s"], "e": w["e"]} for w in words if w["s"] >= start - 0.05 and w["e"] <= end + 0.05] if cap != "none" else []
    ws = [w for w in ws if w["t"]]
    maxc = max(8, int(W * 0.85 / (fs * 0.6)))
    gs, cur = [], []
    for w in ws:
        cur.append(w)
        if len(cur) >= o["wpl"] or sum(len(x["t"]) + 1 for x in cur) > maxc: gs.append(cur); cur = []
    if cur: gs.append(cur)
    pop = "{\\fscx88\\fscy88\\t(0,80,\\fscx100\\fscy100)}"
    for gi, g in enumerate(gs):
        nxt = gs[gi + 1][0]["s"] if gi + 1 < len(gs) else 1e9
        if cap == "white":
            ev.append(f"Dialogue: 0,{ts(max(g[0]['s'] - start, 0))},{ts(min(g[-1]['e'] + 0.3, nxt) - start)},D,,0,0,0,,{' '.join(x['t'] for x in g)}")
            continue
        for j, w in enumerate(g):
            t0 = max(w["s"] - start, 0)
            t1 = (g[j + 1]["s"] if j + 1 < len(g) else min(w["e"] + 0.3, nxt)) - start
            on, off = ("{\\c" + hl + "\\fscx112\\fscy112}", "{\\c&HFFFFFF&\\fscx100\\fscy100}") if cap == "pop" else ("{\\c" + hl + "}", "{\\c&HFFFFFF&}")
            txt = pop + " ".join((on + x["t"] + off) if m == j else x["t"] for m, x in enumerate(g))
            ev.append(f"Dialogue: 0,{ts(t0)},{ts(max(t1, t0 + 0.12))},D,,0,0,0,,{txt}")
    Path(path).write_text(head + "\n".join(ev), encoding="utf-8")

def render(d, src, c, words, i, o):
    W, H = o["W"], o["H"]
    dur0 = dur = c["end"] - c["start"]
    sel = asel = ""
    rng = keep_ranges(words, c) if o["trim"] else None
    wds, c2 = words, c
    if rng:
        rel = [(a - c["start"], b - c["start"]) for a, b in rng]
        expr = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in rel)
        sel, asel = f"select='{expr}',setpts=N/(FRAME_RATE*TB),", f"aselect='{expr}',asetpts=N/(SR*TB),"
        dur = sum(b - a for a, b in rel)
        wds = [{**w, "s": c["start"] + tmap(rng, w["s"]), "e": c["start"] + tmap(rng, w["e"])} for w in words if w["s"] >= c["start"] - 0.05 and w["e"] <= c["end"] + 0.05]
        c2 = {**c, "end": c["start"] + dur}
    make_ass(wds, c2, d / f"c{i}.ass", o)
    try:
        if not (d / "Poppins-Bold.ttf").exists(): shutil.copy(Path(__file__).resolve().parent / "Poppins-Bold.ttf", d / "Poppins-Bold.ttf")
    except Exception:
        pass
    if o["layout"] == "stack":
        Ht = int(H * o["ratio"]) // 2 * 2; Hg = H - Ht
        lay = (f"fps=30,split[a][b];[a]crop=trunc(iw*{o['fw']:.3f}/2)*2:trunc(ih*{o['fh']:.3f}/2)*2:iw*{o['fx']:.3f}:ih*{o['fy']:.3f},"
               f"scale={W}:{Ht}:force_original_aspect_ratio=increase,crop={W}:{Ht}[top];"
               f"[b]scale={W}:{Hg}:force_original_aspect_ratio=increase,crop={W}:{Hg}:x='(iw-{W})*{o['gx']:.3f}'[bot];[top][bot]vstack")
    elif o["layout"] == "crop":
        lay = f"fps=30,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    else:
        bw, bh = max(2, W // 16 * 2), max(2, H // 16 * 2)
        lay = (f"fps=30,split[a][b];[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=2:1,scale={W}:{H}[bg];"
               f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    z = f",scale=w='trunc({W}*(1+0.07*abs(sin(t*0.9)))/2)*2':h=-2:eval=frame,crop={W}:{H}" if o["zoom"] and o["layout"] != "stack" else ""
    game = o["kind"] == "game"
    g = f",eq=contrast=1.06:saturation=1.2,fade=t=in:st=0:d=0.25,fade=t=out:st={dur - 0.3:.2f}:d=0.3" if game else ""
    af = asel + "loudnorm=I=-16:TP=-1.5" + (f",afade=t=in:st=0:d=0.2,afade=t=out:st={dur - 0.3:.2f}:d=0.3" if game else "")
    fc = f"[0:v]{sel}{lay}{z}{g},ass=c{i}.ass:fontsdir=." + ("[v0]" if o["bar"] else "[v]")
    if o["bar"]:
        fc += (f";color=c=0xFFD700:s={W}x{max(6, H // 120)}:r=30:d={dur:.2f}[bar];"
               f"[v0][bar]overlay=x='-W+W*t/{dur:.2f}':y=H/40:shortest=1[v]")
    run(["ffmpeg", "-y", "-ss", f"{c['start'] - c.get('abs0', 0.0):.2f}", "-i", str(c.get("src") or src), "-t", f"{dur0:.2f}", "-filter_complex", fc,
         "-map", "[v]", "-map", "0:a:0?", "-af", af, "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
         "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", f"clip{i+1}.mp4"], cwd=d)

def meta(chosen, segs, topic):
    items = [f"{i + 1}. {(seg_text(segs, c, 400) if segs else '') or topic}" for i, c in enumerate(chosen)]
    out = ask_llm("For each numbered short video clip below write a catchy title (max 60 chars) and 5 hashtags, in the same language as the clip text. "
                  'Return ONLY a JSON list in the same order: [{"title":"...","tags":"#a #b"}]\n\nTopic: ' + topic + "\n" + "\n".join(items))
    try:
        arr = json.loads(re.search(r"\[.*\]", out, re.S).group(0))
        for c, a in zip(chosen, arr): c["meta"] = f'{a.get("title", "")}\n{a.get("tags", "")}'.strip()
    except Exception:
        pass

def apply_text(words, c, text):
    ws = [w for w in words if w["s"] >= c["start"] - 0.05 and w["e"] <= c["end"] + 0.05]
    toks = text.split()
    if not toks or not ws or [w["w"].strip() for w in ws] == toks: return words
    if len(toks) == len(ws):
        new = [{**w, "w": t} for w, t in zip(ws, toks)]
    else:
        a, b = ws[0]["s"], ws[-1]["e"]; step = (b - a) / len(toks)
        new = [{"w": t, "s": a + i * step, "e": a + (i + 1) * step} for i, t in enumerate(toks)]
    ids = {id(w) for w in ws}
    return sorted([w for w in words if id(w) not in ids] + new, key=lambda w: w["s"])

def clip_entry(j, k, c, words):
    ws = [w["w"].strip() for w in words if w["s"] >= c["start"] - 0.05 and w["e"] <= c["end"] + 0.05]
    return {"url": f"/files/{j}/clip{k+1}.mp4?v={int(time.time())}", "title": c["title"], "score": round(c["score"], 1),
            "start": round(c["start"], 1), "end": round(c["end"], 1), "meta": c.get("meta", ""), "hook": c.get("hook", ""), "text": " ".join(ws), "why": c.get("why", "")}

def build_opts(f):
    def g(k, d=""):
        v = f.get(k); return d if v is None else str(v)
    def i(k, d):
        try: return int(float(g(k, str(d))))
        except ValueError: return d
    aspect = g("aspect", "9:16"); aspect = aspect if aspect in AR else "9:16"
    res = i("res", 720); res = res if res in (480, 540, 720, 1080) else 720
    ev = lambda x: int(x // 2 * 2)
    W, H = (ev(res * 16 / 9), res) if aspect == "16:9" else (res, ev(res * AR[aspect][1] / AR[aspect][0]))
    mn = i("minlen", 20); mx = max(i("maxlen", 45), mn + 5)
    ht = re.sub(r"[\U00010000-\U0010ffff{}\\\n]", " ", g("hooktext")).strip()[:60]
    def pct(k, d, lo, hi): return max(lo, min(hi, i(k, d))) / 100
    fx, fy, fw, fh = pct("fx", 74, 0, 90), pct("fy", 71, 0, 90), pct("fw", 25, 5, 100), pct("fh", 28, 5, 100)
    fw, fh = min(fw, 1 - fx), min(fh, 1 - fy)
    kind = g("kind") if g("kind") in ("game", "stream") else "talk"
    url = g("url").strip()
    url = url if re.match(r"https?://", url) else ""
    return dict(n=max(1, min(i("n", 5), 10)), mn=mn, mx=mx, kind=kind, style=g("style") if g("style") in STY else "viral",
                layout=g("layout") if g("layout") in ("crop", "stack") else "blur", fx=fx, fy=fy, fw=fw, fh=fh, ratio=pct("ratio", 38, 25, 55), gx=pct("gx", 50, 0, 100), aspect=aspect, W=W, H=H, res=res,
                cap=g("cap") if g("cap") in ("gold", "pop", "white", "box", "none") else "gold",
                pos=g("pos") if g("pos") in ("bottom", "center", "top", "seam") else "bottom", wpl=max(1, min(i("wpl", 3), 4)),
                hook=bool(ht) if kind == "game" else (i("hook", 1) == 1 or bool(ht)), zoom=i("zoom", 0) == 1, bar=i("bar", 0) == 1,
                hooktext=ht, letters="normal" if g("letters") == "normal" else "upper", hl=g("hl") if g("hl") in HL else "gold",
                size=g("size") if g("size") in ("s", "m", "l") else "m", roman=g("script") == "roman",
                lang=g("lang") if g("lang") in ("hi", "en") else "auto", model="fast" if g("model") == "fast" else "best",
                parts=i("parts", 0) if i("parts", 0) in (30, 45, 60) else 0, outro=re.sub(r"[\U00010000-\U0010ffff{}\\\n]", " ", g("outro")).strip()[:50],
                trim=i("trim", 0) == 1, vision=i("vision", 0) == 1, autocam=i("autocam", 1) == 1,
                custom=" ".join(g("prompt").split())[:300], manual=g("manual")[:500], url=url,
                credit=re.sub(r"[\U00010000-\U0010ffff{}\\\n]", " ", g("credit")).strip()[:40])

# ---------- jobs ----------
def work(j, o):
    d = BASE / j
    with LOCK:
        try:
            upd(j, "Shuru ho raha hai", 2)
            src = next((x for x in d.glob("src.*") if x.stat().st_size > 0), None)
            if src is None and o["url"] and o["kind"] != "stream": src = fetch_full(j, d, o)
            if src is None and not o["url"]: raise RuntimeError("Video file nahi mili")
            manual, words, segs = parse_manual(o["manual"]), [], []
            vis = o["vision"] and has_vision()
            if o["kind"] == "stream":
                words, segs, chosen = stream_flow(j, d, o, src)
            elif o["kind"] == "game":
                cands = manual or energy_pick(j, d, src, o["n"], o["mn"], o["mx"], K=o["n"] * 2 if vis else None)
                for c in cands: c.setdefault("sig", c["score"])
                if vis:
                    for k, c in enumerate(cands):
                        upd(j, f"Gameplay dekh raha hai ({k+1}/{len(cands)})", 45 + 8 * k // len(cands))
                        vision_score(d, src, c, k)
                finalize(cands)
                chosen = choose(cands, o["n"])
            else:
                words, segs = transcribe(j, d, src, o)
                chosen = manual or (pick(j, segs, o["n"], o["mn"], o["mx"], o["style"], o["custom"]) if segs
                                    else energy_pick(j, d, src, o["n"], o["mn"], o["mx"]))
            for k, c in enumerate(chosen):
                if not c.get("title") or re.match(r"(Moment|Clip) \d+$", c["title"]): c["title"] = f"Clip {k + 1}"
                c.setdefault("why", f"AI {c['score']:.0f}")
            chosen = split_parts(chosen, words, o["parts"])
            upd(j, "Titles aur hashtags bana raha hai", 52)
            meta(chosen, segs, o["hooktext"] or o["custom"] or "gaming video")
            for c in chosen:
                if c.get("meta") and re.match(r"(Moment|Clip) \d+", c["title"]):
                    c["title"] = c["meta"].split("\n")[0][:60] + (f" - Part {c['part']}" if c.get("part") else "")
            if o["layout"] == "stack" and o["autocam"] and chosen:
                upd(j, "Facecam dhoondh raha hai", 53)
                c0 = chosen[0]; dd, ss = c0["end"] - c0["start"], c0["start"] - c0.get("abs0", 0.0)
                box = detect_facecam(d, c0.get("src") or src, [ss + dd * q for q in (0.2, 0.5, 0.8)])
                if box:
                    fx, fy = min(max(box[0], 0), 0.9), min(max(box[1], 0), 0.9)
                    o.update(fx=fx, fy=fy, fw=min(max(box[2], 0.05), 1 - fx), fh=min(max(box[3], 0.05), 1 - fy))
                    JOBS[j]["info"] = (JOBS[j].get("info", "") + " | Facecam khud mila").strip(" |")
                else:
                    JOBS[j]["info"] = (JOBS[j].get("info", "") + " | Facecam nahi mila, tumhari set ki hui jagah use hui").strip(" |")
            JD[j] = {"words": words, "clips": chosen}
            out = []
            for i, c in enumerate(chosen):
                upd(j, f"Clip {i+1}/{len(chosen)} edit ho raha hai", 55 + 43 * i // len(chosen))
                render(d, src, c, words, i, o)
                out.append(clip_entry(j, i, c, words))
            for f in d.glob("*"):
                if f.suffix in (".mp3", ".ass", ".txt"): f.unlink()
            JOBS[j].update(status="done", msg="Clips ready", pct=100, clips=out)
            save_state(j)
        except Exception as e:
            JOBS[j].update(status="error", msg=str(e)[:300])

def redo_work(j, f):
    d, jd = BASE / j, JD[j]
    with LOCK:
        try:
            o, k = build_opts(f), int(f.get("k", -1))
            src = next((x for x in d.glob("src.*") if x.stat().st_size > 0), None)
            ks = list(range(len(jd["clips"]))) if k < 0 else [k]
            if k >= 0:
                c = jd["clips"][k]
                jd["words"] = apply_text(jd["words"], c, str(f.get("text", "")))
                s, e = float(f.get("start", c["start"])), float(f.get("end", c["end"]))
                if "abs0" in c: s, e = max(s, c["abs0"]), min(e, c["abs1"])
                if 3 <= e - s <= 180: c["start"], c["end"] = s, e
                c["hook"] = re.sub(r"[{}\\\n]", " ", str(f.get("cliphook", ""))).strip()[:60]
            for n, i in enumerate(ks):
                upd(j, f"Clip {i+1} dobara ban raha hai ({n+1}/{len(ks)})", 10 + 85 * n // len(ks))
                render(d, src, jd["clips"][i], jd["words"], i, o)
                JOBS[j]["clips"][i] = clip_entry(j, i, jd["clips"][i], jd["words"])
            for x in d.glob("*.ass"): x.unlink()
            JOBS[j].update(status="done", msg="Clips ready", pct=100)
            save_state(j)
        except Exception as e:
            JOBS[j].update(status="error", msg=str(e)[:300])

def check(code):
    if CODE and code != CODE: raise HTTPError(403, "Access code galat hai")

def up_init(f):
    check(f.get("code", ""))
    for p in BASE.glob("*"):
        if p.is_dir() and time.time() - p.stat().st_mtime > KEEP:
            shutil.rmtree(p, ignore_errors=True)
            for m in (JOBS, JD, UP): m.pop(p.name, None)
    j = uuid.uuid4().hex[:10]; (BASE / j).mkdir()
    ext = Path(str(f.get("name", ""))).suffix.lower()
    ext = ext if ext in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi") else ".mp4"
    UP[j] = {"ext": ext, "n": 0, "name": str(f.get("name", ""))}
    (BASE / j / f"src{ext}").write_bytes(b"")
    return {"id": j}

def up_chunk(j, i, data):
    u = UP.get(j)
    if not u: raise HTTPError(404, "Upload nahi mila")
    if i < u["n"]: return {"ok": True}
    if i > u["n"]: raise HTTPError(409, "Chunk order galat")
    with open(BASE / j / f"src{u['ext']}", "ab") as fh: fh.write(data)
    u["n"] += 1
    return {"ok": True}

def create(f):
    check(f.get("code", ""))
    j = str(f.get("upload_id", "") or "")
    urls = [u for u in re.split(r"[\s,]+", str(f.get("url", ""))) if re.match(r"https?://", u)][:8]
    if j:
        if j not in UP: raise HTTPError(404, "Pehle video upload karo")
        jobs = [(j, build_opts({**f, "url": ""}), UP[j].get("name") or "Upload")]
    elif urls:
        jobs = []
        for u in urls:
            jj = uuid.uuid4().hex[:10]; (BASE / jj).mkdir(); UP[jj] = {"ext": ".mp4", "n": 0, "name": u}
            jobs.append((jj, build_opts({**f, "url": u}), u))
    else:
        raise HTTPError(400, "Link do ya video upload karo")
    for jj, o, label in jobs:
        if JOBS.get(jj, {}).get("status") == "running": raise HTTPError(409, "Ye job chal raha hai")
        JOBS[jj] = {"status": "running", "msg": f"Queue mein hai (aapse pehle {Q.qsize()} kaam)", "pct": 0, "clips": [], "label": label, "ts": int(time.time())}
        Q.put((work, (jj, o)))
    return {"id": jobs[0][0], "ids": [x[0] for x in jobs]}

def redo(j, f):
    check(f.get("code", ""))
    if j not in JD: raise HTTPError(404, "Session khatam ho gaya (server restart ya kuch ghante). Dobara clips banao")
    if JOBS[j]["status"] == "running": raise HTTPError(409, "Abhi kaam chal raha hai, thoda ruko")
    JOBS[j].update(status="running", msg="Shuru ho raha hai", pct=5)
    Q.put((redo_work, (j, f)))
    return {"ok": True}

def frame(f):
    check(f.get("code", ""))
    url = str(f.get("url", "")).strip()
    if not re.match(r"https?://", url): raise HTTPError(400, "Link galat hai")
    t = max(0.0, float(f.get("t", 60)))
    d = BASE / ("f" + uuid.uuid4().hex[:8]); d.mkdir()
    try:
        ytdlp(["-f", "bv*[height<=480]/b[height<=480]/worst", "--download-sections", f"*{t:.0f}-{t + 3:.0f}", "-o", "fr.%(ext)s", url], cwd=d)
        run(["ffmpeg", "-y", "-i", str(next(x for x in d.glob("fr.*") if x.suffix != ".part")), "-frames:v", "1", "fr.jpg"], cwd=d)
    except Exception as e:
        raise HTTPError(400, str(e)[:200])
    return d / "fr.jpg"

def health():
    import importlib.util
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "ytdlp": importlib.util.find_spec("yt_dlp") is not None, "groq": bool(GKEYS),
            "gemini": has_vision(), "opencv": importlib.util.find_spec("cv2") is not None}

def history(code=""):
    check(code)
    rows = [{"id": j, "label": s.get("label", j), "ts": s.get("ts", 0), "n": len(s.get("clips", []))} for j, s in JOBS.items() if s.get("status") == "done"]
    return sorted(rows, key=lambda r: -r["ts"])[:30]

def status(j):
    if j not in JOBS: raise HTTPError(404, "Job nahi mila")
    return JOBS[j]

def zipall(j):
    d = BASE / j
    if not d.is_dir(): raise HTTPError(404, "Job nahi mila")
    with zipfile.ZipFile(d / "all.zip", "w", zipfile.ZIP_STORED) as zf:
        for f in sorted(d.glob("clip*.mp4")): zf.write(f, f.name)
    return d / "all.zip"

def save_state(j):
    try:
        jd = JD.get(j)
        if jd: (BASE / j / "state.json").write_text(json.dumps({"job": JOBS[j], "words": jd["words"], "clips": jd["clips"]}, default=str))
    except Exception:
        pass

def load_states():
    for p in BASE.glob("*/state.json"):
        try:
            st, j = json.loads(p.read_text()), p.parent.name
            for c in st["clips"]:
                if c.get("src"): c["src"] = Path(c["src"])
            JOBS[j], JD[j] = st["job"], {"words": st["words"], "clips": st["clips"]}
        except Exception:
            pass


HERE = Path(__file__).resolve().parent

def safe(root, rel):
    root = root.resolve(); p = (root / unquote(rel)).resolve()
    if root not in p.parents or not p.is_file(): raise HTTPError(404, "File nahi mili")
    return p

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def _json(self, obj, code=200): self._send(code, json.dumps(obj).encode())

    def _file(self, path, ctype=None, name=None):
        size = path.stat().st_size; start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and size:
            a, b = m.groups()
            if a == "" and b: start = max(0, size - int(b))
            elif a != "": start, end = int(a), (min(int(b), size - 1) if b else size - 1)
            if start > end or start >= size: return self._send(416, b"", extra={"Content-Range": f"bytes */{size}"})
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes"); self.send_header("Content-Length", str(max(0, end - start + 1)))
        if code == 206: self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if name: self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        if self.command == "HEAD": return
        with open(path, "rb") as fh:
            fh.seek(start); left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk: break
                self.wfile.write(chunk); left -= len(chunk)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _form(self):
        try: return json.loads(self._body() or b"{}")
        except ValueError: raise HTTPError(400, "Request galat hai")

    def route(self, method, path, q):
        if method == "GET":
            if path == "/": return self._file(HERE / "index.html", "text/html; charset=utf-8")
            if path == "/manifest.webmanifest": return self._file(HERE / "manifest.webmanifest", "application/manifest+json")
            if path in ("/static/icon-192.png", "/static/icon-512.png"): return self._file(HERE / path[8:])
            if path.startswith("/files/"): return self._file(safe(BASE, path[7:]))
            if path == "/api/health": return self._json(health())
            if path == "/api/history": return self._json(history(q.get("code", [""])[0]))
            m = re.fullmatch(r"/api/jobs/(\w+)", path)
            if m: return self._json(status(m.group(1)))
            m = re.fullmatch(r"/api/jobs/(\w+)/zip", path)
            if m: return self._file(zipall(m.group(1)), "application/zip", "clips.zip")
            raise HTTPError(404, "Nahi mila")
        if path == "/api/upload/init": return self._json(up_init(self._form()))
        m = re.fullmatch(r"/api/upload/(\w+)", path)
        if m: return self._json(up_chunk(m.group(1), int(q.get("i", ["0"])[0]), self._body()))
        if path == "/api/jobs": return self._json(create(self._form()))
        m = re.fullmatch(r"/api/jobs/(\w+)/redo", path)
        if m: return self._json(redo(m.group(1), self._form()))
        if path == "/api/frame": return self._file(frame(self._form()), "image/jpeg")
        raise HTTPError(404, "Nahi mila")

    def _handle(self, method):
        u = urlparse(self.path)
        try: self.route(method, u.path, parse_qs(u.query))
        except HTTPError as e: self._json({"detail": e.detail}, e.code)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e: self._json({"detail": "Server error: " + str(e)[:200]}, 500)

    def do_GET(self): self._handle("GET")
    def do_HEAD(self): self._handle("GET")
    def do_POST(self): self._handle("POST")

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, request, client_address): pass  # browser ne connection beech mein band kiya, ignore

def serve(host=None, port=None):
    port = port or int(os.getenv("PORT", "7860")); host = host or os.getenv("HOST", "0.0.0.0")
    load_states()
    srv = Server((host, port), Handler)
    print(f"Clipper chal raha hai: http://localhost:{port}  (band karne ke liye Ctrl+C)")
    return srv

if __name__ == "__main__":
    serve().serve_forever()
