import os, re, json, subprocess, shutil, uuid
from pathlib import Path
import secrets, hmac
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for, flash
from werkzeug.security import check_password_hash
from yt_dlp import YoutubeDL

ROOT = Path(__file__).parent
WORK = ROOT / "work"
WORK.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

AUTH_FILE = ROOT / "auth_config.json"
def load_auth():
    return json.loads(AUTH_FILE.read_text(encoding="utf-8"))

app.secret_key = load_auth()["secret_key"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]

def verify_csrf():
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
    return bool(token) and hmac.compare_digest(token, session.get("csrf_token", ""))

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify(error="Unauthorized. Please log in."), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("home"))
    if request.method == "POST":
        if not verify_csrf():
            flash("Invalid CSRF token. Please try again.")
            return redirect(url_for("login"))
        auth = load_auth()
        u = request.form.get("username", "").strip()
        p_in = request.form.get("password", "")
        if hmac.compare_digest(u, auth["username"]) and check_password_hash(auth["password_hash"], p_in):
            session.clear()
            session["logged_in"] = True
            session["username"] = u
            get_csrf_token()
            return redirect(url_for("home"))
        flash("Invalid username or password.")
        return redirect(url_for("login"))
    return render_template("login.html", csrf_token=get_csrf_token())

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    if not verify_csrf():
        return jsonify(error="Invalid CSRF token."), 403
    session.clear()
    return redirect(url_for("login"))

def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout

def get_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())

def download_video(url, folder):
    opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "outtmpl": str(folder / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    vid = info.get("id") or ""
    if vid:
        (folder / "video_id.txt").write_text(vid, encoding="utf-8")
    # Safely fetch subtitles separately so 429 never breaks video download
    try:
        sub_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "hi"],
            "subtitlesformat": "vtt",
            "outtmpl": str(folder / "source.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "sleep_interval_subtitles": 2,
        }
        with YoutubeDL(sub_opts) as sydl:
            sydl.download([url])
    except Exception:
        pass
    candidates = list(folder.glob("source.*"))
    candidates = [p for p in candidates if p.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]]
    if not candidates:
        raise RuntimeError("Video download nahi mila. Link public/accessible hai ya nahi check karein.")
    return candidates[0], info.get("title", "YouTube video")

def make_segments(duration, count, clip_len):
    # Fallback when optional speech transcription is unavailable.
    count = max(1, min(int(count), 10))
    clip_len = max(15, min(int(clip_len), 90))
    if duration <= clip_len:
        return [(0, duration)]
    step = max(clip_len, (duration - clip_len) / max(1, count - 1))
    starts = [min(i * step, duration - clip_len) for i in range(count)]
    return [(s, min(s + clip_len, duration)) for s in starts]

def _vtt_to_sec(t):
    parts = t.replace(",", ".").split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return int(parts[0]) * 60 + float(parts[1])

def parse_vtt_subtitles(folder):
    vtts = sorted(folder.glob("source*.vtt"))
    if not vtts:
        return []
    raw = vtts[0].read_text(encoding="utf-8", errors="ignore")
    rows = []
    blocks = re.split(r"\n\s*\n", raw)
    last_text = ""
    for b in blocks:
        m = re.search(r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})", b)
        if not m:
            continue
        s = _vtt_to_sec(m.group(1))
        e = _vtt_to_sec(m.group(2))
        lines = b.splitlines()
        txt_lines = [ln for ln in lines if "-->" not in ln and not ln.strip().isdigit() and not ln.startswith("WEBVTT")]
        txt = " ".join(txt_lines)
        txt = re.sub(r"<[^>]+>", "", txt)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt and txt != last_text and e > s:
            rows.append({"start": s, "end": e, "text": txt})
            last_text = txt
    return rows

def fetch_yt_api_transcript(folder):
    vid_file = folder / "video_id.txt"
    if not vid_file.exists():
        return []
    vid = vid_file.read_text(encoding="utf-8").strip()
    if not vid:
        return []
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt = YouTubeTranscriptApi()
        try:
            fetched = ytt.fetch(vid, languages=["hi", "en", "en-IN", "hi-IN"])
        except Exception:
            tlist = ytt.list(vid)
            fetched = next(iter(tlist)).fetch()
        rows = []
        for item in fetched:
            s = float(getattr(item, "start", item.get("start", 0) if isinstance(item, dict) else 0))
            d = float(getattr(item, "duration", item.get("duration", 1.5) if isinstance(item, dict) else 1.5))
            txt = str(getattr(item, "text", item.get("text", "") if isinstance(item, dict) else "")).strip()
            txt = re.sub(r"\s+", " ", txt)
            if txt:
                rows.append({"start": s, "end": s + max(0.3, d), "text": txt})
        return rows
    except Exception:
        return []

