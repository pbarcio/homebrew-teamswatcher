// TeamsWatcher Settings window (SwiftUI).
//
// TeamsWatcher's own settings are edited in memory and saved to its JSON config.
// Transcription settings live in ownscribe's config.toml; they're read with
// `ownscribe settings show` and saved with `ownscribe settings set`, which change only
// the affected lines and keep the file's comments.

import AppKit
import SwiftUI

// MARK: - Model

struct EditableApp: Identifiable {
    let id = UUID()
    var app: MeetingApp
    let isBuiltIn: Bool
}

enum CopyMode: String, CaseIterable, Identifiable {
    case none, oneDrive, folder
    var id: String { rawValue }
}

struct WhisperModelOption: Identifiable {
    let tag: String
    let label: String
    var id: String { tag }
}

let knownWhisperModels: [WhisperModelOption] = [
    WhisperModelOption(tag: "large-v3-turbo", label: "Large v3 Turbo (recommended)"),
    WhisperModelOption(tag: "large-v3", label: "Large v3 (most accurate, slower)"),
    WhisperModelOption(tag: "medium", label: "Medium (lighter)"),
    WhisperModelOption(tag: "small", label: "Small (fastest, least accurate)"),
]

final class SettingsModel: ObservableObject {
    // TeamsWatcher (config.json)
    @Published var saveRecording = false
    @Published var createSummary = false
    @Published var notifications = true
    @Published var captureAppAudioOnly = true
    @Published var startDebounceSeconds = 10.0
    @Published var stopGraceSeconds = 30.0
    @Published var meetingApps: [EditableApp] = []
    @Published var newAppName = ""
    @Published var newAppBundleID = ""

    // Transcription (ownscribe config.toml)
    @Published var transcriptionLoaded = false
    @Published var transcriptionError: String?
    @Published var myName = ""
    @Published var myNamePlaceholder = "Your macOS name"
    @Published var theirLabel = "Them"
    @Published var english = true
    @Published var spanish = false
    @Published var otherLanguages = ""
    @Published var model = "large-v3-turbo"
    @Published var vocabulary = ""
    @Published var hotwords = ""
    @Published var outputDir = "~/ownscribe"
    @Published var writeNotes = true
    @Published var includeTranscript = true
    @Published var copyMode: CopyMode = .none
    @Published var copyFolder = ""

    // Window state
    @Published var saving = false
    @Published var saveError: String?

    /// Values as loaded, so only real changes are written back.
    private var loadedTranscription: [String: Any] = [:]

    func load(from config: Config) {
        saveRecording = config.saveRecording
        createSummary = config.createSummary
        notifications = config.notifications
        captureAppAudioOnly = config.captureAppAudioOnly
        startDebounceSeconds = config.startDebounceSeconds
        stopGraceSeconds = config.stopGraceSeconds
        let builtInNames = Set(MeetingApp.defaults.map { $0.name })
        meetingApps = config.meetingApps.map { EditableApp(app: $0, isBuiltIn: builtInNames.contains($0.name)) }
    }

    /// Fills the transcription fields from `ownscribe settings show` output.
    func loadTranscription(_ values: [String: Any]) {
        loadedTranscription = values
        myName = values["speakers.me"] as? String ?? ""
        if let fallback = values["speakers.default_me"] as? String, !fallback.isEmpty {
            myNamePlaceholder = fallback
        }
        theirLabel = values["speakers.them"] as? String ?? "Them"

        var languages = values["transcription.languages"] as? [String] ?? []
        if languages.isEmpty, let single = values["transcription.language"] as? String, !single.isEmpty {
            languages = [single]
        }
        english = languages.isEmpty || languages.contains("en")
        spanish = languages.contains("es")
        otherLanguages = languages.filter { $0 != "en" && $0 != "es" }.joined(separator: ", ")

        model = values["transcription.model"] as? String ?? "large-v3-turbo"
        vocabulary = values["transcription.initial_prompt"] as? String ?? ""
        hotwords = values["transcription.hotwords"] as? String ?? ""
        outputDir = values["output.dir"] as? String ?? "~/ownscribe"
        writeNotes = values["export.notes"] as? Bool ?? true
        includeTranscript = values["export.include_transcript"] as? Bool ?? true

        let copyTo = (values["export.copy_to"] as? String ?? "").trimmingCharacters(in: .whitespaces)
        switch copyTo.lowercased() {
        case "": copyMode = .none
        case "auto": copyMode = .oneDrive
        default:
            copyMode = .folder
            copyFolder = copyTo
        }
        transcriptionLoaded = true
        transcriptionError = nil
    }

