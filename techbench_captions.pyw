"""
Tech Bench Auto-Captioner (GUI) - karaoke style
-----------------------------------------------
White captions, the word being spoken highlighted, thick black outline,
burned into a COPY of your video (original is never touched).

New in 1.1.0: review and edit the captions before rendering, and saved
transcripts so restyling / re-rendering doesn't re-run Whisper.

Double-click this file to open the app (.pyw = no console window).

Needs:  pip install faster-whisper
        a current ffmpeg   (Windows: winget install Gyan.FFmpeg)
"""

__version__ = "1.1.0"

import copy
import difflib
import fnmatch
import glob
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

# huggingface_hub: no console progress bars (this is a windowed app) and no symlink warning
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# Helps Whisper spell your jargon correctly. Add your own terms!
JARGON_PROMPT = (
    "Tech Bench. HDMI, DisplayPort, NVMe, SSD, RAM, GPU, CPU, Ryzen, Intel, "
    "NVIDIA, RTX, Raspberry Pi, Linux, Windows, BIOS, USB-C, Wi-Fi, benchmark."
)

DEFAULT_HIGHLIGHT = "#3DFF3D"  # green
FONT_CHOICES = ["Arial Black", "Impact", "Segoe UI Black", "Bahnschrift", "Arial", "Verdana"]

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hides ffmpeg console on Windows

# Set from the app's "ffmpeg location" box when you press Start
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

CPU_ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
HDR_TRANSFERS = ("smpte2084", "arib-std-b67")  # PQ and HLG
# Convert HDR to normal SDR before the captions are drawn (otherwise colours look washed out)
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,")
# ffmpeg messages that are harmless noise and would bury real errors
LOG_NOISE = re.compile(r"embedded font|No charmap autodetected|fontconfig", re.I)


APP_NAME = "TechBenchCaptions"
IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""

# First-run downloads. ffmpeg: the official-site-linked Windows build from gyan.dev
# (GPL, static). The speech models come from Hugging Face (Systran's faster-whisper conversions).
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA256_URL = FFMPEG_URL + ".sha256"
FFMPEG_DOWNLOAD_MB = 100  # approximate; only used in the consent prompt
MODEL_REPO = "Systran/faster-whisper-{}"
MODEL_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
MODEL_MB = {"tiny.en": 75, "base.en": 145, "small.en": 485, "medium.en": 1530, "large-v3": 3090}


class DownloadCancelled(Exception):
    pass