def try_transcribe(video, folder):
    api_rows = fetch_yt_api_transcript(folder)
    if api_rows:
        return api_rows
    vtt_rows = parse_vtt_subtitles(folder)
    if vtt_rows:
        return vtt_rows
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(video), vad_filter=True)
        return [{"start": float(s.start), "end": float(s.end), "text": (s.text or "").strip()} for s in segments if (s.text or "").strip()]
    except Exception:
        return []

def choose_highlights(transcript, duration, count, clip_len):
    if not transcript:
        return make_segments(duration, count, clip_len), "smart evenly-spaced fallback"
    # Rank transcript windows by speech density + emphasis words; spread chosen clips apart.
    windows = []
    step = 5.0
    t = 0.0
    while t < duration:
        end = min(t + clip_len, duration)
        words = " ".join(x["text"] for x in transcript if x["start"] < end and x["end"] > t)
        if words.strip():
            emphasis = len(re.findall(r"\b(wow|amazing|secret|important|best|never|always|why|how|because|but|actually|mistake|surprise)\b", words.lower()))
            density = len(words.split()) / max(1, end-t)
            score = density + 0.35 * emphasis
            windows.append((score, t, end))
        t += step
    windows.sort(reverse=True)
    chosen = []
    for score, start, end in windows:
        if all(abs(start - old[0]) > clip_len * 0.75 for old in chosen):
            chosen.append((start, end))
        if len(chosen) >= count:
            break
    chosen.sort()
    return chosen or make_segments(duration, count, clip_len), "transcript-ranked"

def _ass_time(sec):
    sec = max(0.0, float(sec))
    h, m, s = int(sec // 3600), int((sec % 3600) // 60), int(sec % 60)
    cs = min(99, int(round((sec - int(sec)) * 100)))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def build_ass_subtitles(transcript, start, end, ass_path, caption_style="viral", layout="auto"):
    if caption_style == "off" or not transcript:
        return None
    hi_map = {"viral": "&H00FFFF&", "purple": "&HFF5C9C&", "green": "&H55FF55&", "classic": "&HFFFFFF&"}
    hi_col = hi_map.get(caption_style, "&H00FFFF&")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,2,2,60,60,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for seg in transcript:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        s, e = max(0.0, seg["start"] - start), min(end - start, seg["end"] - start)
        if e - s < 0.15:
            continue
        words = seg["text"].strip().split()
        if not words:
            continue
        chunks = [words[i:i+3] for i in range(0, len(words), 3)]
        cdur = (e - s) / len(chunks)
        for ci, chunk in enumerate(chunks):
            cs = s + ci * cdur
            wdur = cdur / len(chunk)
            for wi in range(len(chunk)):
                ws, we = cs + wi * wdur, cs + (wi + 1) * wdur
                parts = []
                for idx_w, w in enumerate(chunk):
                    cw = w.replace("{", "").replace("}", "")
                    if caption_style != "classic":
                        cw = cw.upper()
                    if idx_w == wi and caption_style != "classic":
                        parts.append(f"{{\\c{hi_col}}}{cw}{{\\c&H00FFFFFF&}}")
                    else:
                        parts.append(cw)
                events.append(f"Dialogue: 0,{_ass_time(ws)},{_ass_time(we)},Default,,0,0,0,,{' '.join(parts)}")
    if not events:
        return None
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_path

def build_dynamic_face_x_expr(video, start, end):
    """Per-second face tracking: automatically snaps crop X to whichever speaker is on screen."""
    try:
        import cv2
        xml = Path("haarcascade_frontalface_default.xml")
        if not xml.exists():
            return None
        cascade = cv2.CascadeClassifier(str(xml))
        cap = cv2.VideoCapture(str(video))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0:
            cap.release()
            return None
        crop_w = min(w, int(h * 9 / 16))
        dur = max(1.0, float(end - start))
        step = 1.0
        scale_w = 320
        scale_h = max(180, int(h * scale_w / w))
        scale_x = w / float(scale_w)

        samples = []
        last_x = max(0, (w - crop_w) // 2)
        t = 0.0
        while t < dur:
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t + 0.25) * 1000.0)
            ok, frame = cap.read()
            cur_x = last_x
            if ok:
                small = cv2.resize(frame, (scale_w, scale_h))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = cascade.detectMultiScale(gray, 1.15, 3, minSize=(20, 20))
                if len(faces) > 0:
                    # Pick largest face (active speaker)
                    fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                    cx = (fx + fw / 2.0) * scale_x
                    cand_x = max(0, min(w - crop_w, int(cx - crop_w / 2.0)))
                    # Snap on camera cut, ignore tiny jitter
                    if abs(cand_x - last_x) >= 35 or not samples:
                        cur_x = cand_x
                        last_x = cand_x
            samples.append((t, cur_x))
            t += step
        cap.release()

        if not samples:
            return None

        # Merge consecutive segments with same X
        segs = []
        for t_val, x_val in samples:
            if not segs or segs[-1][2] != x_val:
                segs.append([t_val, t_val + step, x_val])
            else:
                segs[-1][1] = t_val + step

        # Build FFmpeg piecewise-constant x(t) expression
        expr = str(segs[-1][2])
        for seg_s, seg_e, seg_x in reversed(segs[:-1]):
            expr = f"if(lt(t,{seg_e:.2f}),{seg_x},{expr})"
        return {"w": crop_w, "h": h, "x_expr": expr}
    except Exception:
        return None

