// Ask My Meetings: a small window that asks questions across all transcripts.
//
// Each question runs `ownscribe ask --json "<question>"`, which finds the relevant
// passages and has the local model (llama.cpp) answer with numbered sources. Its output
// is one JSON object per line: status, token (streamed answer text), answer, sources, error.
// Nothing leaves the Mac.

import AppKit
import SwiftUI

struct AskSource: Identifiable {
    let id = UUID()
    let ref: Int
    let title: String
    let date: String
    let time: String
    let at: String
    let notes: String

    var label: String {
        let place = at.isEmpty ? "summary" : "at \(at)"
        return "[\(ref)] \(date) \(time) · \(title) (\(place))"
    }
}

final class AskExchange: ObservableObject, Identifiable {
    let id = UUID()
    let question: String
    @Published var answer = ""
    @Published var status: String? = "Searching your meetings…"
    @Published var sources: [AskSource] = []
    @Published var error: String?
    @Published var done = false

    init(question: String) { self.question = question }
}

final class AskModel: ObservableObject {
    @Published var exchanges: [AskExchange] = []
    @Published var input = ""
    @Published var busy = false
    var ownscribePath = ""
    private var process: Process?

    static let examples = [
        "What did we decide in yesterday's meetings?",
        "What was I asked to do last week?",
        "When is the go-live date?",
    ]

    func ask(_ text: String? = nil) {
        let question = (text ?? input).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !question.isEmpty, !busy else { return }
        input = ""
        let exchange = AskExchange(question: question)
        exchanges.append(exchange)
        busy = true
        run(question, exchange)
    }

    func stop() {
        process?.terminate()
    }

    func clear() {
        guard !busy else { return }
        exchanges.removeAll()
    }

    private func run(_ question: String, _ exchange: AskExchange) {
        guard FileManager.default.isExecutableFile(atPath: ownscribePath) else {
            finish(exchange, status: -1, errorText: "ownscribe wasn't found at \(ownscribePath).")
            return
        }
        let errorURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("teamswatcher-ask-\(UUID().uuidString).log")
        FileManager.default.createFile(atPath: errorURL.path, contents: nil)

        let process = Process()
        process.executableURL = URL(fileURLWithPath: ownscribePath)
        process.arguments = ["ask", "--json", question]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = shellPath
        env["PYTHONUNBUFFERED"] = "1"
        process.environment = env
        let output = Pipe()
        process.standardOutput = output
        process.standardError = (try? FileHandle(forWritingTo: errorURL)) ?? FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice

        do {
            try process.run()
        } catch {
            try? FileManager.default.removeItem(at: errorURL)
            finish(exchange, status: -1, errorText: "Couldn't start ownscribe: \(error.localizedDescription)")
            return
        }
        self.process = process

        // Read the JSON lines as they arrive, so the answer appears while it is written.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let handle = output.fileHandleForReading
            var pending = Data()
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break } // end of output
                pending.append(chunk)
                while let newline = pending.firstIndex(of: 0x0A) {
                    let line = Data(pending[pending.startIndex..<newline])
                    pending = Data(pending[pending.index(after: newline)...])
                    DispatchQueue.main.async { self?.handle(line, exchange) }
                }
            }
            if !pending.isEmpty {
                let line = pending
                DispatchQueue.main.async { self?.handle(line, exchange) }
            }
            process.waitUntilExit()
            let errorText = (try? String(contentsOf: errorURL, encoding: .utf8)) ?? ""
            try? FileManager.default.removeItem(at: errorURL)
            let status = process.terminationStatus
            DispatchQueue.main.async { self?.finish(exchange, status: status, errorText: errorText) }
        }
    }

    private func handle(_ line: Data, _ exchange: AskExchange) {
        guard let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
              let event = object["event"] as? String else { return }
        switch event {
        case "status":
            exchange.status = object["message"] as? String
        case "token":
            exchange.answer += object["text"] as? String ?? ""
        case "answer":
            exchange.answer = object["text"] as? String ?? exchange.answer
        case "sources":
            let list = object["sources"] as? [[String: Any]] ?? []
            exchange.sources = list.map { s in
                AskSource(ref: s["ref"] as? Int ?? 0,
                          title: s["title"] as? String ?? "",
                          date: s["date"] as? String ?? "",
                          time: s["time"] as? String ?? "",
                          at: s["at"] as? String ?? "",
                          notes: s["notes"] as? String ?? "")
            }
        case "error":
            exchange.error = object["message"] as? String
        default:
            break
        }
    }

    private func finish(_ exchange: AskExchange, status: Int32, errorText: String) {
        process = nil
        busy = false
        exchange.done = true
        exchange.status = nil
        if status == 15 || status == 143 {
            if exchange.answer.isEmpty { exchange.error = "Stopped." }
        } else if status != 0, exchange.error == nil {
            let detail = errorText.trimmingCharacters(in: .whitespacesAndNewlines)
            let tail = detail.split(separator: "\n").suffix(6).joined(separator: "\n")
            exchange.error = tail.isEmpty ? "ownscribe exited with status \(status)." : tail
            log("Ask My Meetings failed (status \(status)): \(tail)")
        }
    }
}

// MARK: - Views

struct AskView: View {
    @ObservedObject var model: AskModel

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 22) {
                        if model.exchanges.isEmpty {
                            AskIntro(model: model)
                        }
                        ForEach(model.exchanges) { exchange in
                            ExchangeView(exchange: exchange).id(exchange.id)
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: model.exchanges.count) { _ in
                    withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
                }
            }
            Divider()
            HStack(alignment: .bottom, spacing: 8) {
                if !model.exchanges.isEmpty {
                    Button {
                        model.clear()
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.borderless)
                    .disabled(model.busy)
                    .help("Clear these questions")
                }
                TextField("Ask about your meetings…", text: $model.input, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .onSubmit { model.ask() }
                    .disabled(model.busy)
                if model.busy {
                    Button("Stop") { model.stop() }
                } else {
                    Button("Ask") { model.ask() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(model.input.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .padding(12)
        }
        .frame(minWidth: 480, minHeight: 420)
    }
}

struct AskIntro: View {
    @ObservedObject var model: AskModel

    static let blurb = """
        Answers come from the local model on this Mac and cite the meetings they're based on. \
        Each question is answered on its own, so say what you mean in full. You can name a time, \
        like "yesterday", "last week" or "on Monday". The first question downloads the model \
        (about 5 GB) unless Create Summary already has.
        """

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Ask questions across your meeting transcripts.")
                .font(.headline)
            Text(Self.blurb)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(AskModel.examples, id: \.self) { example in
                Button(example) { model.ask(example) }
                    .buttonStyle(.link)
            }
        }
    }
}

struct ExchangeView: View {
    @ObservedObject var exchange: AskExchange

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(exchange.question)
                .font(.headline)
                .textSelection(.enabled)
            if !exchange.answer.isEmpty {
                Text(markdown(exchange.answer))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let status = exchange.status, !exchange.done {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text(status).foregroundColor(.secondary)
                }
            }
            if let error = exchange.error {
                Text(error)
                    .foregroundColor(.red)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !exchange.sources.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Sources").font(.caption).foregroundColor(.secondary)
                    ForEach(exchange.sources) { source in
                        Button(source.label) { openInTextEditor(source.notes) }
                            .buttonStyle(.link)
                            .help("Open the notes for this meeting")
                            .contextMenu {
                                Button("Show in Finder") { openOrReveal(source.notes, reveal: true) }
                            }
                    }
                }
                .padding(.top, 2)
            }
        }
    }

    func markdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
    }
}