    var languagesFooter: String {
        if languageCodes.count > 1 {
            return "People can switch languages at any point; each stretch of speech is transcribed in its own language."
        }
        return "Everything is transcribed as this one language."
    }

    var canAddNewApp: Bool {
        !newAppName.trimmingCharacters(in: .whitespaces).isEmpty
            && !newAppBundleID.trimmingCharacters(in: .whitespaces).isEmpty
    }

    func addNewApp() {
        guard canAddNewApp else { return }
        let app = MeetingApp(name: newAppName.trimmingCharacters(in: .whitespaces),
                             enabled: true,
                             bundlePrefixes: [newAppBundleID.trimmingCharacters(in: .whitespaces)],
                             pathMarkers: [])
        meetingApps.append(EditableApp(app: app, isBuiltIn: false))
        newAppName = ""
        newAppBundleID = ""
    }

    var languageCodes: [String] {
        var codes: [String] = []
        if english { codes.append("en") }
        if spanish { codes.append("es") }
        for part in otherLanguages.split(whereSeparator: { $0 == "," || $0 == " " }) {
            let code = part.trimmingCharacters(in: .whitespaces).lowercased()
            if !code.isEmpty && !codes.contains(code) { codes.append(code) }
        }
        return codes
    }

    /// Changed transcription settings as {"section.key": value}, or an error to show.
    func transcriptionChanges() -> Result<[String: Any], SettingsProblem> {
        guard transcriptionLoaded else { return .success([:]) }
        let codes = languageCodes
        if codes.isEmpty {
            return .failure(SettingsProblem("Choose at least one language."))
        }
        if let bad = codes.first(where: { !$0.allSatisfy({ $0.isLetter }) || !(2...3).contains($0.count) }) {
            return .failure(SettingsProblem("\"\(bad)\" isn't a language code. Use codes like pt or fr."))
        }
        let copyTo: String
        switch copyMode {
        case .none: copyTo = ""
        case .oneDrive: copyTo = "auto"
        case .folder:
            if copyFolder.trimmingCharacters(in: .whitespaces).isEmpty {
                return .failure(SettingsProblem("Choose a folder to copy notes to, or pick Nowhere."))
            }
            copyTo = copyFolder
        }
        if outputDir.trimmingCharacters(in: .whitespaces).isEmpty {
            return .failure(SettingsProblem("Choose a folder for recordings."))
        }

        let wanted: [String: Any] = [
            "speakers.me": myName.trimmingCharacters(in: .whitespaces),
            "speakers.them": theirLabel.trimmingCharacters(in: .whitespaces).isEmpty ? "Them" : theirLabel,
            "transcription.languages": codes,
            // One language: fix it, so Whisper never guesses wrong. Several: detect per stretch.
            "transcription.language": codes.count == 1 ? codes[0] : "",
            "transcription.model": model,
            "transcription.initial_prompt": vocabulary,
            "transcription.hotwords": hotwords,
            "output.dir": outputDir,
            "export.notes": writeNotes,
            "export.include_transcript": includeTranscript,
            "export.copy_to": copyTo,
        ]
        var changes: [String: Any] = [:]
        for (key, value) in wanted {
            if let before = loadedTranscription[key] as? NSObject, before.isEqual(value) {
                continue
            }
            changes[key] = value
        }
        return .success(changes)
    }
}

