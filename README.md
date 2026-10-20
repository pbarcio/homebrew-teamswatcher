# TeamsWatcher

A Mac menu bar app that records your meetings and turns them into notes, entirely on your Mac.

When a meeting app (Microsoft Teams, Zoom or Webex) opens your microphone, TeamsWatcher starts recording. When the call ends, it transcribes the call with the people labeled, optionally summarizes it, and saves everything in one folder. It uses a bundled fork of [ownscribe](ownscribe/FORK.md). No audio or text is sent to any outside service.

Requires an Apple Silicon Mac with macOS 14.2 or later.

## What you get for each call

- **A transcript labeled you vs. them.** Your mic and the call audio are recorded as separate tracks, so every line is attributed to you (your macOS name) or "Them". No speaker-recognition model or account is needed.
- **Fast, local transcription.** Whisper large-v3-turbo runs on the Mac's GPU.
- **Mixed-language calls, if you need them.** Transcription is English by default. Turn on Spanish (or other languages) and people can switch languages at any point; each stretch of speech is transcribed in the language it was spoken in.
- **A notes file ready to upload to Glean.** It holds the title, date, duration, app and speakers, then the summary (if on) and the transcript.
- **A notification when it's done.** Clicking it opens Finder at that recording's folder, with the notes selected.

