# Tech Bench Auto-Captioner

A free, local, fast alternative to online auto-caption tools. Drop in your videos and get
**karaoke-style captions** burned into a copy: white text, a thick black outline, and the
word currently being spoken highlighted in a colour of your choice (green by default).

It runs on your own PC using [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for
transcription and [ffmpeg](https://ffmpeg.org/) for rendering, so there's no upload, no queue
and no export wait. It also saves a standard `.srt` file you can upload straight to YouTube.

> **Status:** early version, built and tested on Windows. Bug reports welcome.

<!-- Add a screenshot or GIF here, e.g. ![screenshot](docs/screenshot.png) -->

## Features

- Simple window (no command line needed), with batch processing and a progress bar
- Highlighted-word karaoke captions with outline and shadow
- Choose font, size, highlight colour, words on screen and ALL CAPS
- Uses your GPU for transcription (NVIDIA CUDA) and for encoding (NVENC / QSV / AMF) when available
- Works with horizontal and vertical (Shorts/Reels) video, including rotated phone footage
- Custom vocabulary prompt so tech terms (HDMI, NVMe, Ryzen...) are spelled correctly
- Never touches your original video: output is saved as `yourvideo_captioned.mp4`

## Requirements

- Windows (the app is written in Python/tkinter; macOS and Linux may work but are untested)
- [Python 3.9+](https://www.python.org/downloads/) (tick "Add Python to PATH" when installing)
- A **current** [ffmpeg](https://ffmpeg.org/download.html)
  - Windows: `winget install Gyan.FFmpeg`
- Optional: an NVIDIA GPU with up-to-date drivers for much faster transcription

## Install

```
git clone https://github.com/YOUR-USERNAME/techbench-captions.git
cd techbench-captions
pip install -r requirements.txt
```

## Use

Double-click `techbench_captions.pyw`, or run:

```
python techbench_captions.pyw
```

1. Click **Add videos...** and choose one or more files.
2. Pick your caption style.
3. Click **Start**.

Outputs are saved next to each video: `video.srt` and (if enabled) `video_captioned.mp4`.
The first run downloads the Whisper model (a few hundred MB for `small.en`), so it starts
slowly once.

### Settings

| Setting | Notes |
| --- | --- |
| Model | `small.en` is a good balance. `medium.en` / `large-v3` are more accurate but slower. |
| Words on screen | 3-4 suits Shorts; 5-6 suits normal videos. |
| Font | Type any font installed on your PC (name must match exactly). |
| ffmpeg location | Auto-detected; use Browse if it picks the wrong one. |

To improve spelling of your own jargon, edit `JARGON_PROMPT` near the top of the file.

## Troubleshooting

**`Unable to parse option value "-1" as pixel format`** - your ffmpeg is too old to decode the
video (often H.265/HEVC). Install a current ffmpeg and point the app at it. Run
`Get-Command ffmpeg -All | Select-Object Source` in PowerShell to see every ffmpeg on your PATH;
a stray old `ffmpeg.exe` (for example in Python's `Scripts` folder) can hide the new one.

**`A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x`** - an older PyTorch
install is clashing with NumPy 2. Run `pip install "numpy<2"` or upgrade torch, or use a
virtual environment.

**"Disabling PyTorch..." message** - harmless; faster-whisper doesn't need PyTorch.

**GPU errors (CUDA/cuDNN missing)** - the app automatically retries on CPU. Update your NVIDIA
drivers if you want GPU transcription.

**Wrong font** - the font name must match an installed font exactly. Try one from the dropdown.

## Fonts and third-party software

This repo does not include fonts or ffmpeg. Fonts such as Arial Black are referenced by name and
must be installed on your system. faster-whisper and the Whisper models are MIT-licensed; ffmpeg
is licensed separately (LGPL/GPL depending on the build).

## License

[MIT](LICENSE)