def app_data_dir():
    """Where downloaded ffmpeg, speech models and the crash log live (per user, no admin needed)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    d = Path(base) / APP_NAME
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        d = Path(tempfile.gettempdir()) / APP_NAME
        d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(rel):
    """Path to a bundled data file (works from source and from a PyInstaller build)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def ensure_std_streams():
    """Windowed apps (pythonw / PyInstaller --windowed) have no console, so sys.stdout and
    sys.stderr are None, which crashes any library that prints. Give them somewhere to write."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def log_crash(text):
    try:
        with open(app_data_dir() / "crash.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f"  v{__version__}\n{text}\n\n")
    except Exception:
        pass


# ----- ffmpeg: find a usable one, or download one -----

def managed_ffmpeg_dir():
    return app_data_dir() / "ffmpeg"


def _run_text(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW).stdout


def ffmpeg_has_filter(path, name):
    try:
        out = _run_text([path, "-hide_banner", "-filters"])
    except Exception:
        return False
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None


def ffmpeg_ok(path):
    """True if this ffmpeg is modern enough for us: it can decode H.265 and has the 'ass' filter.
    (An ancient ffmpeg from 2013 hiding on someone's PATH fails this check.)"""
    try:
        decoders = _run_text([path, "-hide_banner", "-decoders"])
    except Exception:
        return False
    return "hevc" in decoders.lower() and ffmpeg_has_filter(path, "ass")


def ffmpeg_candidates():
    c = [str(managed_ffmpeg_dir() / f"ffmpeg{EXE}")]
    if getattr(sys, "frozen", False):  # an ffmpeg folder next to the exe, if someone put one there
        c.append(str(Path(sys.executable).parent / "ffmpeg" / f"ffmpeg{EXE}"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        c += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                    "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True)
        c += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    c += [r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
          r"C:\ffmpeg\bin\ffmpeg.exe",
          os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe")]
    on_path = shutil.which("ffmpeg")
    if on_path:
        c.append(on_path)
    return c


def find_ffmpeg():
    """First working, modern ffmpeg we can find; '' if there isn't one (it can be downloaded)."""
    seen = set()
    for c in ffmpeg_candidates():
        if c in seen or not os.path.isfile(c):
            continue
        seen.add(c)
        if ffmpeg_ok(c):
            return c
    return ""


def download_file(url, dest, progress=None, cancel=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def install_ffmpeg(dest_dir, progress=None, cancel=None, log=lambda m: None,
                   url=FFMPEG_URL, sha_url=FFMPEG_SHA256_URL, verify=ffmpeg_ok):
    """
    Download ffmpeg, verify its checksum, extract only ffmpeg/ffprobe (+ the licence files) and
    move them into dest_dir. Nothing is left half-installed if anything goes wrong.
    Returns the path to ffmpeg.exe.
    """
    dest_dir = Path(dest_dir)
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="dl_", dir=dest_dir.parent))
    try:
        zpath = tmp / "ffmpeg.zip"
        download_file(url, zpath, progress, cancel)

        expected = None
        try:
            m = re.search(r"\b[0-9a-fA-F]{64}\b", fetch_text(sha_url))
            expected = m.group(0).lower() if m else None
        except Exception as e:
            log(f"Couldn't fetch the checksum ({e}); continuing without verifying it.")
        if expected:
            if sha256_of(zpath) != expected:
                raise RuntimeError("The download was corrupted (checksum mismatch). Please try again.")
            log("Checksum verified.")

        out = tmp / "out"
        out.mkdir()
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.replace("\\", "/").split("/")
                name = parts[-1]
                wanted = ((name in ("ffmpeg.exe", "ffprobe.exe") and len(parts) >= 2 and parts[-2] == "bin")
                          or (name in ("LICENSE", "README.txt") and len(parts) == 2))
                if wanted:  # fixed output names only, so odd paths inside the zip can't escape
                    with z.open(info) as src, open(out / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        if not (out / "ffmpeg.exe").exists():
            raise RuntimeError("ffmpeg.exe wasn't found in the download.")
        if not verify(str(out / "ffmpeg.exe")):
            raise RuntimeError("The downloaded ffmpeg doesn't run correctly on this PC.")

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.move(str(out), str(dest_dir))
        return dest_dir / "ffmpeg.exe"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----- speech model: find it locally, or download it with progress -----

def model_cache_dirs():
    return [str(app_data_dir() / "models"), None]  # ours first, then the default Hugging Face cache


def find_cached_model(size):
    """Path to a fully downloaded model, or None."""
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        return None
    for cache in model_cache_dirs():
        try:
            p = snapshot_download(MODEL_REPO.format(size), cache_dir=cache,
                                  allow_patterns=MODEL_PATTERNS, local_files_only=True)
        except Exception:
            continue
        if p and (Path(p) / "model.bin").exists():  # an interrupted download has no model.bin
            return str(p)
    return None


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def model_total_bytes(repo, size):
    approx = MODEL_MB.get(size, 500) * 1024 * 1024
    try:
        from huggingface_hub import HfApi
        info = HfApi().model_info(repo, files_metadata=True)
        total = sum((x.size or 0) for x in info.siblings
                    if any(fnmatch.fnmatch(x.rfilename, pat) for pat in MODEL_PATTERNS))
        return total or approx
    except Exception:
        return approx


def download_model(size, progress=None, cancel=None):
    """
    Download a speech model into our own folder. Progress is measured by watching the files grow,
    so it doesn't depend on huggingface_hub's console progress bars.
    """
    from huggingface_hub import snapshot_download
    repo = MODEL_REPO.format(size)
    cache = str(app_data_dir() / "models")
    total = model_total_bytes(repo, size)
    result = {}

    def work():
        try:
            result["path"] = snapshot_download(repo, cache_dir=cache, allow_patterns=MODEL_PATTERNS)
        except BaseException as e:  # reported back to the caller below
            result["error"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    folder = Path(cache) / ("models--" + repo.replace("/", "--"))
    while t.is_alive():
        t.join(0.5)
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()  # the download thread is a daemon; partial files resume next time
        if progress:
            progress(min(_dir_bytes(folder), total), total)
    if "error" in result:
        raise result["error"]
    return result["path"]


def cuda_libs_available():
    """
    True if the CUDA libraries faster-whisper needs (cuBLAS + cuDNN) can be loaded. If they're
    missing, trying the GPU can crash the whole program, so 'auto' only chooses the GPU when
    they're present. (Not an issue on a machine without an NVIDIA GPU: it never gets this far.)
    """
    if not IS_WINDOWS:
        return True
    import ctypes

    def can_load(names):
        for n in names:
            try:
                ctypes.WinDLL(n, winmode=0)  # winmode=0: normal Windows search, including PATH
                return True
            except (OSError, AttributeError):
                continue
        return False

    return (can_load(("cublas64_12.dll", "cublas64_11.dll"))
            and can_load(("cudnn_ops64_9.dll", "cudnn_ops_infer64_8.dll", "cudnn64_8.dll")))


def patch_pyav_compat():
    """
    faster-whisper (up to 1.2.x) opens audio with av.open(..., metadata_errors="ignore"), but
    PyAV 19 removed that argument, which makes every transcription fail with
    "open() got an unexpected keyword argument 'metadata_errors'".
    This wraps av.open so the removed arguments are dropped when PyAV rejects them.
    """
    try:
        import av
    except Exception:
        return False
    if getattr(av.open, "_tb_patched", False):
        return True
    original = av.open

    def compat_open(*args, **kwargs):
        while True:  # Python's error names only the first bad argument, so retry until all are gone
            try:
                return original(*args, **kwargs)
            except TypeError as e:
                removed = [k for k in ("metadata_errors", "metadata_encoding")
                           if k in kwargs and k in str(e)]
                if not removed:
                    raise
                for k in removed:
                    kwargs.pop(k)

    compat_open._tb_patched = True
    av.open = compat_open
    return True

# ---------- transcript data ----------
# A transcript is a list of segments:
#   {"start": float, "end": float, "words": [{"word": str, "start": float, "end": float}, ...]}
# It is stored as plain JSON so it can be cached, edited and re-rendered.

class Word:
    __slots__ = ("word", "start", "end")

    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


def seg_text(seg):
    return " ".join(w["word"] for w in seg["words"])


def realign(old_words, tokens):
    """
    Apply edited text to a segment's timed words. Unchanged words keep their
    original timing; changed/added words get timing spread over the same span.
    """
    sm = difflib.SequenceMatcher(a=[w["word"].lower() for w in old_words],
                                 b=[t.lower() for t in tokens], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        new = tokens[j1:j2]
        if tag == "equal":
            for k, t in enumerate(new):
                w = dict(old_words[i1 + k])
                w["word"] = t
                out.append(w)
        elif tag == "delete":
            continue
        else:  # replace or insert
            if tag == "replace" and len(new) == i2 - i1:
                for k, t in enumerate(new):  # same word count: keep each word's own timing
                    w = dict(old_words[i1 + k])
                    w["word"] = t
                    out.append(w)
                continue
            if tag == "replace":
                t0, t1 = old_words[i1]["start"], old_words[i2 - 1]["end"]
            else:
                if out:
                    t0 = out[-1]["end"]
                elif i1 < len(old_words):
                    t0 = old_words[i1]["start"]
                else:
                    t0 = old_words[-1]["end"]
                t1 = old_words[i1]["start"] if i1 < len(old_words) else t0
                t1 = max(t0, t1)
                needed = 0.12 * len(new)
                if out and t1 - t0 < needed:
                    # no room: borrow time from the end of the previous word
                    prev = out[-1]
                    steal = min(needed - (t1 - t0), max(0.0, (prev["end"] - prev["start"]) - 0.08))
                    t0 -= steal
                    prev["end"] = t0
            weights = [max(1, len(t)) for t in new]
            total = sum(weights)
            span = max(t1 - t0, 0.12 * len(new))
            cur = t0
            for t, wt in zip(new, weights):
                d = span * wt / total
                out.append({"word": t, "start": cur, "end": cur + d})
                cur += d
    return out


def apply_text(seg, text):
    """Replace a segment's text with edited text (re-aligning word timings)."""
    tokens = text.split()
    if not tokens:
        seg["words"] = []
        return
    seg["words"] = realign(seg["words"], tokens)
    seg["start"] = seg["words"][0]["start"]
    seg["end"] = seg["words"][-1]["end"]


def flatten_words(segments):
    """All words in order, with timings made strictly increasing so highlights never overlap."""
    words = []
    for seg in segments:
        for w in seg["words"]:
            text = w["word"].strip()
            if text:
                words.append(Word(text, float(w["start"]), float(w["end"])))
    for i, w in enumerate(words):
        if i and w.start < words[i - 1].start + 0.04:
            w.start = words[i - 1].start + 0.04
        if w.end < w.start + 0.04:
            w.end = w.start + 0.04
    return words


def cache_path(video):
    video = Path(video)
    return video.with_name(video.stem + ".captions.json")


def needs_transcription(files, reuse):
    """True if at least one video has no usable saved transcript (so the Whisper model is needed)."""
    for f in files:
        c = cache_path(f)
        if not (reuse and c.exists() and load_cache(c) is not None):
            return True
    return False


def save_cache(path, segments, model):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "app": __version__, "model": model, "segments": segments},
                  f, ensure_ascii=False, indent=1)


def load_cache(path):
    """Return the segments from a saved transcript, or None if missing/invalid."""
    try:
        with open(path, encoding="utf-8") as f:
            segs = json.load(f)["segments"]
        for s in segs:
            for w in s["words"]:
                w["word"], float(w["start"]), float(w["end"])
        return segs
    except Exception:
        return None


# ---------- subtitle helpers ----------

def fmt_srt(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def fmt_ass(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def chunk_words(words, n):
    for i in range(0, len(words), n):
        yield words[i:i + n]


def write_srt(words, path, n):
    with open(path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunk_words(words, n), 1):
            text = " ".join(w.word for w in chunk)
            f.write(f"{i}\n{fmt_srt(chunk[0].start)} --> {fmt_srt(chunk[-1].end)}\n{text}\n\n")


def hex_to_bgr(hex_color):
    """'#RRGGBB' -> 'BBGGRR' (ASS colours are blue-green-red)."""
    h = hex_color.lstrip("#")
    return h[4:6] + h[2:4] + h[0:2]


def write_ass(words, path, opts, width, height):
    """
    One caption chunk (e.g. 4 words) is shown at a time. For each word we emit
    an event that shows the whole chunk with ONLY that word coloured.
    """
    scale = min(width, height) / 1080.0
    font = opts["font"]
    size = int(opts["font_size"] * scale)
    outline = max(2, round(opts["font_size"] * 0.09 * scale))   # thick black stroke
    shadow = max(1, round(2 * scale))
    margin_v = int(110 * scale)
    margin_lr = int(60 * scale)
    bold = 0 if any(k in font.lower() for k in ("black", "impact")) else -1
    hl = hex_to_bgr(opts["highlight"])

    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        f"{bold},0,0,0,100,100,0,0,1,{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    def clean(w):
        t = w.word.replace("{", "").replace("}", "")
        return t.upper() if opts["uppercase"] else t

    chunks = list(chunk_words(words, opts["words"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for ci, chunk in enumerate(chunks):
            limit = chunks[ci + 1][0].start if ci + 1 < len(chunks) else None
            texts = [clean(w) for w in chunk]
            for i, w in enumerate(chunk):
                start = w.start
                if i + 1 < len(chunk):
                    end = chunk[i + 1].start  # hold until the next word so the highlight never flickers off
                else:
                    end = w.end
                    if limit is not None:
                        end = min(end, limit)
                end = max(end, start + 0.03)
                parts = []
                for j, t in enumerate(texts):
                    if j == i:
                        parts.append(f"{{\\1c&H{hl}&}}{t}{{\\1c&HFFFFFF&}}")
                    else:
                        parts.append(t)
                f.write(f"Dialogue: 0,{fmt_ass(start)},{fmt_ass(end)},Default,,0,0,0,,"
                        f"{' '.join(parts)}\n")


def pick_encoder():
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True,
                         text=True, creationflags=NO_WINDOW).stdout
    if "h264_nvenc" in out:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21"], "NVIDIA NVENC"
    if "h264_qsv" in out:
        return ["-c:v", "h264_qsv", "-global_quality", "21"], "Intel QSV"
    if "h264_amf" in out:
        return ["-c:v", "h264_amf", "-quality", "balanced"], "AMD AMF"
    return CPU_ENCODER, "CPU (x264)"


def probe_video(video):
    """Return (width, height, duration), accounting for phone rotation. Falls back to 1920x1080."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:stream_side_data=rotation:format=duration",
             "-of", "json", str(video)],
            capture_output=True, text=True, creationflags=NO_WINDOW)
        data = json.loads(r.stdout)
        st = data["streams"][0]
        w, h = int(st["width"]), int(st["height"])
        rot = 0
        for sd in st.get("side_data_list", []):
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) % 180 == 90:
            w, h = h, w
        dur = float(data.get("format", {}).get("duration", 0)) or None
        return w, h, dur
    except Exception:
        return 1920, 1080, None


def probe_color(video):
    """Return (pix_fmt, color_transfer) of the first video stream ('' when unknown)."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt,color_transfer", "-of", "json", str(video)],
            capture_output=True, text=True, creationflags=NO_WINDOW)
        st = json.loads(r.stdout)["streams"][0]
        return st.get("pix_fmt", ""), st.get("color_transfer", "")
    except Exception:
        return "", ""


def find_font_files(name):
    """Windows: locate the installed font file(s) for a font family via the registry."""
    try:
        import winreg
    except ImportError:
        return []
    base = name.strip().lower()
    allowed = {base, base + " regular", base + " bold", base + " italic", base + " bold italic"}
    fonts_dir = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
    reg_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    found = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, reg_path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    vname, vdata, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                entry = re.sub(r"\s*\((truetype|opentype)\)\s*$", "", vname, flags=re.I).lower()
                if any(n.strip() in allowed for n in entry.split("&")):
                    path = Path(vdata)
                    if not path.is_absolute():
                        path = fonts_dir / path
                    if path.exists() and path not in found:
                        found.append(path)
    return found


# ---------- caption editor window ----------

def fmt_time(t):
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


class EditorWindow(tk.Toplevel):
    """Review/edit caption lines before rendering. Result goes into holder['result']."""

    def __init__(self, app, video, segments, holder, done_event):
        super().__init__(app)
        self.app, self.video = app, video
        self.holder, self.done_event = holder, done_event
        self.segments = copy.deepcopy(segments)
        self.current = None  # index of the row being edited

        self.title(f"Edit captions - {video.name}")
        self.geometry("840x620")
        self.minsize(700, 520)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.skip)

        tip = ("Select a line, fix the text, press Enter to save it and jump to the next line. "
               "Timings of unchanged words are kept.")
        ttk.Label(self, text=tip, wraplength=780).pack(anchor="w", padx=10, pady=(10, 4))

        # caption list
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, padx=10)
        self.tree = ttk.Treeview(lf, columns=("time", "text"), show="headings", selectmode="browse")
        self.tree.heading("time", text="Time")
        self.tree.heading("text", text="Caption")
        self.tree.column("time", width=70, stretch=False)
        self.tree.column("text", width=700)
        sb = ttk.Scrollbar(lf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # edit box
        ef = ttk.LabelFrame(self, text="Edit selected line")
        ef.pack(fill="x", padx=10, pady=8)
        self.box = tk.Text(ef, height=3, wrap="word", font=("Segoe UI", 11))
        self.box.pack(fill="x", padx=8, pady=(8, 4))
        self.box.bind("<Return>", self.on_return)
        row = ttk.Frame(ef)
        row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(row, text="Save line", command=self.commit).pack(side="left")
        ttk.Button(row, text="Delete line", command=self.delete_row).pack(side="left", padx=6)
        ttk.Button(row, text="Open video to check", command=self.open_video).pack(side="left")

        # find / replace
        fr = ttk.LabelFrame(self, text="Find and replace (all lines)")
        fr.pack(fill="x", padx=10, pady=(0, 8))
        self.find_var, self.repl_var = tk.StringVar(), tk.StringVar()
        self.case_var = tk.BooleanVar(value=False)
        ttk.Label(fr, text="Find:").grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(fr, textvariable=self.find_var, width=22).grid(row=0, column=1)
        ttk.Label(fr, text="Replace with:").grid(row=0, column=2, padx=8)
        ttk.Entry(fr, textvariable=self.repl_var, width=22).grid(row=0, column=3)
        ttk.Checkbutton(fr, text="Match case", variable=self.case_var).grid(row=0, column=4, padx=8)
        ttk.Button(fr, text="Replace all", command=self.replace_all).grid(row=0, column=5, padx=4)

        # finish
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=(0, 10))
        self.count_label = ttk.Label(bf, text="")
        self.count_label.pack(side="left")
        ttk.Button(bf, text="Skip this video", command=self.skip).pack(side="right")
        ttk.Button(bf, text="Done - continue", command=self.done).pack(side="right", padx=8)

        self.refresh(select=0)
        self.lift()
        self.focus_force()

    # --- list handling ---
    def refresh(self, select=None):
        self.current = None  # don't let selection events commit stale text
        self.tree.delete(*self.tree.get_children())
        for i, seg in enumerate(self.segments):
            self.tree.insert("", "end", iid=str(i), values=(fmt_time(seg["start"]), seg_text(seg)))
        self.count_label.config(text=f"{len(self.segments)} caption lines")
        self.box.delete("1.0", "end")
        if select is not None and self.segments:
            select = max(0, min(select, len(self.segments) - 1))
            self.tree.selection_set(str(select))
            self.tree.see(str(select))

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.commit()
        self.current = int(sel[0])
        self.box.delete("1.0", "end")
        self.box.insert("1.0", seg_text(self.segments[self.current]))

    def commit(self):
        """Apply the edit box text to the current line (if it changed)."""
        if self.current is None:
            return
        seg = self.segments[self.current]
        text = " ".join(self.box.get("1.0", "end").split())
        if text and text != seg_text(seg):
            apply_text(seg, text)
            self.tree.item(str(self.current), values=(fmt_time(seg["start"]), seg_text(seg)))

    def on_return(self, _event):
        self.commit()
        if self.current is not None and self.current + 1 < len(self.segments):
            nxt = str(self.current + 1)
            self.tree.selection_set(nxt)
            self.tree.see(nxt)
        return "break"

    def delete_row(self):
        if self.current is None:
            return
        idx = self.current
        del self.segments[idx]
        self.refresh(select=idx)

    def replace_all(self):
        self.commit()
        find = self.find_var.get()
        if not find:
            return
        repl = self.repl_var.get()
        pattern = re.compile(re.escape(find), 0 if self.case_var.get() else re.I)
        total = 0
        for seg in self.segments:
            new_text, n = pattern.subn(lambda _m: repl, seg_text(seg))
            if n:
                total += n
                apply_text(seg, new_text)
        self.segments = [s for s in self.segments if s["words"]]
        keep = self.current
        self.refresh(select=keep if keep is not None else 0)
        messagebox.showinfo("Find and replace", f"Replaced {total} occurrence(s).", parent=self)

    def open_video(self):
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(self.video))
            else:
                subprocess.Popen(["open" if shutil.which("open") else "xdg-open", str(self.video)])
        except Exception as e:
            messagebox.showerror("Couldn't open video", str(e), parent=self)

    # --- finishing ---
    def done(self):
        self.commit()
        self.holder["result"] = [s for s in self.segments if s["words"]]
        self.finish()

    def skip(self):
        self.holder["result"] = None
        self.finish()

    def finish(self):
        self.app.editor = None
        self.done_event.set()
        self.destroy()


# ---------- the app ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Tech Bench Auto-Captioner {__version__}")
        self.geometry("760x780")
        self.minsize(680, 700)
        try:
            self.iconbitmap(resource_path(os.path.join("assets", "icon.ico")))
        except Exception:
            pass  # no icon file / not Windows: use the default

        self.files = []
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.ffmpeg_proc = None
        self.editor = None

        self.model_var = tk.StringVar(value="small.en")
        self.device_var = tk.StringVar(value="auto")
        self.words_var = tk.IntVar(value=4)
        self.font_var = tk.StringVar(value="Arial Black")
        self.size_var = tk.IntVar(value=72)
        self.upper_var = tk.BooleanVar(value=False)
        self.burn_var = tk.BooleanVar(value=True)
        self.edit_var = tk.BooleanVar(value=True)
        self.reuse_var = tk.BooleanVar(value=True)
        self.highlight = DEFAULT_HIGHLIGHT
        self.ffmpeg_var = tk.StringVar(value=find_ffmpeg())

        self.build_ui()
        self.after(100, self.poll_queue)

    def report_callback_exception(self, exc, val, tb):
        text = "".join(traceback.format_exception(exc, val, tb))
        log_crash(text)
        self.write_log("Unexpected error:\n" + text)
        try:
            messagebox.showerror("Unexpected error",
                                 f"{val}\n\nDetails were saved to:\n{app_data_dir() / 'crash.log'}")
        except Exception:
            pass

    def build_ui(self):
        pad = {"padx": 10, "pady": 5}

        # Files
        ff = ttk.LabelFrame(self, text="Videos")
        ff.pack(fill="both", padx=10, pady=(10, 5))
        self.listbox = tk.Listbox(ff, height=5, selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        btns = ttk.Frame(ff)
        btns.pack(side="right", fill="y", padx=8, pady=8)
        ttk.Button(btns, text="Add videos...", command=self.add_files).pack(fill="x", pady=2)
        ttk.Button(btns, text="Remove selected", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="Clear", command=self.clear_files).pack(fill="x", pady=2)

        # Caption style
        sf = ttk.LabelFrame(self, text="Caption style")
        sf.pack(fill="x", padx=10, pady=5)

        ttk.Label(sf, text="Font:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(sf, textvariable=self.font_var, width=18, values=FONT_CHOICES
                     ).grid(row=0, column=1, sticky="w", **pad)  # editable: type any installed font

        ttk.Label(sf, text="Size:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Spinbox(sf, from_=30, to=200, textvariable=self.size_var, width=6
                    ).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(sf, text="Highlight colour:").grid(row=1, column=0, sticky="w", **pad)
        hl = ttk.Frame(sf)
        hl.grid(row=1, column=1, sticky="w", **pad)
        self.swatch = tk.Label(hl, width=3, bg=self.highlight, relief="solid", bd=1)
        self.swatch.pack(side="left")
        ttk.Button(hl, text="Pick...", command=self.pick_color).pack(side="left", padx=6)

        ttk.Label(sf, text="Words on screen:").grid(row=1, column=2, sticky="w", **pad)
        ttk.Spinbox(sf, from_=1, to=10, textvariable=self.words_var, width=6
                    ).grid(row=1, column=3, sticky="w", **pad)

        ttk.Checkbutton(sf, text="ALL CAPS", variable=self.upper_var
                        ).grid(row=2, column=0, sticky="w", **pad)

        # Processing options
        of = ttk.LabelFrame(self, text="Processing")
        of.pack(fill="x", padx=10, pady=5)
        ttk.Label(of, text="Model:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(of, textvariable=self.model_var, width=14, state="readonly",
                     values=["tiny.en", "base.en", "small.en", "medium.en", "large-v3"]
                     ).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(of, text="Device:").grid(row=0, column=2, sticky="w", **pad)
        ttk.Combobox(of, textvariable=self.device_var, width=8, state="readonly",
                     values=["auto", "cuda", "cpu"]).grid(row=0, column=3, sticky="w", **pad)
        ttk.Checkbutton(of, text="Burn captions into a copy of the video (_captioned.mp4)",
                        variable=self.burn_var).grid(row=1, column=0, columnspan=5, sticky="w", **pad)
        ttk.Checkbutton(of, text="Review and edit captions before rendering",
                        variable=self.edit_var).grid(row=2, column=0, columnspan=5, sticky="w", **pad)
        ttk.Checkbutton(of, text="Reuse saved transcript (.captions.json) if one exists "
                                 "- skips transcription",
                        variable=self.reuse_var).grid(row=3, column=0, columnspan=5, sticky="w", **pad)
        ttk.Label(of, text="ffmpeg location:").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(of, textvariable=self.ffmpeg_var, width=52).grid(row=4, column=1, columnspan=3,
                                                                    sticky="we", **pad)
        ttk.Button(of, text="Browse...", command=self.browse_ffmpeg).grid(row=4, column=4, **pad)
        ttk.Label(of, text="Leave empty to download ffmpeg automatically (one time) when it's needed.",
                  foreground="gray").grid(row=5, column=1, columnspan=4, sticky="w", padx=10)

        # Run controls
        rf = ttk.Frame(self)
        rf.pack(fill="x", padx=10, pady=5)
        self.start_btn = ttk.Button(rf, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(rf, text="Cancel", command=self.cancel_run, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(self, maximum=100)
        self.progress.pack(fill="x", padx=10, pady=(5, 0))
        self.status = ttk.Label(self, text="Add some videos to get started.")
        self.status.pack(anchor="w", padx=10, pady=2)

        # Log
        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.log = tk.Text(lf, height=8, state="disabled", wrap="word")
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True, padx=(8, 0), pady=8)

    def pick_color(self):
        c = colorchooser.askcolor(color=self.highlight, title="Highlight colour")[1]
        if c:
            self.highlight = c
            self.swatch.config(bg=c)

    def browse_ffmpeg(self):
        p = filedialog.askopenfilename(title="Select ffmpeg.exe",
                                       filetypes=[("ffmpeg", "ffmpeg.exe"), ("All files", "*.*")])
        if p:
            self.ffmpeg_var.set(p)

    # --- file list ---
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose videos",
            filetypes=[("Video", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("All files", "*.*")])
        for p in paths:
            if p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)

    def remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
            del self.files[i]

    def clear_files(self):
        self.listbox.delete(0, "end")
        self.files.clear()

    # --- logging / queue ---
    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll_queue(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.write_log(val)
                elif kind == "status":
                    self.status.config(text=val)
                elif kind == "progress":
                    self.progress["value"] = val
                elif kind == "edit":
                    video, segments, holder, ev = val
                    self.editor = EditorWindow(self, video, segments, holder, ev)
                elif kind == "ffmpeg_path":
                    self.ffmpeg_var.set(val)
                elif kind == "done":
                    self.start_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def log_q(self, msg):
        self.q.put(("log", msg))

    # --- run control ---
    def start(self):
        if not self.files:
            messagebox.showinfo("No videos", "Add at least one video first.")
            return

        global FFMPEG, FFPROBE
        burn = self.burn_var.get()
        size = self.model_var.get()

        # --- ffmpeg: only needed to render captions into the video ---
        need_ffmpeg, ffmpeg_note = False, ""
        if burn:
            entered = self.ffmpeg_var.get().strip()
            resolved = entered if (entered and os.path.isfile(entered)) else (
                shutil.which(entered) if entered else None)
            if resolved and not ffmpeg_ok(resolved):
                ffmpeg_note = ("\n\n(The ffmpeg you selected is too old or incomplete, so a "
                               "current one will be used instead.)")
                resolved = None
            if not resolved:
                resolved = find_ffmpeg()
            if resolved:
                FFMPEG = resolved
                probe = Path(resolved).with_name("ffprobe" + Path(resolved).suffix)
                FFPROBE = str(probe) if probe.exists() else "ffprobe"
                self.ffmpeg_var.set(resolved)
                self.write_log(f"Using ffmpeg: {resolved}")
            elif IS_WINDOWS:
                need_ffmpeg = True
            else:
                messagebox.showerror("ffmpeg missing",
                                     "ffmpeg wasn't found. Install it with your package manager "
                                     "(for example: sudo apt install ffmpeg) and restart the app.")
                return

        # --- speech model: only needed if some video has to be transcribed ---
        need_model = (needs_transcription(self.files, self.reuse_var.get())
                      and find_cached_model(size) is None)

        if need_ffmpeg or need_model:
            lines = []
            if need_ffmpeg:
                lines.append(f"  - ffmpeg, to render captions into your video "
                             f"(about {FFMPEG_DOWNLOAD_MB} MB, from gyan.dev)")
            if need_model:
                mb = MODEL_MB.get(size)
                lines.append(f"  - the speech-recognition model '{size}' "
                             f"({'about %d MB, ' % mb if mb else ''}from Hugging Face)")
            msg = ("First-time setup needs to download:\n\n" + "\n".join(lines) +
                   f"\n\nThey're saved in:\n{app_data_dir()}\nand only downloaded once." +
                   ffmpeg_note + "\n\nDownload now?")
            if not messagebox.askokcancel("First-time setup", msg):
                return

        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        opts = dict(model=size, device=self.device_var.get(),
                    words=max(1, self.words_var.get()), font=self.font_var.get().strip() or "Arial Black",
                    font_size=self.size_var.get(), uppercase=self.upper_var.get(),
                    highlight=self.highlight, burn=burn,
                    edit=self.edit_var.get(), reuse=self.reuse_var.get(),
                    install_ffmpeg=need_ffmpeg)
        self.worker = threading.Thread(target=self.run_all, args=(list(self.files), opts), daemon=True)
        self.worker.start()

    def cancel_run(self):
        self.cancel.set()
        if self.editor is not None:
            self.editor.skip()
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            self.ffmpeg_proc.terminate()
        self.log_q("Cancelling...")

    # --- worker thread ---
    def run_all(self, files, opts):
        state = {"model": None, "device": None, "path": None}
        try:
            if opts.get("install_ffmpeg"):
                self.setup_ffmpeg()
            for idx, f in enumerate(files, 1):
                if self.cancel.is_set():
                    break
                video = Path(f)
                total = len(files)
                self.log_q(f"\n=== [{idx}/{total}] {video.name} ===")
                self.q.put(("progress", 0))
                cache = cache_path(video)

                # 1. transcript: reuse the saved one, or transcribe
                segments = None
                if opts["reuse"] and cache.exists():
                    segments = load_cache(cache)
                    if segments is not None:
                        self.log_q(f"Loaded saved transcript: {cache.name}")
                        self.q.put(("progress", 100))
                if segments is None:
                    segments = self.transcribe_video(state, video, opts, idx, total)
                    if segments is None or self.cancel.is_set():
                        break
                    save_cache(cache, segments, opts["model"])
                    self.log_q(f"Saved transcript: {cache.name}")

                # 2. optional review/edit
                if opts["edit"]:
                    edited = self.request_edit(video, segments)
                    if edited is None:
                        self.log_q("Skipped - nothing saved or rendered for this video.")
                        continue
                    segments = edited
                    save_cache(cache, segments, opts["model"])
                    self.log_q("Saved edited transcript.")

                # 3. outputs
                words = flatten_words(segments)
                if not words:
                    self.log_q("No speech found - nothing to caption.")
                    continue
                srt = video.with_suffix(".srt")
                write_srt(words, srt, opts["words"])
                self.log_q(f"Saved {srt}")
                if opts["burn"]:
                    self.burn(video, words, opts, idx, total)

            msg = "Cancelled." if self.cancel.is_set() else "All done!"
            self.q.put(("status", msg))
            self.log_q(msg)
        except DownloadCancelled:
            self.log_q("Download cancelled.")
            self.q.put(("status", "Cancelled."))
        except ImportError as e:
            self.log_q(f"A required component is missing ({e}). "
                       "If you're running from source, run:  pip install faster-whisper")
            self.q.put(("status", "Error - see log"))
        except Exception as e:
            self.log_q(f"ERROR: {e}")
            log_crash(traceback.format_exc())
            self.q.put(("status", "Error - see log"))
        finally:
            self.q.put(("done", None))

    def request_edit(self, video, segments):
        """Ask the main thread to open the editor, and wait until the user finishes."""
        holder = {"result": None}
        ev = threading.Event()
        self.q.put(("status", f"Edit the captions for {video.name}, then click 'Done - continue'."))
        self.q.put(("edit", (video, segments, holder, ev)))
        while not ev.wait(0.3):
            if self.cancel.is_set():
                break
        return holder["result"]

    def setup_ffmpeg(self):
        """First-run download of ffmpeg (consent was already given in start())."""
        global FFMPEG, FFPROBE
        self.q.put(("status", "Downloading ffmpeg (one time)..."))
        self.log_q(f"Downloading ffmpeg from gyan.dev (about {FFMPEG_DOWNLOAD_MB} MB, one time only)...")

        def prog(done, total):
            if total:
                self.q.put(("progress", done / total * 100))
                self.q.put(("status", f"Downloading ffmpeg: {done / 1e6:.0f} / {total / 1e6:.0f} MB"))

        try:
            exe = install_ffmpeg(managed_ffmpeg_dir(), prog, self.cancel, self.log_q)
        except DownloadCancelled:
            raise
        except Exception as e:
            raise RuntimeError(f"Couldn't download ffmpeg: {e}\n"
                               "Check your internet connection and try again, or pick an existing "
                               "ffmpeg.exe with Browse.") from e
        FFMPEG = str(exe)
        FFPROBE = str(exe.with_name("ffprobe" + EXE))
        self.q.put(("ffmpeg_path", FFMPEG))
        self.log_q(f"ffmpeg installed: {exe}")
        self.q.put(("progress", 0))

    def get_model_path(self, state, opts):
        """Folder of the downloaded speech model (downloading it first if this is the first run)."""
        if state.get("path"):
            return state["path"]
        size = opts["model"]
        path = find_cached_model(size)
        if path is None:
            mb = MODEL_MB.get(size)
            self.log_q(f"Downloading speech model '{size}'" +
                       (f" (about {mb} MB, one time only)..." if mb else "..."))
            self.q.put(("status", f"Downloading speech model '{size}'..."))

            def prog(done, total):
                if total:
                    self.q.put(("progress", done / total * 100))
                    self.q.put(("status", f"Downloading speech model '{size}': "
                                          f"{done / 1e6:.0f} / {total / 1e6:.0f} MB"))

            try:
                path = download_model(size, prog, self.cancel)
            except DownloadCancelled:
                raise
            except ImportError:
                path = size  # huggingface_hub unavailable: let faster-whisper fetch it itself
            except Exception as e:
                raise RuntimeError(f"Couldn't download the speech model: {e}\n"
                                   "Check your internet connection and try again.") from e
            self.q.put(("progress", 0))
        state["path"] = path
        return path

    def ensure_model(self, state, opts, force_cpu=False):
        if state["model"] is not None and not force_cpu:
            return
        from faster_whisper import WhisperModel
        patch_pyav_compat()

        path = self.get_model_path(state, opts)
        self.q.put(("status", "Loading the speech model..."))
        device = "cpu" if force_cpu else opts["device"]
        if device == "auto":
            device = "cpu"
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    if cuda_libs_available():
                        device = "cuda"
                    else:
                        self.log_q("NVIDIA GPU found, but the CUDA libraries (cuBLAS/cuDNN) aren't "
                                   "installed, so the CPU will be used (slower).")
            except Exception:
                pass
        compute = "float16" if device == "cuda" else "int8"
        self.log_q(f"Loading model '{opts['model']}' on {device}...")
        state["model"] = None
        state["device"] = device  # set first so a failed GPU load can fall back to the CPU
        state["model"] = WhisperModel(path, device=device, compute_type=compute)

    def transcribe_video(self, state, video, opts, idx, total):
        try:
            self.ensure_model(state, opts)
            return self.run_transcription(state["model"], video, idx, total)
        except DownloadCancelled:
            raise
        except Exception as e:
            if state.get("device") == "cuda" and not self.cancel.is_set():
                self.log_q(f"GPU failed ({e}). Retrying on CPU...")
                self.ensure_model(state, opts, force_cpu=True)
                return self.run_transcription(state["model"], video, idx, total)
            raise

    def run_transcription(self, model, video, idx, total):
        self.q.put(("status", f"Transcribing {video.name} ({idx}/{total})"))
        segs, info = model.transcribe(str(video), word_timestamps=True, vad_filter=True,
                                      initial_prompt=JARGON_PROMPT)
        self.log_q(f"Language: {info.language}, length: {info.duration:.0f}s")
        out = []
        for seg in segs:  # generator: transcription happens as we iterate
            if self.cancel.is_set():
                return None
            words = [{"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
                     for w in (seg.words or []) if w.word.strip()]
            if words:
                out.append({"start": words[0]["start"], "end": words[-1]["end"], "words": words})
            if info.duration:
                self.q.put(("progress", min(100, seg.end / info.duration * 100)))
        return out

    def build_filter(self, tmp, opts, hdr):
        """ffmpeg filter chain: (tone-map HDR) -> captions -> 8-bit 4:2:0 for broad compatibility."""
        fonts_dir = Path(tmp) / "fonts"
        fonts_dir.mkdir()
        files = find_font_files(opts["font"])
        for f in files:
            try:
                shutil.copy(f, fonts_dir)
            except OSError:
                pass
        if files:
            self.log_q("Using font file(s): " + ", ".join(f.name for f in files))
            ass = "ass=subs.ass:fontsdir=fonts"
        else:
            ass = "ass=subs.ass"
            sysfonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
            if sysfonts.exists():
                # two backslashes: ffmpeg unescapes filter text twice
                ass += ":fontsdir=" + sysfonts.as_posix().replace(":", "\\\\:")
            self.log_q(f"Couldn't find a font file for '{opts['font']}'; using system font lookup "
                       "(it may fall back to a default font).")
        tonemapped = hdr and ffmpeg_has_filter(FFMPEG, "zscale") and ffmpeg_has_filter(FFMPEG, "tonemap")
        if hdr and not tonemapped:
            self.log_q("This ffmpeg has no tone-mapping support, so HDR colours may look flat.")
        return (TONEMAP if tonemapped else "") + ass + ",format=yuv420p", tonemapped

    def run_ffmpeg(self, video, out, vf, encoder_args, tmp, duration, hdr):
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video),
               "-vf", vf, *encoder_args]
        if hdr:  # label the converted output as normal SDR
            cmd += ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709"]
        cmd += ["-c:a", "copy", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", str(out)]
        tail = deque(maxlen=25)
        self.ffmpeg_proc = subprocess.Popen(
            cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=NO_WINDOW)
        for line in self.ffmpeg_proc.stdout:
            line = line.strip()
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m and duration:
                self.q.put(("progress", min(100, int(m.group(1)) / 1e6 / duration * 100)))
            elif "=" not in line and line and not LOG_NOISE.search(line):
                tail.append(line)
        self.ffmpeg_proc.wait()
        return self.ffmpeg_proc.returncode == 0, list(tail)

    def burn(self, video, words, opts, idx, total):
        encoder_args, encoder_name = pick_encoder()
        width, height, duration = probe_video(video)
        pix_fmt, transfer = probe_color(video)
        hdr = transfer in HDR_TRANSFERS
        note = f"{width}x{height}, {pix_fmt or 'unknown pixel format'}"
        if hdr:
            note += ", HDR (will be tone-mapped to SDR)"
        self.log_q(f"Video is {note}. Burning captions using {encoder_name}...")
        self.q.put(("status", f"Encoding {video.name} ({idx}/{total})"))
        self.q.put(("progress", 0))
        out = video.with_name(video.stem + "_captioned.mp4")

        # Temp folder + short names avoid ffmpeg filter-escaping problems with odd filenames
        with tempfile.TemporaryDirectory() as tmp:
            write_ass(words, Path(tmp) / "subs.ass", opts, width, height)
            vf, tonemapped = self.build_filter(tmp, opts, hdr)
            ok, tail = self.run_ffmpeg(video, out, vf, encoder_args, tmp, duration, tonemapped)
            if not ok and not self.cancel.is_set() and encoder_name != "CPU (x264)":
                self.log_q(f"{encoder_name} failed:\n" + "\n".join(tail[-6:]) +
                           "\nRetrying with the CPU encoder (slower)...")
                self.q.put(("progress", 0))
                ok, tail = self.run_ffmpeg(video, out, vf, CPU_ENCODER, tmp, duration, tonemapped)

        if ok and not self.cancel.is_set():
            self.log_q(f"Saved {out}")
            return
        try:  # don't leave an empty/partial file behind
            out.unlink()
        except OSError:
            pass
        if not self.cancel.is_set():
            self.log_q("ffmpeg failed:\n" + "\n".join(tail))


def main():
    ensure_std_streams()
    try:
        App().mainloop()
    except Exception:
        text = traceback.format_exc()
        log_crash(text)
        try:
            messagebox.showerror("Tech Bench Captions couldn't start",
                                 f"{text}\n\nDetails were saved to:\n{app_data_dir() / 'crash.log'}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
