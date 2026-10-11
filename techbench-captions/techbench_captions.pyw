"""
Tech Bench Auto-Captioner (GUI) - karaoke style
-----------------------------------------------
White captions, the word being spoken highlighted, thick black outline,
burned into a COPY of your video (original is never touched).

Double-click this file to open the app (.pyw = no console window).

Needs:  pip install faster-whisper
        ffmpeg on your PATH  (Windows: winget install ffmpeg)
"""

import glob
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

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


def find_ffmpeg():
    """Prefer a modern ffmpeg from the usual install spots (winget/Chocolatey/Scoop), then PATH."""
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                             "*FFmpeg*", "**", "bin", "ffmpeg.exe"), recursive=True)
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
    candidates += [r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
                   r"C:\ffmpeg\bin\ffmpeg.exe",
                   os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe")]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("ffmpeg") or "ffmpeg"


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


def all_words(segments):
    return [w for seg in segments for w in (seg.words or []) if w.word.strip()]


def chunk_words(words, n):
    for i in range(0, len(words), n):
        yield words[i:i + n]


def write_srt(segments, path, n):
    with open(path, "w", encoding="utf-8") as f:
        for i, chunk in enumerate(chunk_words(all_words(segments), n), 1):
            text = " ".join(w.word.strip() for w in chunk)
            f.write(f"{i}\n{fmt_srt(chunk[0].start)} --> {fmt_srt(chunk[-1].end)}\n{text}\n\n")


def hex_to_bgr(hex_color):
    """'#RRGGBB' -> 'BBGGRR' (ASS colours are blue-green-red)."""
    h = hex_color.lstrip("#")
    return h[4:6] + h[2:4] + h[0:2]


def write_ass(segments, path, opts, width, height):
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
        t = w.word.strip().replace("{", "").replace("}", "")
        return t.upper() if opts["uppercase"] else t

    with open(path, "w", encoding="utf-8") as f:
        f.write(header)
        for chunk in chunk_words(all_words(segments), opts["words"]):
            texts = [clean(w) for w in chunk]
            for i, w in enumerate(chunk):
                start = w.start
                # hold until the next word begins so the highlight never flickers off
                end = chunk[i + 1].start if i + 1 < len(chunk) else w.end
                end = max(end, start + 0.06)
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
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"], "CPU (x264)"


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


# ---------- the app ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Tech Bench Auto-Captioner")
        self.geometry("740x680")
        self.minsize(660, 600)

        self.files = []
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.ffmpeg_proc = None

        self.model_var = tk.StringVar(value="small.en")
        self.device_var = tk.StringVar(value="auto")
        self.words_var = tk.IntVar(value=4)
        self.font_var = tk.StringVar(value="Arial Black")
        self.size_var = tk.IntVar(value=72)
        self.upper_var = tk.BooleanVar(value=False)
        self.burn_var = tk.BooleanVar(value=True)
        self.highlight = DEFAULT_HIGHLIGHT
        self.ffmpeg_var = tk.StringVar(value=find_ffmpeg())

        self.build_ui()
        self.after(100, self.poll_queue)

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
                        variable=self.burn_var).grid(row=1, column=0, columnspan=4, sticky="w", **pad)
        ttk.Label(of, text="ffmpeg location:").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(of, textvariable=self.ffmpeg_var, width=52).grid(row=2, column=1, columnspan=3,
                                                                    sticky="we", **pad)
        ttk.Button(of, text="Browse...", command=self.browse_ffmpeg).grid(row=2, column=4, **pad)

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
        entered = self.ffmpeg_var.get().strip() or "ffmpeg"
        resolved = entered if os.path.isfile(entered) else shutil.which(entered)
        if not resolved:
            messagebox.showerror("ffmpeg missing",
                                 "ffmpeg wasn't found.\n\n"
                                 "Install it with:  winget install Gyan.FFmpeg\n"
                                 "then restart this app, or use Browse to select ffmpeg.exe.")
            return
        FFMPEG = resolved
        probe = Path(resolved).with_name("ffprobe" + Path(resolved).suffix)
        FFPROBE = str(probe) if probe.exists() else "ffprobe"
        if self.burn_var.get():
            try:
                decoders = subprocess.run([FFMPEG, "-decoders"], capture_output=True, text=True,
                                          creationflags=NO_WINDOW).stdout
            except Exception:
                decoders = ""
            if "hevc" not in decoders.lower():
                messagebox.showerror(
                    "ffmpeg too old",
                    f"This ffmpeg can't decode H.265/HEVC video:\n{resolved}\n\n"
                    "It's probably a very old build. Install a current one with\n"
                    "    winget install Gyan.FFmpeg\n"
                    "then restart this app (or use Browse to select the new ffmpeg.exe).")
                return
        self.write_log(f"Using ffmpeg: {resolved}")
        self.cancel.clear()
        self.start_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        opts = dict(model=self.model_var.get(), device=self.device_var.get(),
                    words=max(1, self.words_var.get()), font=self.font_var.get().strip() or "Arial Black",
                    font_size=self.size_var.get(), uppercase=self.upper_var.get(),
                    highlight=self.highlight, burn=self.burn_var.get())
        self.worker = threading.Thread(target=self.run_all, args=(list(self.files), opts), daemon=True)
        self.worker.start()

    def cancel_run(self):
        self.cancel.set()
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            self.ffmpeg_proc.terminate()
        self.log_q("Cancelling...")

    # --- worker thread ---
    def run_all(self, files, opts):
        try:
            self.q.put(("status", "Loading faster-whisper (first run downloads the model)..."))
            from faster_whisper import WhisperModel

            device = opts["device"]
            if device == "auto":
                try:
                    import ctranslate2
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                except Exception:
                    device = "cpu"

            model = self.load_model(WhisperModel, opts["model"], device)
            current_device = device

            for idx, f in enumerate(files, 1):
                if self.cancel.is_set():
                    break
                video = Path(f)
                self.log_q(f"\n=== [{idx}/{len(files)}] {video.name} ===")
                self.q.put(("progress", 0))

                try:
                    segments = self.transcribe(model, video, idx, len(files))
                except Exception as e:
                    if current_device == "cuda":
                        self.log_q(f"GPU failed ({e}). Retrying on CPU...")
                        current_device = "cpu"
                        model = self.load_model(WhisperModel, opts["model"], "cpu")
                        segments = self.transcribe(model, video, idx, len(files))
                    else:
                        raise

                if self.cancel.is_set() or segments is None:
                    break

                srt = video.with_suffix(".srt")
                write_srt(segments, srt, opts["words"])
                self.log_q(f"Saved {srt}")

                if opts["burn"]:
                    self.burn(video, segments, opts, idx, len(files))

            msg = "Cancelled." if self.cancel.is_set() else "All done!"
            self.q.put(("status", msg))
            self.log_q(msg)
        except ImportError:
            self.log_q("faster-whisper isn't installed. Run:  pip install faster-whisper")
            self.q.put(("status", "Error - see log"))
        except Exception as e:
            self.log_q(f"ERROR: {e}")
            self.q.put(("status", "Error - see log"))
        finally:
            self.q.put(("done", None))

    def load_model(self, WhisperModel, size, device):
        compute = "float16" if device == "cuda" else "int8"
        self.log_q(f"Loading model '{size}' on {device}...")
        return WhisperModel(size, device=device, compute_type=compute)

    def transcribe(self, model, video, idx, total):
        self.q.put(("status", f"Transcribing {video.name} ({idx}/{total})"))
        segs, info = model.transcribe(str(video), word_timestamps=True, vad_filter=True,
                                      initial_prompt=JARGON_PROMPT)
        self.log_q(f"Language: {info.language}, length: {info.duration:.0f}s")
        out = []
        for seg in segs:  # generator: transcription happens as we iterate
            if self.cancel.is_set():
                return None
            out.append(seg)
            if info.duration:
                self.q.put(("progress", min(100, seg.end / info.duration * 100)))
        return out

    def burn(self, video, segments, opts, idx, total):
        encoder_args, encoder_name = pick_encoder()
        width, height, duration = probe_video(video)
        self.log_q(f"Video is {width}x{height}. Burning captions using {encoder_name}...")
        self.q.put(("status", f"Encoding {video.name} ({idx}/{total})"))
        self.q.put(("progress", 0))
        out = video.with_name(video.stem + "_captioned.mp4")

        # Temp folder + short name avoids ffmpeg filter-escaping problems with odd filenames
        with tempfile.TemporaryDirectory() as tmp:
            write_ass(segments, Path(tmp) / "subs.ass", opts, width, height)
            # Point libass at the Windows fonts folder directly (works even when ffmpeg
            # can't find a fontconfig setup). Two backslashes: ffmpeg unescapes filter text twice.
            vf = "ass=subs.ass"
            fonts_dir = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
            if fonts_dir.exists():
                vf += ":fontsdir=" + fonts_dir.as_posix().replace(":", "\\\\:")
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video),
                   "-vf", vf, *encoder_args,
                   "-c:a", "copy", "-progress", "pipe:1", "-nostats", str(out)]
            tail = deque(maxlen=25)
            self.ffmpeg_proc = subprocess.Popen(
                cmd, cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, creationflags=NO_WINDOW)
            for line in self.ffmpeg_proc.stdout:
                line = line.strip()
                m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                if m and duration:
                    self.q.put(("progress", min(100, int(m.group(1)) / 1e6 / duration * 100)))
                elif "=" not in line:
                    tail.append(line)
            self.ffmpeg_proc.wait()

        if self.cancel.is_set():
            return
        if self.ffmpeg_proc.returncode != 0:
            self.log_q("ffmpeg failed:\n" + "\n".join(tail))
        else:
            self.log_q(f"Saved {out}")


if __name__ == "__main__":
    App().mainloop()
