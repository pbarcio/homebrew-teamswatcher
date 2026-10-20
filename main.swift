// TeamsWatcher — menu bar app that auto-starts ownscribe when a meeting app (Teams, Zoom,
// Webex, ...) opens the microphone, and stops it (Ctrl+C equivalent) when the app releases it.
//
// Everything stays local: this app only watches Core Audio and launches ownscribe.
// Requires macOS 14.2+ (Core Audio process objects). Build with ./build.sh.

import AppKit
import CoreAudio
import Darwin
import UserNotifications

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

/// Main-thread only (call sites on other threads hop to main first).
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

/// A meeting app whose microphone use starts a recording.
struct MeetingApp: Codable {
    var name: String
    var enabled: Bool = true
    /// Audio processes whose bundle ID starts with one of these belong to the app.
    /// Also passed to ownscribe (--app) so only this app's audio is captured.
    var bundlePrefixes: [String] = []
    /// Fallback match on executable path (helpers without a bundle ID).
    var pathMarkers: [String] = []

    init(name: String, enabled: Bool, bundlePrefixes: [String], pathMarkers: [String]) {
        self.name = name
        self.enabled = enabled
        self.bundlePrefixes = bundlePrefixes
        self.pathMarkers = pathMarkers
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? "Meeting app"
        if let v = try c.decodeIfPresent(Bool.self, forKey: .enabled) { enabled = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .bundlePrefixes) { bundlePrefixes = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .pathMarkers) { pathMarkers = v }
    }

    func matches(_ p: AudioProc) -> Bool {
        if !p.bundleID.isEmpty, bundlePrefixes.contains(where: { p.bundleID.hasPrefix($0) }) {
            return true
        }
        return !p.path.isEmpty && pathMarkers.contains(where: { p.path.contains($0) })
    }

    static let defaults: [MeetingApp] = [
        MeetingApp(name: "Microsoft Teams", enabled: true,
                   bundlePrefixes: ["com.microsoft.teams"], pathMarkers: ["Microsoft Teams"]),
        MeetingApp(name: "Zoom", enabled: true,
                   bundlePrefixes: ["us.zoom."], pathMarkers: ["zoom.us.app"]),
        MeetingApp(name: "Webex", enabled: true,
                   bundlePrefixes: ["Cisco-Systems.Spark", "com.webex.", "com.cisco.webex"],
                   pathMarkers: ["Webex.app", "Cisco Webex Meetings.app"]),
        MeetingApp(name: "Slack huddles", enabled: false,
                   bundlePrefixes: ["com.tinyspeck.slackmacgap"], pathMarkers: ["Slack.app"]),
        // Browsers: any site that uses the mic (Google Meet, Teams on the web, ...) starts a recording.
        MeetingApp(name: "Google Chrome", enabled: false,
                   bundlePrefixes: ["com.google.Chrome"], pathMarkers: ["Google Chrome.app"]),
        MeetingApp(name: "Microsoft Edge", enabled: false,
                   bundlePrefixes: ["com.microsoft.edgemac"], pathMarkers: ["Microsoft Edge.app"]),
        MeetingApp(name: "Safari", enabled: false,
                   bundlePrefixes: ["com.apple.Safari", "com.apple.WebKit"], pathMarkers: ["Safari.app"]),
    ]
}

struct Config: Codable {
    /// Where `uv tool install ownscribe` puts the command.
    var ownscribePath = "~/.local/bin/ownscribe"
    /// Extra ownscribe flags, e.g. ["--template", "meeting"].
    var extraArgs: [String] = []
    /// Apps that trigger recording. Set "enabled" to turn one on or off.
    var meetingApps: [MeetingApp] = MeetingApp.defaults
    /// The app must hold the mic this long before recording starts (skips device tests / pre-join).
    var startDebounceSeconds = 10.0
    /// After the app releases the mic, wait this long before stopping (covers brief drops).
    var stopGraceSeconds = 30.0
    var pollIntervalSeconds = 2.0
    /// true = record only the meeting app's audio (plus your mic); false = all system audio.
    var captureAppAudioOnly = true
    /// ownscribe's flag for limiting capture to an app's bundle ID.
    var appFlag = "--app"
    var notifications = true
    var transcriptsDir = "~/ownscribe"
    var logDir = "~/Library/Logs/TeamsWatcher"

