import os
import re
import json
import time
import uuid
import shutil
import subprocess
import urllib.request
from pathlib import Path
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    send_from_directory,
)
import cv2

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "clipforge_ai_pro_secret_2026")
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB max upload

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "work"
OUTPUT_DIR = BASE_DIR / "output"
WORK_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CASCADE_PATH = BASE_DIR / "haarcascade_frontalface_default.xml"
if not CASCADE_PATH.exists():
    CASCADE_PATH = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
FACE_CASCADE = cv2.CascadeClassifier(str(CASCADE_PATH))


def load_users():
    cfg_path = BASE_DIR / "auth_config.json"
    if not cfg_path.exists():
        return {"admin": "admin123"}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if "username" in data and "password" in data:
                return {str(data["username"]): str(data["password"])}
            if "users" in data:
                u = data["users"]
                if isinstance(u, dict):
                    return {str(k): str(v) for k, v in u.items()}
                if isinstance(u, list):
                    res = {}
                    for item in u:
                        if isinstance(item, dict) and "username" in item and "password" in item:
                            res[str(item["username"])] = str(item["password"])
                    if res:
                        return res
            simple = {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int))}
            if simple:
                return simple
    except Exception:
        pass
    return {"admin": "admin123"}


def cleanup_old_files(max_age_sec=3600):
    now = time.time()
    for folder in (WORK_DIR, OUTPUT_DIR):
        try:
            for item in folder.iterdir():
                if now - item.stat().st_mtime > max_age_sec:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
        except Exception:
            pass


def run_cmd(cmd, timeout=300):
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Command failed").strip()
        raise RuntimeError(err[-600:])
    return proc.stdout


def extract_video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([0-9A-Za-z_-]{11})", url or "")
    return m.group(1) if m else None


def fallback_piped_download(url, out_mp4):
    vid = extract_video_id(url)
    if not vid:
        return False
    apis = [
        f"https://pipedapi.kavin.rocks/streams/{vid}",
        f"https://pipedapi.adminforge.de/streams/{vid}",
        f"https://api.piped.private.coffee/streams/{vid}",
        f"https://pipedapi.r4fo.com/streams/{vid}",
    ]
    for api in apis:
        try:
            req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            v_streams = [s for s in data.get("videoStreams", []) if "mp4" in s.get("mimeType", "")]
            a_streams = [
                s for s in data.get("audioStreams", [])
                if "mp4" in s.get("mimeType", "") or "m4a" in s.get("mimeType", "")
            ]
            prog = [s for s in v_streams if not s.get("videoOnly")]
            out_mp4.parent.mkdir(parents=True, exist_ok=True)
            if prog:
                best = sorted(
                    prog,
                    key=lambda x: int(re.sub(r"\D", "", str(x.get("quality", "0"))) or 0),
                    reverse=True,
                )[0]
                run_cmd(["ffmpeg", "-y", "-i", best["url"], "-c", "copy", str(out_mp4)], timeout=180)
                if out_mp4.exists() and out_mp4.stat().st_size > 50000:
                    return True
            elif v_streams and a_streams:
                v_best = sorted(
                    v_streams,
                    key=lambda x: int(re.sub(r"\D", "", str(x.get("quality", "0"))) or 0),
                    reverse=True,
                )[0]
                a_best = sorted(a_streams, key=lambda x: int(x.get("bitrate", 0)), reverse=True)[0]
                run_cmd(
                    ["ffmpeg", "-y", "-i", v_best["url"], "-i", a_best["url"], "-c", "copy", str(out_mp4)],
                    timeout=180,
                )
                if out_mp4.exists() and out_mp4.stat().st_size > 50000:
                    return True
        except Exception:
            continue
    return False


def download_source_video(url, job_dir):
    out_mp4 = job_dir / "source.mp4"
    if url.startswith("local://"):
        src = WORK_DIR / url.replace("local://", "").strip()
        if not src.exists():
            raise RuntimeError("Uploaded video nahi mili — kripya dobara upload karein.")
        shutil.copy2(src, out_mp4)
        return out_mp4

    extra_args = [
        "--no-check-certificates",
        "--geo-bypass",
        "--no-playlist",
        "--extractor-args",
        "youtube:player_client=tv_embedded,android_vr,ios,mweb",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    ]
    ck_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if ck_env:
        ck_path = Path("/tmp/yt_cookies.txt")
        ck_path.write_text(ck_env, encoding="utf-8")
        extra_args.extend(["--cookies", str(ck_path)])

    cmd = [
        "yt-dlp",
        *extra_args,
        "-f",
        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(out_mp4),
        url,
    ]
    try:
        run_cmd(cmd, timeout=240)
        if out_mp4.exists() and out_mp4.stat().st_size > 50000:
            return out_mp4
    except Exception as e:
        if fallback_piped_download(url, out_mp4):
            return out_mp4
        raise RuntimeError(
            f"YouTube cloud server block: {str(e)[:180]} | Neeche 'Phone Se Video Upload Karein (MP4)' button se video select karein!"
        )
    return out_mp4


