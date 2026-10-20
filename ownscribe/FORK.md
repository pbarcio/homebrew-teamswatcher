# TeamsWatcher fork of ownscribe

This is [paberr/ownscribe](https://github.com/paberr/ownscribe) 0.15.0 (MIT) with these additions:

| Change | Where |
|---|---|
| **Me vs. Them labels.** `--split-tracks` makes the audio helper save aligned `recording.me.wav` (mic) and `recording.them.wav` (system audio). Each track is transcribed separately and labeled, with silence hallucinations and speaker bleed into the mic filtered out. | `swift/Sources/AudioCapture.swift`, `transcription/channels.py`, `[speakers]` config |
| **App-only capture.** `--app <bundle-id prefix>` (repeatable) records only that app's audio, e.g. `com.microsoft.teams`. | `swift/Sources/AudioCapture.swift`, `audio/coreaudio.py`, `cli.py` |
| **MLX Whisper backend.** `backend = "mlx"` runs Whisper on the Apple Silicon GPU via mlx-whisper (`ownscribe[mlx]`). | `transcription/mlx_transcriber.py` |
| **One-file export.** `[export] dir` writes title, front matter, summary and transcript into a single Markdown file (`"auto"` = work OneDrive "Meeting Notes"). | `output/export.py` |
| **Launcher hand-off.** When `OWNSCRIBE_RESULT_FILE` is set, the final paths are written there as JSON. `OWNSCRIBE_SOURCE_APP` is recorded in the export. | `pipeline.py` |

The audio helper is built from source by `teamswatcher-setup` (it isn't downloaded from upstream releases), because the flags above only exist in this fork.

To pull in upstream changes, diff this folder against a fresh upstream checkout and re-apply.
