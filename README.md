# TeamsWatcher

A menu bar app that starts recording when a meeting app (Microsoft Teams, Zoom, Webex, and optionally Slack or a browser) opens your microphone, then stops when the call ends. It transcribes and summarizes everything on the Mac with a bundled fork of [ownscribe](ownscribe/FORK.md). No audio or text is sent to an external service.

**What you get for each call**

- **A transcript labeled you vs. them.** Your mic and the call audio are recorded as separate tracks, so every line is attributed to you (your macOS name) or "Them". No speaker-recognition model or account is needed.
- **Fast transcription on the GPU.** Whisper large-v3-turbo runs on the Apple Silicon GPU via MLX.
- **A local summary** with key points, action items and decisions, from Llama 3.1 8B.
- **One export file**, ready to upload to Glean: `~/ownscribe/Exports/2026-10-19 1405 - <Title>.md`, with title, date, duration, app and speakers in its front matter, followed by the summary and transcript.
- **A clickable "Meeting notes ready" notification** that opens the notes. **Open Latest Notes** in the menu does the same.

## Install with Homebrew

```
brew install OWNER/teamswatcher/teamswatcher
teamswatcher-setup
```

`brew install` builds the app on your Mac, so it needs Xcode Command Line Tools (`xcode-select --install`). `teamswatcher-setup` does the rest:

- installs the bundled ownscribe fork with MLX support, and builds its audio capture helper
- writes `~/.config/ownscribe/config.toml`, backing up any existing config
- downloads the Whisper and summary models (no accounts or tokens needed)
- installs TeamsWatcher to /Applications (or ~/Applications without admin rights), starts it, and adds it to Login Items

When it finishes, do the one-time permission step it prints: choose **Start Recording Now** from the menu bar icon, allow the Microphone and Screen & System Audio Recording prompts, stop the recording, then quit and reopen TeamsWatcher.

| Command | What it does |
|---|---|
| `brew upgrade teamswatcher && teamswatcher-setup` | Update to the latest release |
| `teamswatcher-setup` | Repair the install; safe to re-run any time |
| `teamswatcher-setup --upgrade` | Also reinstall ownscribe with the newest versions of its dependencies |
| `teamswatcher-setup --rebuild` | Reinstall the app even if unchanged (re-grant permissions afterward) |
| `teamswatcher-uninstall` | Remove TeamsWatcher, and optionally ownscribe (transcripts are kept) |

The setup only replaces the app when it actually changed, so your macOS permissions stay granted across re-runs.

### Install from a git checkout instead

```
git clone https://github.com/OWNER/homebrew-teamswatcher.git
cd homebrew-teamswatcher
./install.sh
```

`./install.sh` takes the same `--upgrade` and `--rebuild` options, and it installs uv and ffmpeg with Homebrew if they're missing. To remove everything, run `./uninstall.sh`.

## Publishing (maintainer)

The repo is its own Homebrew tap, so it **must** be named `homebrew-teamswatcher` and be public for a plain `brew install` to work.

1. Create an empty public GitHub repo named `homebrew-teamswatcher`, then push this folder to it:
   ```
   git init -b main && git add . && git commit -m "Initial commit"
   git remote add origin git@github.com:<you>/homebrew-teamswatcher.git
   git push -u origin main
   ```
2. Publish a release: `./release.sh 0.1.0`. This tags the commit, fills in the formula's URL and checksum, and pushes. Coworkers can install right after.
3. For later changes, commit and then run `./release.sh 0.1.1` (and so on). Coworkers update with `brew upgrade teamswatcher && teamswatcher-setup`.

Replace `OWNER` in this README with your GitHub username. `release.sh` fixes it in the formula automatically.

Speaker labels (diarization) are turned off. Without real names they'd only read SPEAKER_00, SPEAKER_01 and so on, and leaving them off means no HuggingFace account is needed.

## How it works