    init() {}

    // Tolerant decoding: any key missing from the file keeps its default.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let v = try c.decodeIfPresent(String.self, forKey: .ownscribePath) { ownscribePath = v }
        if let v = try c.decodeIfPresent([String].self, forKey: .extraArgs) { extraArgs = v }
        if let v = try c.decodeIfPresent([MeetingApp].self, forKey: .meetingApps) { meetingApps = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .startDebounceSeconds) { startDebounceSeconds = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .stopGraceSeconds) { stopGraceSeconds = v }
        if let v = try c.decodeIfPresent(Double.self, forKey: .pollIntervalSeconds) { pollIntervalSeconds = v }
        if let v = try c.decodeIfPresent(Bool.self, forKey: .captureAppAudioOnly) { captureAppAudioOnly = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .appFlag) { appFlag = v }
        if let v = try c.decodeIfPresent(Bool.self, forKey: .notifications) { notifications = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .transcriptsDir) { transcriptsDir = v }
        if let v = try c.decodeIfPresent(String.self, forKey: .logDir) { logDir = v }
        // Settings from the Teams-only version.
        let legacy = try decoder.container(keyedBy: LegacyKeys.self)
        if !c.contains(.captureAppAudioOnly),
           let v = try legacy.decodeIfPresent(Bool.self, forKey: .captureTeamsAudioOnly) {
            captureAppAudioOnly = v
        }
    }

    private enum LegacyKeys: String, CodingKey {
        case captureTeamsAudioOnly
    }
}

func encodeConfig(_ cfg: Config) -> Data? {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    return try? encoder.encode(cfg)
}