struct SettingsProblem: Error {
    let message: String
    init(_ message: String) { self.message = message }
}

// MARK: - Helpers

func chooseFolder(startingAt path: String) -> String? {
    let panel = NSOpenPanel()
    panel.canChooseDirectories = true
    panel.canChooseFiles = false
    panel.canCreateDirectories = true
    panel.allowsMultipleSelection = false
    panel.prompt = "Choose"
    panel.directoryURL = URL(fileURLWithPath: expand(path))
    guard panel.runModal() == .OK, let url = panel.url else { return nil }
    return (url.path as NSString).abbreviatingWithTildeInPath
}

struct FolderRow: View {
    let title: String
    @Binding var path: String

    var body: some View {
        LabeledContent(title) {
            HStack {
                Text(path.isEmpty ? "Not set" : path)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundColor(path.isEmpty ? Color.secondary : Color.primary)
                Button("Choose…") {
                    if let chosen = chooseFolder(startingAt: path.isEmpty ? "~" : path) {
                        path = chosen
                    }
                }
            }
        }
    }
}

// MARK: - Tabs

struct GeneralTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section("For each call") {
                Toggle("Save recording (keep the audio files)", isOn: $model.saveRecording)
                Toggle("Create summary", isOn: $model.createSummary)
                if model.createSummary {
                    Text("Made by a local AI model. The first summary downloads it (about 5 GB).")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Section("Recording") {
                Toggle("Record only the meeting app's audio, plus your mic", isOn: $model.captureAppAudioOnly)
                Stepper(value: $model.startDebounceSeconds, in: 3...60, step: 1) {
                    LabeledContent("Start once the app has used the mic for",
                                   value: "\(Int(model.startDebounceSeconds)) seconds")
                }
                Stepper(value: $model.stopGraceSeconds, in: 10...300, step: 5) {
                    LabeledContent("Stop once the mic has been released for",
                                   value: "\(Int(model.stopGraceSeconds)) seconds")
                }
            }
            Section("Notifications") {
                Toggle("Show notifications", isOn: $model.notifications)
            }
        }
        .formStyle(.grouped)
    }
}