- Every 2 seconds TeamsWatcher asks Core Audio which processes have the mic open.
- If an enabled meeting app holds the mic for **10 seconds**, it launches `ownscribe --app <that app's bundle ID>`. That records the app's audio plus your mic, each on its own track. The delay keeps device tests and pre-join screens from triggering it.
- When the app releases the mic for **30 seconds**, TeamsWatcher sends Ctrl+C. ownscribe then transcribes each track, labels and merges them, summarizes, and writes the export.
- The menu bar icon shows the current state: waveform = watching, red dot = recording, hourglass = processing.
- The menu also has **Skip This Call**, **Pause Auto-Start**, manual Start/Stop, and **Open Latest Notes**.

**Use a headset when you can.** On laptop speakers, your mic also picks up the other side. TeamsWatcher drops mic lines that duplicate what was said on the call at the same moment, but a headset gives the cleanest "you" track.

## Meeting apps

Teams, Zoom and Webex are on by default. To change the list, open **Edit Config…** from the menu, set `"enabled"` for an app in `meetingApps`, save, and choose **Reload Config**.

| App | Default | Note |
|---|---|---|
| Microsoft Teams | on | |
| Zoom | on | |
| Webex | on | |
| Slack huddles | off | |
| Google Chrome, Microsoft Edge, Safari | off | Any website using the mic starts a recording (Google Meet, Teams on the web, and so on), so only turn these on if that's what you want. |

You can add other apps too: give the entry a `name`, its `bundlePrefixes` (run `osascript -e 'id of app "AppName"'` to find a bundle ID), and `"enabled": true`.

To check detection, join a test call and choose **Log Audio Processes (Debug)**. The app should be listed with `in=true`. Also check that `in=true` stays on when you mute yourself. If it drops while muted, raise `stopGraceSeconds`.

## Settings

- **ownscribe:** `~/.config/ownscribe/config.toml` sets the transcription model, vocabulary hints, speaker labels, summary model, and export folder. Your edits are safe: setup only overwrites the file if you haven't changed it. If the defaults change in a later release and you've edited yours, setup saves the new defaults as `config.toml.new` for you to merge. To change the defaults for everyone, edit `ownscribe-config.toml` in the repo and publish a release.
- **TeamsWatcher:** `~/.config/teamswatcher/config.json` is created on first launch. Edit it from the menu (**Edit Config…**), then choose **Reload Config**.

Useful ownscribe settings:

| Setting | Default | Purpose |
|---|---|---|
| `[speakers] me` | your macOS name | Your label in transcripts |
| `[speakers] them` | `Them` | Label for everyone else |
| `[transcription] backend` / `model` | `mlx` / `large-v3-turbo` | Use `whisperx` / `medium` for the CPU backend |
| `[export] dir` | `~/ownscribe/Exports` | `"auto"` = a "Meeting Notes" folder in your work OneDrive (syncs to the cloud); `""` = off |

TeamsWatcher settings:

| Key | Default | Purpose |
|---|---|---|
| `meetingApps` | Teams, Zoom, Webex on | Which apps start a recording (see above) |
| `startDebounceSeconds` | 10 | How long the app must hold the mic before recording starts |
| `stopGraceSeconds` | 30 | How long the mic must be released before recording stops |
| `captureAppAudioOnly` | true | Record only the meeting app's audio; `false` = all system audio |
| `extraArgs` | `[]` | Extra ownscribe flags, e.g. `["--template", "meeting"]` |
| `notifications` | true | Start, stop and notes-ready notifications |

Logs are in `~/Library/Logs/TeamsWatcher/`: `teamswatcher.log` for the watcher, plus one `ownscribe-*.log` per recording.

## Notes

- The first time TeamsWatcher runs, macOS asks whether it may send notifications. Allow it to get clickable ones. If you don't, you still get basic notifications that can't be clicked.
- The app is ad-hoc signed, so macOS ties its permissions to the exact build. Setup avoids unnecessary rebuilds, but an update that changes the app means granting permissions again.
- If ownscribe exits within 60 seconds of starting, auto-start pauses so it doesn't loop. This is usually a permissions problem; click the notification to see the log.
- If your work Mac's MDM blocks Screen & System Audio Recording, system audio capture won't work.
- Recording consent: tell participants you're transcribing, especially with clients.
