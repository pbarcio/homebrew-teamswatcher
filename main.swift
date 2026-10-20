// TeamsWatcher — menu bar app that auto-starts ownscribe when Microsoft Teams
// opens the microphone, and stops it (Ctrl+C equivalent) when Teams releases it.
//
// Everything stays local: this app only watches Core Audio and launches ownscribe.
// Requires macOS 14.2+ (Core Audio process objects). Build with ./build.sh.

import AppKit
import CoreAudio
import Darwin

// MARK: - Paths, formatting, logging

let home = FileManager.default.homeDirectoryForCurrentUser.path
let configPath = expand("~/.config/teamswatcher/config.json")
var logDirectory = expand("~/Library/Logs/TeamsWatcher")

func expand(_ path: String) -> String { (path as NSString).expandingTildeInPath }

let stampFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyyMMdd-HHmmss"
    return f
}()

let logFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

let clockFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "h:mm a"
    return f
}()

var logFileURL: URL { URL(fileURLWithPath: logDirectory).appendingPathComponent("teamswatcher.log") }

func log(_ message: String) {
    let line = "\(logFormatter.string(from: Date())) \(message)\n"
    guard let data = line.data(using: .utf8) else { return }
    try? FileManager.default.createDirectory(atPath: logDirectory, withIntermediateDirectories: true)
    if let handle = try? FileHandle(forWritingTo: logFileURL) {
        handle.seekToEndOfFile()
        handle.write(data)
        try? handle.close()
    } else {
        try? data.write(to: logFileURL)
    }
}

// MARK: - Config (~/.config/teamswatcher/config.json, created on first run)

struct Config: Codable {
    /// Where `uv tool install ownscribe` puts the command.
    var ownscribePath = "~/.local/bin/ownscribe"
    /// Extra ownscribe flags, e.g. ["--diarize", "--template", "meeting"].
    var extraArgs: [String] = []
    /// Audio processes whose bundle ID starts with one of these count as Teams.
    var teamsBundlePrefixes = ["com.microsoft.teams"]
    /// Fallback match on executable path (covers helpers with no bundle ID).
    var teamsPathMarkers = ["Microsoft Teams"]
    /// Teams must hold the mic this long before recording starts (skips device tests / pre-join).
    var startDebounceSeconds = 10.0
    /// After Teams releases the mic, wait this long before stopping (covers brief drops).
    var stopGraceSeconds = 30.0
    var pollIntervalSeconds = 2.0
    /// true = pass the Teams audio process PID to ownscribe so only Teams + your mic are captured.
    /// Falls back to all system audio if no Teams output stream is found.
    var captureTeamsAudioOnly = true
    /// ownscribe's flag for targeting a process (check `ownscribe --help`).
    var pidFlag = "--pid"
    var notifications = true
    var transcriptsDir = "~/ownscribe"
    var logDir = "~/Library/Logs/TeamsWatcher"

    init() {}

    // Tolerant decoding: any key missing from the file keeps its default.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let v = try c.decodeIfPresent(String.self, forKey: .ownscribePath) { ownscribePath = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .extraArgs) { extraArgs = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .teamsBundlePrefixes) { teamsBundlePrefixes = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .teamsPathMarkers) { teamsPathMarkers = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .startDebounceSeconds) { startDebounceSeconds = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .stopGraceSeconds) { stopGraceSeconds = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .pollIntervalSeconds) { pollIntervalSeconds = v }
        if let v = try c.decodeIfPresent(Bool.self, forKey: .captureTeamsAudioOnly) { captureTeamsAudioOnly = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .pidFlag) { pidFlag = v }
        if let v = try c.decodeIfPresent(Bool.self, forKey: .notifications) { notifications = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .transcriptsDir) { transcriptsDir = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .logDir) { logDir = v }
    }
}

func loadConfig() -> Config {
    let url = URL(fileURLWithPath: configPath)
    if let data = try? Data(contentsOf: url) {
        do {
            return try JSONDecoder().decode(Config.self, from: data)
        } catch {
            log("Config parse error, using defaults: \(error)")
            return Config()
        }
    }
    let cfg = Config()
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    if let data = try? encoder.encode(cfg) { try? data.write(to: url) }
    return cfg
}

// MARK: - Core Audio process objects (macOS 14+)

struct AudioProc {
    let pid: pid_t
    let bundleID: String
    let path: String
    let runningInput: Bool
    let runningOutput: Bool
}

