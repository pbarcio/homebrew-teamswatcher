# TeamsWatcher

A menu bar app that starts **ownscribe** when Microsoft Teams opens your microphone and stops it when the call ends. Everything runs on the Mac. The app only watches Core Audio and launches ownscribe, and ownscribe's local backend never calls an external LLM.

## Install with Homebrew

```
brew install OWNER/teamswatcher/teamswatcher
teamswatcher-setup
```

`brew install` builds the app on your Mac, so it needs Xcode Command Line Tools (`xcode-select --install`). `teamswatcher-setup` does the rest:

- installs ownscribe
- writes `~/.config/ownscribe/config.toml`, backing up any existing config
- downloads the Whisper and summary models (no accounts or tokens needed)
- installs TeamsWatcher to /Applications (or ~/Applications without admin rights), starts it, and adds it to Login Items

When it finishes, do the one-time permission step it prints: choose **Start Recording Now** from the menu bar icon, allow the Microphone and Screen & System Audio Recording prompts, stop the recording, then quit and reopen TeamsWatcher.

| Command | What it does |
|---|---|
| `brew upgrade teamswatcher && teamswatcher-setup` | Update to the latest release |
| `teamswatcher-setup` | Repair the install; safe to re-run any time |
| `teamswatcher-setup --upgrade` | Also upgrade ownscribe to its latest release |
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

- Every 2 seconds it asks Core Audio which processes have the mic open.
- If a Teams process holds the mic for **10 seconds**, it launches `ownscribe --pid <Teams audio PID>`. That records Teams audio plus your mic, and the delay keeps device tests from triggering it.
- When Teams releases the mic for **30 seconds**, it sends Ctrl+C, and ownscribe transcribes and summarizes into `~/ownscribe/`.
- The menu bar icon shows the current state: waveform = watching, red dot = recording, hourglass = processing.
- The menu also has **Skip This Call**, **Pause Auto-Start**, and manual Start/Stop.

## Verify Teams detection

Join a Teams test call, then choose **Log Audio Processes (Debug)** from the menu. You should see `teams=true in=true`. Also check that `in=true` stays on when you mute yourself in Teams. If it drops while muted, raise `stopGraceSeconds`.

## Settings

- **ownscribe:** `~/.config/ownscribe/config.toml` sets the models, vocabulary hints and output folder. Your edits are safe: setup only overwrites the file if you haven't changed it. If the defaults change in a later release and you've edited yours, it saves the new defaults as `config.toml.new` for you to merge. To change the defaults for everyone, edit `ownscribe-config.toml` in the repo and publish a release.
- **TeamsWatcher:** `~/.config/teamswatcher/config.json` is created on first launch. Edit it from the menu (**Edit Config…**), then choose **Reload Config**.

| Key | Default | Purpose |
|---|---|---|
| `startDebounceSeconds` | 10 | How long Teams must hold the mic before recording starts |
| `stopGraceSeconds` | 30 | How long the mic must be released before recording stops |
| `captureTeamsAudioOnly` | true | Pass Teams' PID to ownscribe; falls back to all system audio |
| `pidFlag` | `--pid` | ownscribe's flag for targeting a process |
| `extraArgs` | `[]` | Extra ownscribe flags, e.g. `["--template", "meeting"]` |
| `notifications` | true | Start, stop and finished notifications |

Logs are in `~/Library/Logs/TeamsWatcher/`: `teamswatcher.log` for the watcher, plus one `ownscribe-*.log` per recording.

## Notes

- The app is ad-hoc signed, so macOS ties its permissions to the exact build. That's why the installer avoids unnecessary rebuilds.
- If ownscribe exits within 60 seconds of starting, auto-start pauses so it doesn't loop. This is usually a permissions problem; check the session log.
- If your work Mac's MDM blocks Screen & System Audio Recording, system audio capture won't work.
- Recording consent: tell participants you're transcribing, especially with clients.