def export_clip(video, start, end, out, transcript, layout="auto", caption_style="viral"):
    duration = max(1, end - start)
    ass_file = build_ass_subtitles(transcript, start, end, out.with_suffix(".ass"), caption_style=caption_style, layout=layout)
    sub_filter = ""
    if ass_file and ass_file.exists():
        esc = str(ass_file).replace("\\", "/").replace(":", "\\:")
        sub_filter = f",ass='{esc}'"

    track = build_dynamic_face_x_expr(video, start, end)

    if layout == "blur":
        fc = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5,eq=brightness=-0.18[bg];"
            "[0:v]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sub_filter}[v]"
        )
        run(["ffmpeg", "-y", "-ss", str(start), "-i", str(video), "-t", str(duration),
             "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "0",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])
    else:
        # Auto-Frame & Single 9:16 both use real-time shot-by-shot face tracking
        if track:
            vf = f"crop={track['w']}:{track['h']}:'{track['x_expr']}':0,scale=1080:1920{sub_filter}"
        else:
            vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920" + sub_filter
        run(["ffmpeg", "-y", "-ss", str(start), "-i", str(video), "-t", str(duration),
             "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-threads", "0",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)])

@app.route("/")
@login_required
def home():
    return render_template("index.html", csrf_token=get_csrf_token())

@app.route("/api/create", methods=["POST"])
@login_required
def create():
    if not verify_csrf():
        return jsonify(error="Invalid CSRF token. Refresh page and try again."), 403
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not re.match(r"^https?://(www\.)?(youtube\.com|youtu\.be)/", url):
        return jsonify(error="Valid YouTube URL enter karein."), 400
    job = uuid.uuid4().hex[:10]
    folder = WORK / job
    folder.mkdir()
    try:
        video, title = download_video(url, folder)
        duration = get_duration(video)
        count = int(data.get("count", 5))
        clip_len = int(data.get("clip_len", 35))
        layout = data.get("layout", "auto")
        caption_style = data.get("caption_style", "viral")
        transcript = try_transcribe(video, folder)
        segments, mode = choose_highlights(transcript, duration, count, clip_len)
        outputs = []
        for i, (start, end) in enumerate(segments, 1):
            name = f"clip_{i:02d}.mp4"
            export_clip(video, start, end, folder / name, transcript, layout=layout, caption_style=caption_style)
            outputs.append({"name": name, "url": f"/files/{job}/{name}", "preview_url": f"/files/{job}/{name}?preview=1", "start": round(start), "end": round(end), "score": max(82, 98 - (i * 2)), "layout": layout})
        return jsonify(job=job, title=title, duration=round(duration), mode=mode,
                       transcript_available=bool(transcript), clips=outputs)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=str(e)), 500

@app.route("/files/<job>/<path:name>")
@login_required
def files(job, name):
    return send_from_directory(WORK / job, name, as_attachment=(request.args.get("preview") != "1"))

import os, shutil, uuid