struct AppsTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        Form {
            Section {
                ForEach($model.meetingApps) { $entry in
                    HStack {
                        Toggle(isOn: $entry.app.enabled) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(entry.app.name)
                                Text(entry.app.bundlePrefixes.joined(separator: ", "))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        if !entry.isBuiltIn {
                            Button {
                                let id = entry.id
                                DispatchQueue.main.async { model.meetingApps.removeAll { $0.id == id } }
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.borderless)
                            .help("Remove \(entry.app.name)")
                        }
                    }
                }
            } header: {
                Text("Start recording when one of these apps uses the microphone")
            } footer: {
                Text("Browsers start a recording for any website that uses the mic, such as Google Meet.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Section {
                TextField("Name", text: $model.newAppName, prompt: Text("e.g. GoTo Meeting"))
                TextField("Bundle ID", text: $model.newAppBundleID, prompt: Text("e.g. com.logmein.GoToMeeting"))
                Button("Add App") {
                    model.addNewApp()
                }
                .disabled(!model.canAddNewApp)
            } header: {
                Text("Add another app")
            } footer: {
                Text("Find an app's bundle ID in Terminal: osascript -e 'id of app \"AppName\"'")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
        .formStyle(.grouped)
    }
}

struct TranscriptionTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        if let error = model.transcriptionError {
            VStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle").font(.largeTitle).foregroundStyle(.secondary)
                Text("Couldn't read the transcription settings.")
                Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if !model.transcriptionLoaded {
            ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Form {
                Section("Speaker labels") {
                    TextField("Your name", text: $model.myName, prompt: Text(model.myNamePlaceholder))
                    TextField("Everyone else", text: $model.theirLabel, prompt: Text("Them"))
                }
                Section {
                    Toggle("English", isOn: $model.english)
                    Toggle("Spanish", isOn: $model.spanish)
                    TextField("Other languages", text: $model.otherLanguages, prompt: Text("codes, e.g. pt, fr"))
                } header: {
                    Text("Languages spoken in your calls")
                } footer: {
                    Text(model.languagesFooter)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section("Accuracy") {
                    Picker("Whisper model", selection: $model.model) {
                        ForEach(knownWhisperModels) { option in
                            Text(option.label).tag(option.tag)
                        }
                        if !knownWhisperModels.contains(where: { $0.tag == model.model }) {
                            Text(model.model).tag(model.model)
                        }
                    }
                }
                Section {
                    TextField("Vocabulary", text: $model.vocabulary, axis: .vertical)
                        .lineLimit(3...6)
                    TextField("Key terms", text: $model.hotwords, prompt: Text("comma-separated"))
                } header: {
                    Text("Vocabulary hints")
                } footer: {
                    Text("Names and terms to spell correctly, such as product, system and client names.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
        }
    }
}

struct NotesTab: View {
    @ObservedObject var model: SettingsModel

    var body: some View {
        if !model.transcriptionLoaded {
            ProgressView("Loading…").frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Form {
                Section {
                    FolderRow(title: "Recordings folder", path: $model.outputDir)
                } header: {
                    Text("Where recordings are saved")
                } footer: {
                    Text("Each recording gets its own folder here, named with the date, time and title.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Section("Notes file") {
                    Toggle("Write a notes file in each recording's folder", isOn: $model.writeNotes)
                    Toggle("Include the transcript in the notes file", isOn: $model.includeTranscript)
                        .disabled(!model.writeNotes)
                    Picker("Also copy notes to", selection: $model.copyMode) {
                        Text("Nowhere").tag(CopyMode.none)
                        Text("Work OneDrive › Meeting Notes").tag(CopyMode.oneDrive)
                        Text("A folder…").tag(CopyMode.folder)
                    }
                    .disabled(!model.writeNotes)
                    if model.copyMode == .folder {
                        FolderRow(title: "Copy to", path: $model.copyFolder)
                    }
                    if model.copyMode == .oneDrive {
                        Text("OneDrive syncs these notes to your company's cloud storage.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .formStyle(.grouped)
        }
    }
}

struct AdvancedTab: View {
    var openTeamsWatcherFile: () -> Void
    var openTranscriptionFile: () -> Void
    var reload: () -> Void

    var body: some View {
        Form {
            Section {
                Button("Open TeamsWatcher settings file", action: openTeamsWatcherFile)
                Button("Open transcription settings file", action: openTranscriptionFile)
                Button("Reload settings files", action: reload)
            } header: {
                Text("Settings files")
            } footer: {
                Text("For settings not shown in this window. After editing a file by hand, choose Reload.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - Window content

struct SettingsView: View {
    @ObservedObject var model: SettingsModel
    var onSave: () -> Void
    var onCancel: () -> Void
    var openTeamsWatcherFile: () -> Void
    var openTranscriptionFile: () -> Void
    var reload: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            TabView {
                GeneralTab(model: model).tabItem { Text("General") }
                AppsTab(model: model).tabItem { Text("Meeting Apps") }
                TranscriptionTab(model: model).tabItem { Text("Transcription") }
                NotesTab(model: model).tabItem { Text("Notes") }
                AdvancedTab(openTeamsWatcherFile: openTeamsWatcherFile,
                            openTranscriptionFile: openTranscriptionFile,
                            reload: reload)
                    .tabItem { Text("Advanced") }
            }
            .padding([.top, .horizontal])

            HStack {
                if let error = model.saveError {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                        .font(.callout)
                        .lineLimit(3)
                }
                Spacer()
                if model.saving { ProgressView().controlSize(.small) }
                Button("Cancel", action: onCancel)
                    .keyboardShortcut(.cancelAction)
                Button("Save", action: onSave)
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.saving)
            }
            .padding()
        }
        .frame(width: 620, height: 580)
    }
}