Two options are **off by default** and can be turned on from the menu (a checkmark shows when they're on):

- **Save Recording:** keep the audio files. When it's off, audio is deleted once it's transcribed.
- **Create Summary:** add a summary to the notes, with key points, action items and decisions, made by a local AI model (Llama 3.1 8B). The first summary downloads that model, about 5 GB, so it takes longer.

Both are remembered between calls. Changing one during a call applies to that call, as long as you change it before the call ends.

## Where things are saved

Each recording gets one folder in `~/ownscribe`, named with the date, time and title:

```
~/ownscribe/
  2026-10-19 1405 - Spec handoff/
    2026-10-19 1405 - Spec handoff.md    <- the notes: upload this one to Glean
    transcript.md
    summary.md                           <- only with Create Summary
    recording.wav, recording.me.wav,
    recording.them.wav                   <- only with Save Recording
  2026-10-20 0930 - Microsoft Teams call/
    ...
```

The title comes from the summary. Without a summary, it's the app's name, such as "Microsoft Teams call". While a call is still being recorded, the folder is just the date and time, and it gets its title when processing finishes.

**Cleaning up older recordings:** versions before 0.4 used folder names like `2026-10-19_1405_spec-handoff` and kept notes in a separate `Exports` folder. Choose **Tidy Transcripts Folder…** from the menu to convert them. It shows what will change before doing anything: it renames old folders, moves each notes file from `Exports` into its recording's folder, and removes empty folders left by failed recordings. It only deletes empty folders and never touches a recording made in the last 10 minutes. From Terminal, the same thing is `ownscribe tidy` to preview and `ownscribe tidy --apply` to make the changes.

## The menu

| Item | What it does |
|---|---|
| Start Recording Now / Stop Recording | Record manually. With no meeting app on the mic, this records all sound the Mac plays, plus your mic. |
| Skip This Call | Stop, and don't record again until the current call ends. |
| Pause Auto-Start | Stop recording calls automatically until you turn it back on. |
| Save Recording / Create Summary | The two options above. |
| Open Latest Notes | Show the most recent recording's folder in Finder. |
| Open Transcripts Folder | Open `~/ownscribe`. |
| Tidy Transcripts Folder… | Convert older recordings to one folder each (see above). |
| Open Logs / Log Audio Processes (Debug) | For troubleshooting. |
| Settings… (⌘,) | Change how TeamsWatcher records and transcribes (see Settings). |
| Check for Updates… | Install the latest release. |

The menu bar icon shows what's happening: microphone = watching, red microphone = recording, hourglass = processing, crossed-out microphone = paused.

## Install

```
brew install pbarcio/teamswatcher/teamswatcher
teamswatcher-setup
```

`brew install` builds the app on your Mac, so you need Xcode Command Line Tools first (`xcode-select --install`). `teamswatcher-setup` then:

- installs the bundled ownscribe fork and builds its audio capture helper
- writes the transcription settings to `~/.config/ownscribe/config.toml`, backing up any existing file
- downloads the Whisper model (no accounts or tokens needed)
- puts TeamsWatcher in /Applications (or ~/Applications if you aren't an admin), starts it, and adds it to Login Items

**One-time permissions:** choose **Start Recording Now** from the menu bar icon and allow the **Microphone** and **Screen & System Audio Recording** prompts. Allow notifications too, so the notifications can be clicked. Then choose **Stop Recording**, and quit and reopen TeamsWatcher.

To install from a git checkout instead (for example, to try changes), clone the repo and run `./install.sh`.

## Updating

Choose **Check for Updates…** from the menu. If a new release is out, TeamsWatcher offers to install it and restarts if the app itself changed. Updates wait until any call has finished processing.

When the app itself changes, macOS asks for its Microphone and Screen Recording permissions again. If recordings come out silent after an update, see Troubleshooting.

From Terminal, the equivalent is `brew update && brew upgrade teamswatcher && teamswatcher-setup`. Run `brew update` first, because Homebrew only checks for new releases about once a day on its own.

## Meeting apps

Teams, Zoom and Webex are on by default. To change the list, open **Settings… › Meeting Apps** and check or uncheck apps.

| App | Default | Note |
|---|---|---|
| Microsoft Teams, Zoom, Webex | on | |
| Slack huddles | off | |
| Google Chrome, Microsoft Edge, Safari | off | Any website using the mic would start a recording (Google Meet, Teams on the web, and so on). |

To add another app, enter its name and bundle ID under **Add another app** on the same tab. Find an app's bundle ID with `osascript -e 'id of app "AppName"'`.

Recording starts once the app has held the mic for **10 seconds**, which keeps device tests from triggering it, and stops after the app has released it for **30 seconds**. While recording, only the meeting app's audio and your mic are captured, so music or videos playing elsewhere on the Mac aren't included.

**Use a headset when you can.** On laptop speakers, your mic also picks up the other side. Lines the mic picked up from the call are filtered out, but a headset gives the cleanest result.

## Languages

Transcription is **English only** by default. If your calls use other languages, open **Settings… › Transcription** and check Spanish, or enter other language codes such as `pt` or `fr`.

With more than one language on, calls can switch between them at any moment, mid-conversation or person to person. Each track is split at natural pauses, the language of every stretch of speech is detected (choosing only among the languages you turned on), and each stretch is transcribed in that language. The notes file lists the languages, such as `languages: ["en", "es"]`, when more than one was spoken.

- Very short replies like "sí" or "OK" keep the language of the surrounding conversation, unless they're clearly the other one.
- A switch in the middle of a sentence, with no pause, is transcribed in whichever language most of that stretch was in.
- The summary is written in English, covering all the languages spoken.
- Only turn on languages your calls actually use. Each extra language gives Whisper one more option to mistake speech for, especially on short replies.
- Mixed-language calls need the default GPU (`mlx`) backend.

## Settings

Choose **Settings…** from the menu (or press ⌘, while the menu is open). Changes take effect when you click **Save**. Save Recording and Create Summary also apply to a call that's already being recorded.

| Tab | What you can change |
|---|---|
| **General** | Save Recording and Create Summary; record only the meeting app's audio or everything the Mac plays; how long the mic must be in use before recording starts (10 s) and released before it stops (30 s); notifications |
| **Meeting Apps** | Which apps start a recording; add or remove other apps |
| **Transcription** | Your name and the label for everyone else; the languages spoken in your calls; the Whisper model; vocabulary hints such as product, system and client names |
| **Notes** | The recordings folder; whether to write a notes file and include the transcript in it; an extra copy of each notes file in your work OneDrive or another folder |
| **Advanced** | Open the settings files, for settings not shown in the window, and reload them after editing by hand |

The settings are stored in two files:

- `~/.config/teamswatcher/config.json`: recording, meeting apps and notifications.
- `~/.config/ownscribe/config.toml`: transcription and notes. Saving from the window changes only the lines for the settings you changed and keeps the file's comments. Updates never overwrite your changes to this file. If a later release changes its defaults, they're saved alongside as `config.toml.new` for you to merge.

A few settings exist only in the files, such as `extraArgs` (extra ownscribe flags) in `config.json`, and `backend = "whisperx"` (CPU instead of GPU, one language per recording) in `config.toml`.

## Troubleshooting

**"No audio was captured," or only your side appears in the transcript.** TeamsWatcher doesn't have Screen & System Audio Recording permission, which is needed to hear the call. In System Settings > Privacy & Security > Screen & System Audio Recording, turn TeamsWatcher off and on again, then quit and reopen it. This is common right after an update. If your work Mac's management software blocks this permission, call audio can't be recorded.

**Testing it.** In Teams, go to Settings > Devices > Make a test call. It starts recording just like a real meeting. Or, with no meeting app open, choose **Start Recording Now**, play a video on the Mac, and talk over it. Sound from a TV or another device only reaches your mic, which records it as you, and usually too quietly to transcribe.

**A call wasn't detected.** Join a test call, then choose **Log Audio Processes (Debug)**. The meeting app should be listed with `in=true`. Also check that `in=true` stays on when you mute yourself; if it drops, increase the stop delay in **Settings… › General**.

**Notifications can't be clicked.** Allow notifications for TeamsWatcher in System Settings > Notifications.

**Recording stopped right after it started.** If ownscribe exits within a minute, auto-start pauses so it doesn't keep failing. Click the notification to see the log. It's usually a permissions problem.

**Logs** are in `~/Library/Logs/TeamsWatcher/`: `teamswatcher.log` for the app, one `ownscribe-*.log` per recording, and one `update-*.log` per update.

## Uninstall

```
teamswatcher-uninstall
brew uninstall teamswatcher
```

`teamswatcher-uninstall` removes the app, its settings and logs, and optionally ownscribe. Your recordings in `~/ownscribe` are kept.

## Privacy and consent

Audio, transcripts and summaries never leave the Mac, unless you set `copy_to` to a synced folder or upload the notes yourself. The only downloads are the Whisper and summary models, once each.

Tell people when you're transcribing a call, especially clients. Some places and companies require everyone's consent.

---

## For maintainers

**Repository layout**

| Path | What it is |
|---|---|
| `main.swift`, `Settings.swift`, `Info.plist`, `build.sh` | The TeamsWatcher menu bar app and its Settings window |
| `AppIcon.png`, `tools/make_icon.py` | The app icon and the script that draws it (`python3 tools/make_icon.py AppIcon.png`, needs Pillow) |
| `ownscribe/` | The ownscribe fork: transcription, labels, notes, `tidy`. See `ownscribe/FORK.md` for how it differs from upstream. |
| `ownscribe-config.toml` | Default transcription settings installed by setup |
| `teamswatcher-setup`, `install.sh`, `uninstall.sh` | Setup and removal |
| `Formula/teamswatcher.rb` | The Homebrew formula (this repo is its own tap) |
| `release.sh` | Publishes a release |

**Publishing a release.** Commit your changes, push, then run `./release.sh 0.4.0` (or the next version number). It tags the commit, points the formula at the new release, and pushes. Coworkers then get it from **Check for Updates…**. The repo must stay public and named `homebrew-teamswatcher` for `brew install pbarcio/teamswatcher/teamswatcher` to work.

**Tests** for the ownscribe fork: `cd ownscribe && uv run pytest`. The Swift app has no automated tests, so check a build with `./build.sh` before releasing.

Speaker identification by voice (diarization) is deliberately off. It can only label speakers as SPEAKER_00, SPEAKER_01 and so on, and it needs a HuggingFace account.
