# TeamsWatcher fork of ownscribe

This is [paberr/ownscribe](https://github.com/paberr/ownscribe) 0.15.0 (MIT) with these additions:

| Change | Where |
|---|---|
| **Me vs. Them labels.** `--split-tracks` makes the audio helper save aligned `recording.me.wav` (mic) and `recording.them.wav` (system audio). Each track is transcribed separately and labeled, with silence hallucinations and speaker bleed into the mic filtered out. | `swift/Sources/AudioCapture.swift`, `transcription/channels.py`, `[speakers]` config |
| **App-only capture.** `--app <bundle-id prefix>` (repeatable) records only that app's audio, e.g. `com.microsoft.teams`. | `swift/Sources/AudioCapture.swift`, `audio/coreaudio.py`, `cli.py` |
| **MLX Whisper backend.** `backend = "mlx"` runs Whisper on the Apple Silicon GPU via mlx-whisper (`ownscribe[mlx]`). | `transcription/mlx_transcriber.py` |
| **Mixed-language calls.** `languages = ["en", "es"]` splits each track at pauses, detects the language per stretch (restricted to those codes, with short ambiguous replies keeping the surrounding language), regroups same-language speech into ≤28 s windows and transcribes each in its language. Segments carry a `language`. | `transcription/multilingual.py`, `mlx_transcriber.py` |
| **One folder per recording.** Folders are named `YYYY-MM-DD HHMM - Title` (title from the summary, else the app's name) instead of `YYYY-MM-DD_HHMM_slug`, and only folders inside the output directory are ever renamed. | `output/naming.py`, `pipeline.py` |
| **Notes file.** Each folder gets `<folder name>.md` with front matter, summary and transcript; `[export] copy_to` optionally copies it elsewhere (`"auto"` = work OneDrive "Meeting Notes"). | `output/export.py`, `pipeline.py` |
| **`ownscribe tidy`.** Converts older folders and the old `Exports` folder to the layout above; preview by default, `--apply` to change. Skips anything modified in the last 10 minutes. | `tidy.py`, `cli.py` |
| **`--summarize/--no-summarize`**, an empty-transcript guard (no summary or notes when nothing was said), and a warning when the system-audio track is pure silence. | `cli.py`, `pipeline.py` |
| **Launcher hand-off.** When `OWNSCRIBE_RESULT_FILE` is set, the final paths (and `speech`/`system_audio` flags) are written there as JSON. `OWNSCRIBE_SOURCE_APP` names the app in titles and notes. `OWNSCRIBE_OPTIONS_FILE` lets the launcher change "summarize"/"keep_recording" until recording stops. | `pipeline.py` |

The audio helper is built from source by `teamswatcher-setup` (it isn't downloaded from upstream releases), because the flags above only exist in this fork.

To pull in upstream changes, diff this folder against a fresh upstream checkout and re-apply.