_orig_run = run
def run(cmd, *args, **kwargs):
    if isinstance(cmd, (list, tuple)) and len(cmd) > 0 and cmd[0] == "yt-dlp":
        for _a in cmd:
            if isinstance(_a, str) and _a.startswith("local://"):
                _src = Path("work") / _a.replace("local://", "")
                if "-o" in cmd:
                    _out_tpl = cmd[cmd.index("-o") + 1]
                    _dst = Path(_out_tpl.replace("%(ext)s", "mp4"))
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_src, _dst)
                    return ""
        _extra = [
            "--no-check-certificates",
            "--geo-bypass",
            "--extractor-args", "youtube:player_client=tv_embedded,android_vr,ios,mweb",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
        cmd = ["yt-dlp"] + _extra + list(cmd[1:])
    return _orig_run(cmd, *args, **kwargs)

@app.route("/upload_source", methods=["POST"])
def upload_source():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"error": "No video selected"}), 400
    work_dir = Path("work")
    work_dir.mkdir(parents=True, exist_ok=True)
    fname = f"uploaded_{uuid.uuid4().hex[:10]}.mp4"
    f.save(str(work_dir / fname))
    return jsonify({"url": f"local://{fname}"})

if __name__ == "__main__":
import os, shutil, subprocess, uuid
from pathlib import Path

_orig_sp_run = subprocess.run
def _safe_sp_run(cmd, *args, **kwargs):
    if isinstance(cmd, (list, tuple)) and len(cmd) > 0 and cmd[0] == "yt-dlp":
        for _a in cmd:
            if isinstance(_a, str) and _a.startswith("local://"):
                _src = Path("work") / _a.replace("local://", "")
                if "-o" in cmd:
                    _out_tpl = cmd[cmd.index("-o") + 1]
                    _dst = Path(_out_tpl.replace("%(ext)s", "mp4"))
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(_src, _dst)
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _orig_sp_run(cmd, *args, **kwargs)
subprocess.run = _safe_sp_run

def _upload_local_mp4():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"error": "No video selected"}), 400
    work_dir = Path("work")
    work_dir.mkdir(parents=True, exist_ok=True)
    fname = f"uploaded_{uuid.uuid4().hex[:10]}.mp4"
    f.save(str(work_dir / fname))
    return jsonify({"url": f"local://{fname}"})

if "upload_local_mp4" not in app.view_functions:
    app.add_url_rule("/upload_local_mp4", "upload_local_mp4", _upload_local_mp4, methods=["POST"])

_UPLOAD_WIDGET = """
<script>
(function(){
  const inp = document.querySelector('input[type="text"], input[type="url"]');
  if(!inp || document.getElementById('local-vid-up')) return;
  const anchor = document.getElementById('yt-thumb-box') || inp;
  const wrap = document.createElement('div');
  wrap.innerHTML = '<div style="margin:10px 0;"><label for="local-vid-up" style="display:block;text-align:center;padding:12px;border-radius:10px;border:2px dashed #8b5cf6;background:rgba(139,92,246,0.18);color:#e9d5ff;font-weight:600;font-size:13px;cursor:pointer;">📁 Phone Se Video Upload Karein (MP4)</label><input id="local-vid-up" type="file" accept="video/*" style="display:none;"><div id="local-vid-status" style="display:none;font-size:12px;margin-top:6px;"></div></div>';
  anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
  const fileInp = document.getElementById('local-vid-up');
  const st = document.getElementById('local-vid-status');
  fileInp.addEventListener('change', async function(){
    if(!this.files || !this.files[0]) return;
    st.style.display = 'block'; st.style.color = '#fbbf24';
    st.textContent = '⏳ Video upload ho rahi hai, thoda rukhein...';
    const fd = new FormData(); fd.append('video', this.files[0]);
    try {
      const res = await fetch('/upload_local_mp4', { method: 'POST', body: fd });
      const data = await res.json();
      if(data.url){
        inp.value = data.url;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        st.style.color = '#34d399';
        st.textContent = '✅ Video ready (' + this.files[0].name + ') — Ab "Get clips in 1 click" dabayein!';
      } else { st.style.color = '#f87171'; st.textContent = '❌ Upload fail'; }
    } catch(e) { st.style.color = '#f87171'; st.textContent = '❌ Error: ' + e.message; }
  });
})();
</script>
"""

@app.after_request
def _inject_ui(response):
    if response.content_type and "text/html" in response.content_type:
        html = response.get_data(as_text=True)
        if "</body>" in html and "local-vid-up" not in html:
            response.set_data(html.replace("</body>", _UPLOAD_WIDGET + "</body>", 1))
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    