func loadConfig() -> Config {
    let url = URL(fileURLWithPath: configPath)
    if let data = try? Data(contentsOf: url) {
        do {
            let cfg = try JSONDecoder().decode(Config.self, from: data)
            // Rewrite configs from older versions so new settings (like meetingApps) appear in the file.
            let existingKeys = Set(((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?.keys.map { $0 } ?? [])
            if let encoded = encodeConfig(cfg),
               let currentKeys = (try? JSONSerialization.jsonObject(with: encoded)) as? [String: Any],
               Set(currentKeys.keys) != existingKeys {
                try? data.write(to: url.appendingPathExtension("bak"))
                try? encoded.write(to: url)
            }
            return cfg
        } catch {
            log("Config parse error, using defaults: \(error)")
            return Config()
        }
    }
    let cfg = Config()
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    if let data = encodeConfig(cfg) { try? data.write(to: url) }
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

// MARK: - Notifications

func appleScriptEscape(_ s: String) -> String {
    s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"")
}

/// Fallback when macOS notification permission isn't available (not clickable).
func notifyViaAppleScript(_ title: String, _ body: String) {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
    process.arguments = ["-e", "display notification \"\(appleScriptEscape(body))\" with title \"\(appleScriptEscape(title))\""]
    try? process.run()
}

// MARK: - Recording session

final class Session {
    let process: Process
    let stdinPipe: Pipe
    let logURL: URL
    let resultURL: URL
    let started = Date()
    let automatic: Bool
    let appName: String
    let appOnly: Bool

    init(process: Process, stdinPipe: Pipe, logURL: URL, resultURL: URL,
         automatic: Bool, appName: String, appOnly: Bool) {
        self.process = process
        self.stdinPipe = stdinPipe
        self.logURL = logURL
        self.resultURL = resultURL
        self.automatic = automatic
        self.appName = appName
        self.appOnly = appOnly
    }

    /// Paths ownscribe reported when it finished (output_dir, transcript, summary, export).
    func results() -> [String: String] {
        guard let data = try? Data(contentsOf: resultURL),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: String] else { return [:] }
        return object
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    var config = Config()
    var statusItem: NSStatusItem!
    var timer: Timer?

    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    var startItem: NSMenuItem!
    var stopItem: NSMenuItem!
    var skipItem: NSMenuItem!
    var pauseItem: NSMenuItem!
    var latestNotesItem: NSMenuItem!

    var current: Session?
    var processing: [Session] = []
    var paused = false
    var skipCurrentCall = false
    /// The meeting app currently holding the mic, and since when.
    var activeApp: MeetingApp?
    var micActiveSince: Date?
    var micLastSeen: Date?
    var latestNotesPath: String?
    var clickableNotifications = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        applyConfig(loadConfig())
        buildMenu()
        setUpNotifications()
        log("TeamsWatcher started. Config: \(configPath)")
        let watched = config.meetingApps.filter { $0.enabled }.map { $0.name }
        log("Watching: \(watched.joined(separator: ", "))")
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

    // MARK: Notifications

    func setUpNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            DispatchQueue.main.async {
                self.clickableNotifications = granted
                if let error {
                    log("Notification permission unavailable (\(error.localizedDescription)); using basic notifications")
                } else if !granted {
                    log("Notifications not allowed for TeamsWatcher; using basic notifications")
                }
            }
        }
    }

    /// Shows a notification. With `open`, clicking it opens that file or folder.
    func notify(_ title: String, _ body: String, open path: String? = nil) {
        guard config.notifications else { return }
        guard clickableNotifications else {
            notifyViaAppleScript(title, body)
            return
        }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        if let path { content.userInfo = ["path": path] }
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            guard let error else { return }
            DispatchQueue.main.async {
                log("Notification failed (\(error.localizedDescription)); using basic notification")
                notifyViaAppleScript(title, body)
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let path = response.notification.request.content.userInfo["path"] as? String {
            DispatchQueue.main.async { NSWorkspace.shared.open(URL(fileURLWithPath: path)) }
        }
        completionHandler()
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
        latestNotesItem = item("Open Latest Notes", #selector(openLatestNotes))
        menu.addItem(latestNotesItem)
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
            let source = s.appOnly ? "\(s.appName) audio" : "all system audio"
            lines.append("Recording since \(clockFormatter.string(from: s.started)) (\(source) + mic)")
        } else if paused {
            lines.append("Auto-start paused")
        } else if skipCurrentCall {
            lines.append("Skipping this call")
        } else {
            lines.append("Watching for calls")
        }
        if !processing.isEmpty {
            lines.append("Processing \(processing.count) recording(s)…")
        }
        statusLine.title = lines.joined(separator: " · ")

        startItem.isEnabled = current == nil
        stopItem.isEnabled = current != nil
        skipItem.isEnabled = current != nil || micActiveSince != nil
        pauseItem.state = paused ? .on : .off
        latestNotesItem.isEnabled = latestNotesPath != nil
    }

    // MARK: Watch loop

    /// The first enabled meeting app that has the mic open, with its audio processes.
    func meetingAppUsingMic(_ procs: [AudioProc]) -> (app: MeetingApp, procs: [AudioProc])? {
        for app in config.meetingApps where app.enabled {
            let mine = procs.filter(app.matches)
            if mine.contains(where: { $0.runningInput }) {
                return (app, mine)
            }
        }
        return nil
    }

    @objc func tick() {
        let now = Date()
        let inUse = meetingAppUsingMic(audioProcesses())

        if let inUse {
            if micActiveSince == nil {
                micActiveSince = now
                log("\(inUse.app.name) opened the microphone")
            }
            activeApp = inUse.app
            micLastSeen = now
        } else if micActiveSince != nil {
            micActiveSince = nil
            log("\(activeApp?.name ?? "Meeting app") released the microphone")
        }
        let micActive = inUse != nil

        let releasedLongEnough: Bool = {
            guard !micActive, let last = micLastSeen else { return !micActive }
            return now.timeIntervalSince(last) >= config.stopGraceSeconds
        }()

        if let session = current {
            if session.automatic && releasedLongEnough {
                stopRecording(reason: "\(session.appName) released the mic for \(Int(config.stopGraceSeconds))s")
            }
        } else {
            if releasedLongEnough { skipCurrentCall = false }
            if let inUse, !paused, !skipCurrentCall, let since = micActiveSince,
               now.timeIntervalSince(since) >= config.startDebounceSeconds {
                startRecording(app: inUse.app, automatic: true)
            }
        }
        updateUI()
    }

    // MARK: Start / stop

    func startRecording(app: MeetingApp?, automatic: Bool) {
        guard current == nil else { return }
        let exe = expand(config.ownscribePath)
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            log("ownscribe not found at \(exe); pausing auto-start")
            notify("TeamsWatcher", "ownscribe not found at \(exe). Auto-start paused.")
            paused = true
            return
        }

        var args = config.extraArgs
        let appOnly = config.captureAppAudioOnly && !(app?.bundlePrefixes.isEmpty ?? true)
        if appOnly, let app {
            for prefix in app.bundlePrefixes { args += [config.appFlag, prefix] }
        }
        let appName = app?.name ?? "Meeting"

        try? FileManager.default.createDirectory(atPath: logDirectory, withIntermediateDirectories: true)
        let stamp = stampFormatter.string(from: Date())
        let sessionLog = URL(fileURLWithPath: logDirectory).appendingPathComponent("ownscribe-\(stamp).log")
        let resultURL = URL(fileURLWithPath: logDirectory).appendingPathComponent("result-\(stamp).json")

        // `script` gives ownscribe a pseudo-terminal so its TUI and Ctrl+C handling behave
        // exactly as in Terminal, and records the session output to a log file.
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/script")
        process.arguments = ["-q", sessionLog.path, exe] + args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["TERM"] = "xterm-256color"
        env["OWNSCRIBE_RESULT_FILE"] = resultURL.path
        env["OWNSCRIBE_SOURCE_APP"] = appName
        process.environment = env
        let stdinPipe = Pipe() // held open so `script` never sends EOF to ownscribe
        process.standardInput = stdinPipe
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        let session = Session(process: process, stdinPipe: stdinPipe, logURL: sessionLog, resultURL: resultURL,
                              automatic: automatic, appName: appName, appOnly: appOnly)
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
        log("Started ownscribe (\(automatic ? "auto" : "manual"), \(appName)) args=\(args) log=\(sessionLog.lastPathComponent)")
        notify("Recording \(appName) call",
               appOnly ? "Capturing \(appName) audio + your mic." : "Capturing all system audio + your mic.")
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
        notify("\(session.appName) call ended", "Transcribing and summarizing locally…")
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
                       "Auto-start paused. Click to see the log.", open: session.logURL.path)
                updateUI()
                return
            }
            log("ownscribe stopped on its own after \(Int(runtime))s (status \(status))")
        }

        processing.removeAll { $0 === session }
        let results = session.results()
        try? FileManager.default.removeItem(at: session.resultURL)
        log("ownscribe finished with status \(status); results=\(results); log=\(session.logURL.lastPathComponent)")

        let notes = results["export"] ?? results["summary"] ?? results["transcript"] ?? results["output_dir"]
        if status == 0, let notes {
            latestNotesPath = notes
            let name = (notes as NSString).lastPathComponent
            notify("Meeting notes ready", "Click to open \(name)", open: notes)
        } else if status == 0 {
            notify("Meeting notes ready", "Saved in \(config.transcriptsDir)", open: expand(config.transcriptsDir))
        } else {
            notify("ownscribe exited with an error", "Click to see the log.", open: session.logURL.path)
        }
        updateUI()
    }

    // MARK: Menu actions

    @objc func manualStart() {
        let inUse = meetingAppUsingMic(audioProcesses())
        startRecording(app: inUse?.app, automatic: false)
    }

    @objc func manualStop() { stopRecording(reason: "stopped from menu") }

    @objc func skipCall() {
        skipCurrentCall = true
        if current != nil { stopRecording(reason: "skip this call") }
        log("Skipping current call until the app releases the mic")
        updateUI()
    }

    @objc func togglePause() {
        paused.toggle()
        log(paused ? "Auto-start paused" : "Auto-start resumed")
        updateUI()
    }

    @objc func openLatestNotes() {
        guard let path = latestNotesPath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
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
            let app = config.meetingApps.first(where: { $0.matches(p) })
            let appLabel = app.map { "\($0.name)\($0.enabled ? "" : " (disabled)")" } ?? "-"
            lines.append("  pid=\(p.pid) in=\(p.runningInput) out=\(p.runningOutput) app=\(appLabel) bundle=\(p.bundleID) path=\(p.path)")
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
        log("Config reloaded. Watching: \(config.meetingApps.filter { $0.enabled }.map { $0.name }.joined(separator: ", "))")
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
