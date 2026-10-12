# Changelog

## 1.1.0
- **Caption editor:** review and fix the transcript before rendering. Edit any line, press Enter
  to jump to the next, delete junk lines, and use find-and-replace to fix a misheard word
  everywhere. Timings of words you didn't change are preserved.
- **Saved transcripts:** each video gets a `.captions.json` next to it. Re-rendering, restyling or
  editing again no longer re-runs Whisper.
- Highlight timing is now cleaned so two highlighted words never overlap on screen.
- Added options "Review and edit captions before rendering" and "Reuse saved transcript".
- Whisper model is only loaded when a transcription is actually needed.
- Renamed the app file to `techbench_captions.pyw`.
- **No more manual setup:** the first time you render, the app offers to download **ffmpeg**
  (checksum-verified) and the speech **model** (with a progress bar) into
  `%LOCALAPPDATA%\TechBenchCaptions`. One prompt, one time. If you already have a *working, modern*
  ffmpeg it is used instead; an ancient ffmpeg hiding on your PATH is detected and skipped.
- **Windows app:** added `build_exe.bat` (PyInstaller) and a GitHub Actions workflow that builds the
  standalone app and attaches it to a Release when you push a version tag.
- The GPU is only chosen automatically when its CUDA libraries (cuBLAS/cuDNN) are actually
  installed, because a missing library can crash the whole program. Otherwise the CPU is used.
- Unexpected errors are written to `%LOCALAPPDATA%\TechBenchCaptions\crash.log`.
- ffmpeg is no longer required if you only want the `.srt` file.
- **Fixed:** transcription failed with `open() got an unexpected keyword argument 'metadata_errors'`
  on a fresh install. PyAV 19 (released 2026-09-29) removed an argument that faster-whisper still
  uses. `requirements.txt` now pins `av<19`, and the app also works around it if PyAV 19 is present.
- `build_exe.bat` records the exact versions it built with in `build-freeze.txt`. Copy that file to
  `requirements-lock.txt` after a build you've confirmed works, and future builds will use those
  same versions instead of whatever is newest.
- **Fixed:** GPU encoding failed on 10-bit footage (`10 bit encode not supported`), leaving an empty
  `_captioned.mp4`. Output is now always 8-bit H.264, which plays everywhere.
- **HDR videos** are detected and tone-mapped to normal SDR so colours don't look washed out.
- If the GPU encoder fails for any reason, the app **retries on the CPU** automatically.
- Only the selected font's file is passed to ffmpeg (no more scanning `C:\Windows\Fonts` and
  `.fon` warning spam). Failed encodes no longer leave an empty output file behind.

## 1.0.0
- Initial release: GUI, batch processing, karaoke-style burned-in captions, `.srt` export,
  GPU transcription and encoding, ffmpeg auto-detection.