func globalAddress(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func audioProcessObjectIDs() -> [AudioObjectID] {
    let system = AudioObjectID(kAudioObjectSystemObject)
    var address = globalAddress(kAudioHardwarePropertyProcessObjectList)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(system, &address, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    let status = ids.withUnsafeMutableBytes { buffer in
        AudioObjectGetPropertyData(system, &address, 0, nil, &size, buffer.baseAddress!)
    }
    guard status == noErr else { return [] }
    return Array(ids.prefix(Int(size) / MemoryLayout<AudioObjectID>.size))
}

func audioUInt32(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = globalAddress(selector)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func audioPID(_ object: AudioObjectID) -> pid_t? {
    var address = globalAddress(kAudioProcessPropertyPID)
    var value: pid_t = 0
    var size = UInt32(MemoryLayout<pid_t>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func audioBundleID(_ object: AudioObjectID) -> String {
    var address = globalAddress(kAudioProcessPropertyBundleID)
    var value: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let status = withUnsafeMutablePointer(to: &value) { pointer in
        AudioObjectGetPropertyData(object, &address, 0, nil, &size, pointer)
    }
    guard status == noErr, let cf = value?.takeRetainedValue() else { return "" }
    return cf as String
}

func executablePath(of pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN)) // PROC_PIDPATHINFO_MAXSIZE
    let length = proc_pidpath(pid, &buffer, UInt32(buffer.count))
    return length > 0 ? String(cString: buffer) : ""
}

func audioProcesses() -> [AudioProc] {
    audioProcessObjectIDs().compactMap { id in
        guard let pid = audioPID(id) else { return nil }
        return AudioProc(pid: pid,
                         bundleID: audioBundleID(id),
                         path: executablePath(of: pid),
                         runningInput: (audioUInt32(id, kAudioProcessPropertyIsRunningInput) ?? 0) != 0,
                         runningOutput: (audioUInt32(id, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0)
    }
}

func childPIDs(of pid: pid_t) -> [pid_t] {
    var buffer = [pid_t](repeating: 0, count: 64)
    let count = proc_listchildpids(pid, &buffer, Int32(buffer.count * MemoryLayout<pid_t>.size))
    guard count > 0 else { return [] }
    return Array(buffer.prefix(Int(count)))
}

// MARK: - Notifications (osascript avoids notification entitlements for an ad-hoc signed app)

func appleScriptEscape(_ s: String) -> String {
    s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
}

// MARK: - Recording session

final class Session {
    let process: Process
    let stdinPipe: Pipe
    let logURL: URL
    let started = Date()
    let automatic: Bool
    let teamsOnly: Bool

    init(process: Process, stdinPipe: Pipe, logURL: URL, automatic: Bool, teamsOnly: Bool) {
        self.process = process
        self.stdinPipe = stdinPipe
        self.logURL = logURL
        self.automatic = automatic
        self.teamsOnly = teamsOnly
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    var config = Config()
    var statusItem: NSStatusItem!
    var timer: Timer?

    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var skipItem: NSMenuItem!
    var pauseItem: NSMenuItem!

    var current: Session?
    var processing: [Session] = []
    var paused = false
    var skipCurrentCall = false
    var micActiveSince: Date?
    var micLastSeen: Date?

    func applicationDidFinishLaunching(_ notification: Notification) {
        applyConfig(loadConfig())
        buildMenu()
        log("TeamsWatcher started. Config: \(configPath)")
        let exe = expand(config.ownscribePath)
        if !FileManager.default.isExecutableFile(atPath: exe) {
            notify("TeamsWatcher", "ownscribe not found at \(exe). Edit the config.")
        }
        updateUI()
    }

    func applyConfig(_ cfg: Config) {
        config = cfg
        logDirectory = expand(cfg.logDir)
        timer?.invalidate()
        timer = Timer.scheduledTimer(timeInterval: max(0.5, cfg.pollIntervalSeconds),
                                     target: self, selector: #selector(tick),
                                     userInfo: nil, repeats: true)
    }

    // MARK: Menu

    func buildMenu() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.autoenablesItems = false

        statusLine.isEnabled = false
        menu.addItem(statusLine)
        menu.addItem(.separator())

        startItem = item("Start Recording Now", #selector(manualStart))
        stopItem = item("Stop Recording", #selector(manualStop))
        skipItem = item("Skip This Call", #selector(skipCall))
        pauseItem = item("Pause Auto-Start", #selector(togglePause))
        [startItem, stopItem, skipItem, pauseItem].forEach { menu.addItem($0) }

        menu.addItem(.separator())
        menu.addItem(item("Open Transcripts Folder", #selector(openTranscripts)))
        menu.addItem(item("Open Logs", #selector(openLogs)))
        menu.addItem(item("Log Audio Processes (Debug)", #selector(dumpAudioProcesses)))
        menu.addItem(item("Edit Config…", #selector(editConfig)))
        menu.addItem(item("Reload Config", #selector(reloadConfig)))
        menu.addItem(.separator())
        menu.addItem(item("Quit TeamsWatcher", #selector(quit)))

        statusItem.menu = menu
    }

    func item(_ title: String, _ action: Selector) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: "")
        menuItem.target = self
        return menuItem
    }

    func updateUI() {
        guard let button = statusItem?.button else { return }
        let symbol: String
        var tint: NSColor? = nil
        if current != nil {
            symbol = "record.circle.fill"
            tint = .systemRed
        } else if !processing.isEmpty {
            symbol = "hourglass"
        } else if paused {
            symbol = "pause.circle"
        } else {
            symbol = "waveform"
        }
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: "TeamsWatcher")
        button.contentTintColor = tint

        var lines: [String] = []
        if let s = current {
            lines.append("Recording since \(clockFormatter.string(from: s.started)) (\(s.teamsOnly ? "Teams audio" : "all system audio") + mic)")
        } else if paused {
            lines.append("Auto-start paused")
        } else if skipCurrentCall {
            lines.append("Skipping this call")
        } else {
            lines.append("Watching for Teams calls")
        }
        if !processing.isEmpty {
            lines.append("Processing \(processing.count) recording(s)…")
        }
        statusLine.title = lines.joined(separator: " · ")

        startItem.isEnabled = current == nil
        stopItem.isEnabled = current != nil
        skipItem.isEnabled = current != nil || micActiveSince != nil
        pauseItem.state = paused ? .on : .off
    }

    // MARK: Watch loop

    @objc func tick() {
        let now = Date()
        let teams = audioProcesses().filter(isTeams)
        let micActive = teams.contains { $0.runningInput }

        if micActive {
            if micActiveSince == nil {
                micActiveSince = now
                log("Teams opened the microphone")
            }
            micLastSeen = now
        } else if micActiveSince != nil {
            micActiveSince = nil
            log("Teams released the microphone")
        }

        let releasedLongEnough: Bool = {
            guard !micActive, let last = micLastSeen else { return !micActive }
            return now.timeIntervalSince(last) >= config.stopGraceSeconds
        }()

        if let session = current {
            if session.automatic && releasedLongEnough {
                stopRecording(reason: "Teams released the mic for \(Int(config.stopGraceSeconds))s")
            }
        } else {
            if releasedLongEnough { skipCurrentCall = false }
            if micActive, !paused, !skipCurrentCall, let since = micActiveSince,
               now.timeIntervalSince(since) >= config.startDebounceSeconds {
                let target = config.captureTeamsAudioOnly ? teams.first(where: { $0.runningOutput })?.pid : nil
                startRecording(targetPID: target, automatic: true)
            }
        }
        updateUI()
    }

    func isTeams(_ p: AudioProc) -> Bool {
        if !p.bundleID.isEmpty, config.teamsBundlePrefixes.contains(where: { p.bundleID.hasPrefix($0) }) {
            return true
        }
        return !p.path.isEmpty && config.teamsPathMarkers.contains(where: { p.path.contains($0) })
    }

    // MARK: Start / stop

    func startRecording(targetPID: pid_t?, automatic: Bool) {
        guard current == nil else { return }
        let exe = expand(config.ownscribePath)
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            log("ownscribe not found at \(exe); pausing auto-start")
            notify("TeamsWatcher", "ownscribe not found at \(exe). Auto-start paused.")
            paused = true
            return
        }

        var args = config.extraArgs
        if let pid = targetPID { args += [config.pidFlag, String(pid)] }

        try? FileManager.default.createDirectory(atPath: logDirectory, withIntermediateDirectories: true)
        let sessionLog = URL(fileURLWithPath: logDirectory)
            .appendingPathComponent("ownscribe-\(stampFormatter.string(from: Date())).log")

        // `script` gives ownscribe a pseudo-terminal so its TUI and Ctrl+C handling behave
        // exactly as in Terminal, and records the session output to a log file.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/script")
        process.arguments = ["-q", sessionLog.path, exe] + args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["TERM"] = "xterm-256color"
        process.environment = env
        let stdinPipe = Pipe() // held open so `script` never sends EOF to ownscribe
        process.standardInput = stdinPipe
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        let session = Session(process: process, stdinPipe: stdinPipe, logURL: sessionLog,
                              automatic: automatic, teamsOnly: targetPID != nil)
        process.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            DispatchQueue.main.async { self?.sessionEnded(session, status: status) }
        }

        do {
            try process.run()
        } catch {
            log("Failed to launch ownscribe: \(error)")
            notify("TeamsWatcher", "Couldn't launch ownscribe. See the log.")
            return
        }

        current = session
        log("Started ownscribe (\(automatic ? "auto" : "manual")) args=\(args) log=\(sessionLog.lastPathComponent)")
        notify("Recording Teams call",
               targetPID != nil ? "Capturing Teams audio + your mic." : "Capturing all system audio + your mic.")
        updateUI()
    }

    func stopRecording(reason: String) {
        guard let session = current else { return }
        let scriptPID = session.process.processIdentifier
        // ownscribe runs as the session leader of the pty, so signalling its process
        // group is the same as pressing Ctrl+C in Terminal.
        if let child = childPIDs(of: scriptPID).first {
            if killpg(child, SIGINT) != 0 { kill(child, SIGINT) }
        } else {
            kill(scriptPID, SIGINT)
        }
        current = nil
        processing.append(session)
        log("Stop requested: \(reason)")
        notify("Teams call ended", "Transcribing and summarizing locally…")
        updateUI()
    }

    func sessionEnded(_ session: Session, status: Int32) {
        try? session.stdinPipe.fileHandleForWriting.close()
        let runtime = Date().timeIntervalSince(session.started)

        if current === session {
            // ownscribe stopped by itself (silence timeout or error).
            current = nil
            if runtime < 60 {
                paused = true
                log("ownscribe exited after \(Int(runtime))s with status \(status); pausing auto-start")
                notify("ownscribe stopped unexpectedly",
                       "Auto-start paused. See \(session.logURL.lastPathComponent).")
                updateUI()
                return
            }
            log("ownscribe stopped on its own after \(Int(runtime))s (status \(status))")
        }

        processing.removeAll { $0 === session }
        log("ownscribe finished with status \(status); output log \(session.logURL.lastPathComponent)")
        if status == 0 {
            notify("Meeting notes ready", "Saved in \(config.transcriptsDir)")
        } else {
            notify("ownscribe exited with an error", "See \(session.logURL.lastPathComponent)")
        }
        updateUI()
    }

    func notify(_ title: String, _ body: String) {
        guard config.notifications else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", "display notification \"\(appleScriptEscape(body))\" with title \"\(appleScriptEscape(title))\""]
        try? process.run()
    }

    // MARK: Menu actions

    @objc func manualStart() {
        let teams = audioProcesses().filter(isTeams)
        let target = config.captureTeamsAudioOnly ? teams.first(where: { $0.runningOutput })?.pid : nil
        startRecording(targetPID: target, automatic: false)
    }

    @objc func manualStop() { stopRecording(reason: "stopped from menu") }

    @objc func skipCall() {
        skipCurrentCall = true
        if current != nil { stopRecording(reason: "skip this call") }
        log("Skipping current call until Teams releases the mic")
        updateUI()
    }

    @objc func togglePause() {
        paused.toggle()
        log(paused ? "Auto-start paused" : "Auto-start resumed")
        updateUI()
    }

    @objc func openTranscripts() {
        let path = expand(config.transcriptsDir)
        try? FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc func openLogs() {
        try? FileManager.default.createDirectory(atPath: logDirectory, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: logDirectory))
    }

    @objc func dumpAudioProcesses() {
        let procs = audioProcesses()
        var lines = ["Audio processes (\(procs.count)):"]
        for p in procs {
            lines.append("  pid=\(p.pid) in=\(p.runningInput) out=\(p.runningOutput) teams=\(isTeams(p)) bundle=\(p.bundleID) path=\(p.path)")
        }
        log(lines.joined(separator: "\n"))
        NSWorkspace.shared.open(logFileURL)
    }

    @objc func editConfig() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-t", configPath]
        try? process.run()
    }

    @objc func reloadConfig() {
        applyConfig(loadConfig())
        log("Config reloaded")
        updateUI()
    }

    @objc func quit() {
        if current != nil || !processing.isEmpty {
            NSApp.activate()
            let alert = NSAlert()
            alert.messageText = "Quit TeamsWatcher?"
            alert.informativeText = current != nil
                ? "The current recording will be stopped. ownscribe should keep transcribing it in the background."
                : "ownscribe is still processing a recording and should finish in the background."
            alert.addButton(withTitle: "Quit")
            alert.addButton(withTitle: "Cancel")
            guard alert.runModal() == .alertFirstButtonReturn else { return }
            if current != nil { stopRecording(reason: "app quit") }
        }
        NSApp.terminate(nil)
    }
}

// MARK: - Entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