def get_video_info(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    raw = run_cmd(cmd, timeout=30)
    info = json.loads(raw)
    stream = (info.get("streams") or [{}])[0]
    w = int(stream.get("width") or 1280)
    h = int(stream.get("height") or 720)
    dur = float(stream.get("duration") or (info.get("format") or {}).get("duration") or 60.0)
    return w, h, max(dur, 1.0)


def fetch_transcript_segments(url):
    vid = extract_video_id(url)
    if not vid:
        return []
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        langs = ["hi", "en", "hi-IN", "en-US", "en-GB"]
        try:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(vid, languages=langs)
            return [
                {"start": float(s.start), "duration": float(s.duration), "text": str(s.text)}
                for s in fetched
            ]
        except Exception:
            raw = YouTubeTranscriptApi.get_transcript(vid, languages=langs)
            return [
                {"start": float(s["start"]), "duration": float(s.get("duration", 2.0)), "text": str(s["text"])}
                for s in raw
            ]
    except Exception:
        return []


def pick_clip_windows(duration, num_clips, clip_len, transcript):
    num_clips = max(1, min(int(num_clips), 15))
    clip_len = max(10, min(int(clip_len), 120))
    if duration <= clip_len:
        return [(0.0, duration)]

    hooks = ("?", "!", "secret", "kaise", "kyun", "why", "how", "truth", "never", "best", "money", "paisa")
    candidates = []
    if transcript:
        for seg in transcript:
            st = seg["start"]
            if st + clip_len <= duration:
                txt = seg["text"].lower()
                score = sum(2 for h in hooks if h in txt) + min(len(txt) / 20.0, 3.0)
                candidates.append((score, st))
        candidates.sort(key=lambda x: x[0], reverse=True)

    chosen = []
    for _, st in candidates:
        if all(abs(st - c) >= clip_len * 0.75 for c in chosen):
            chosen.append(st)
            if len(chosen) >= num_clips:
                break

    if len(chosen) < num_clips:
        usable = max(duration - clip_len, 1.0)
        step = usable / max(num_clips, 1)
        for i in range(num_clips):
            st = round(min(i * step, usable), 2)
            if all(abs(st - c) >= clip_len * 0.5 for c in chosen):
                chosen.append(st)
            if len(chosen) >= num_clips:
                break

    chosen.sort()
    return [(st, min(st + clip_len, duration)) for st in chosen[:num_clips]]


def build_ass_subtitles(transcript, start_t, end_t, style_name, ass_path):
    style_key = (style_name or "off").lower().strip()
    if style_key in ("off", "none", "") or not transcript:
        return None

    palette = {
        "yellow": ("&H0000FFFF", "&H00FFFFFF"),
        "viral yellow": ("&H0000FFFF", "&H00FFFFFF"),
        "purple": ("&H00FF66C4", "&H00FFFFFF"),
        "purple pop": ("&H00FF66C4", "&H00FFFFFF"),
        "green": ("&H0050FF50", "&H00FFFFFF"),
        "green bold": ("&H0050FF50", "&H00FFFFFF"),
        "white": ("&H00FFFFFF", "&H00E0E0E0"),
        "classic white": ("&H00FFFFFF", "&H00E0E0E0"),
    }
    hi_color, base_color = palette.get(style_key, ("&H0000FFFF", "&H00FFFFFF"))

    def fmt_ts(sec):
        sec = max(0.0, sec)
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int(round((sec - int(sec)) * 100))
        if cs >= 100:
            cs = 99
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    events = []
    for seg in transcript:
        s0 = seg["start"]
        s1 = s0 + max(seg.get("duration", 2.0), 0.5)
        if s1 <= start_t or s0 >= end_t:
            continue
        rel0 = max(0.0, s0 - start_t)
        rel1 = min(end_t - start_t, s1 - start_t)
        words = [w for w in re.sub(r"\s+", " ", seg["text"]).strip().split(" ") if w]
        if not words or rel1 <= rel0:
            continue
        chunks = [words[i : i + 4] for i in range(0, len(words), 4)]
        chunk_dur = (rel1 - rel0) / len(chunks)
        for ci, ch in enumerate(chunks):
            c0 = rel0 + ci * chunk_dur
            w_dur = chunk_dur / len(ch)
            for wi in range(len(ch)):
                w0 = c0 + wi * w_dur
                w1 = w0 + w_dur
                styled_words = []
                for idx, w in enumerate(ch):
                    clean_w = w.replace("{", "").replace("}", "").replace("\\", "")
                    if idx == wi:
                        styled_words.append(f"{{\\c{hi_color}\\b1}}{clean_w}{{\\c{base_color}}}")
                    else:
                        styled_words.append(clean_w)
                line = " ".join(styled_words)
                events.append(f"Dialogue: 0,{fmt_ts(w0)},{fmt_ts(w1)},Default,,0,0,0,,{line}")

    if not events:
        return None

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,68,{base_color},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return ass_path


def compute_dynamic_face_crop_x(video_path, start_t, dur, src_w, src_h):
    crop_w = min(src_w, int(round(src_h * 9.0 / 16.0)))
    if crop_w >= src_w - 4:
        return "0", crop_w

    max_x = max(0, src_w - crop_w)
    center_x = max_x // 2
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return str(center_x), crop_w

    scale = 320.0 / float(src_w)
    xs = []
    steps = max(1, int(dur))
    for sec in range(steps):
        cap.set(cv2.CAP_PROP_POS_MSEC, (start_t + sec + 0.3) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            xs.append(xs[-1] if xs else center_x)
            continue
        small = cv2.resize(frame, (320, max(180, int(src_h * scale))))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        faces = FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=4, minSize=(24, 24))
        if len(faces) > 0:
            fx, _, fw, _ = max(faces, key=lambda b: b[2] * b[3])
            face_cx = (fx + fw / 2.0) / scale
            target_x = int(max(0, min(max_x, round(face_cx - crop_w / 2.0))))
            xs.append(target_x)
        else:
            xs.append(xs[-1] if xs else center_x)
    cap.release()

    if not xs or len(set(xs)) == 1:
        return str(xs[0] if xs else center_x), crop_w

    # Build piecewise per-second expression for FFmpeg crop filter
    segments = []
    seg_start = 0
    for i in range(1, len(xs)):
        if xs[i] != xs[seg_start]:
            segments.append((i, xs[seg_start]))
            seg_start = i
    last_val = xs[seg_start]

    expr = str(last_val)
    for end_sec, val in reversed(segments):
        expr = f"if(lt(t\\,{end_sec})\\,{val}\\,{expr})"
    return expr, crop_w


def render_single_clip(video_path, start_t, end_t, layout, caption_style, transcript, out_file):
    dur = max(1.0, end_t - start_t)
    src_w, src_h, _ = get_video_info(video_path)
    layout_key = (layout or "auto").lower().strip()

    ass_file = out_file.with_suffix(".ass")
    has_ass = build_ass_subtitles(transcript, start_t, end_t, caption_style, ass_file)

    if "blur" in layout_key:
        vf = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[0:v]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        if has_ass:
            esc_ass = str(ass_file).replace("\\", "/").replace(":", "\\:")
            vf += f",ass='{esc_ass}'"
        vf += "[vout]"
        cmd = [
            "ffmpeg", "-y", "-threads", "2",
            "-ss", f"{start_t:.2f}", "-t", f"{dur:.2f}",
            "-i", str(video_path),
            "-filter_complex", vf,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_file),
        ]
    else:
        if "single" in layout_key or "9:16" in layout_key:
            crop_w = min(src_w, int(round(src_h * 9.0 / 16.0)))
            x_expr = str(max(0, (src_w - crop_w) // 2))
        else:
            x_expr, crop_w = compute_dynamic_face_crop_x(video_path, start_t, dur, src_w, src_h)

        vf = f"crop={crop_w}:{src_h}:{x_expr}:0,scale=1080:1920:flags=bilinear"
        if has_ass:
            esc_ass = str(ass_file).replace("\\", "/").replace(":", "\\:")
            vf += f",ass='{esc_ass}'"

        cmd = [
            "ffmpeg", "-y", "-threads", "2",
            "-ss", f"{start_t:.2f}", "-t", f"{dur:.2f}",
            "-i", str(video_path),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_file),
        ]

    run_cmd(cmd, timeout=240)
    if ass_file.exists():
        ass_file.unlink(missing_ok=True)


_UPLOAD_WIDGET = """
<script>
(function(){
  const inp = document.querySelector('input[type="text"], input[type="url"]');
  if(!inp
