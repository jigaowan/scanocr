import Foundation
import NaturalLanguage
import Translation
import Vision

private struct Command: Decodable {
    let operation: String
    let image_path: String?
    let source_language: String?
    let target_language: String?
    let text: String?
}

private enum HelperFailure: Error {
    case coded(String, String)
}

private func languageIdentifier(_ language: Locale.Language) -> String {
    language.minimalIdentifier
}

private func respond(_ value: [String: Any]) {
    do {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
    } catch {
        FileHandle.standardError.write(Data("unable to encode response: \(error)\n".utf8))
        exit(2)
    }
}

private func capabilities() throws -> [String: Any] {
    var request = RecognizeTextRequest(.revision3)
    request.recognitionLevel = .accurate
    let visionLanguages = request.supportedRecognitionLanguages.map(languageIdentifier)
    return [
        "ok": true,
        "vision_languages": visionLanguages.sorted(),
        "vision_revision": 3,
        "translation_languages": "dynamic",
        "translation_pairs": "queried per request"
    ]
}

private func recognize(_ command: Command) async throws -> [String: Any] {
    guard let path = command.image_path, FileManager.default.fileExists(atPath: path) else {
        throw HelperFailure.coded("invalid_input", "image_path does not exist")
    }
    var request = RecognizeTextRequest(.revision3)
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let requestedLanguage = command.source_language ?? "auto"
    if requestedLanguage == "auto" {
        request.automaticallyDetectsLanguage = true
    } else {
        request.recognitionLanguages = [Locale.Language(identifier: requestedLanguage)]
    }
    let observations: [RecognizedTextObservation]
    do {
        observations = try await request.perform(on: URL(fileURLWithPath: path))
    } catch {
        throw HelperFailure.coded("execution_failed", "Vision request failed: \(error.localizedDescription)")
    }
    var lines: [String] = []
    var regions: [[String: Any]] = []
    var confidences: [Float] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else { continue }
        lines.append(candidate.string)
        confidences.append(candidate.confidence)
        let points = [observation.topLeft, observation.topRight, observation.bottomRight, observation.bottomLeft]
        let minX = points.map(\.x).min() ?? 0
        let maxX = points.map(\.x).max() ?? 0
        let minY = points.map(\.y).min() ?? 0
        let maxY = points.map(\.y).max() ?? 0
        regions.append([
            "text": candidate.string,
            "confidence": candidate.confidence,
            "x": minX,
            "y": minY,
            "width": maxX - minX,
            "height": maxY - minY
        ])
    }
    let confidence: Any
    if confidences.isEmpty {
        confidence = NSNull()
    } else {
        confidence = confidences.reduce(Float(0), +) / Float(confidences.count)
    }
    return [
        "ok": true,
        "text": lines.joined(separator: "\n"),
        "detected_language": NSNull(),
        "confidence": confidence,
        "regions": regions,
        "model_name": "VNRecognizeTextRequest",
        "model_version": "revision-3"
    ]
}

private func inferredLanguage(for text: String) throws -> Locale.Language {
    let recognizer = NLLanguageRecognizer()
    recognizer.processString(text)
    guard let language = recognizer.dominantLanguage else {
        throw HelperFailure.coded("unsupported_language", "unable to identify source language")
    }
    return Locale.Language(identifier: language.rawValue)
}

private func translateInstalled(
    text: String,
    source: Locale.Language,
    target: Locale.Language
) async throws -> [String: Any] {
    let status = await LanguageAvailability().status(from: source, to: target)
    switch status {
    case .unsupported:
        throw HelperFailure.coded("unsupported_language", "the requested language pair is unsupported")
    case .supported:
        throw HelperFailure.coded("model_not_installed", "the requested Apple translation model is not installed")
    case .installed:
        break
    @unknown default:
        throw HelperFailure.coded("engine_unavailable", "unknown Apple Translation language status")
    }
    let session = TranslationSession(installedSource: source, target: target)
    do {
        let result = try await session.translate(text)
        return [
            "ok": true,
            "text": result.targetText,
            "detected_source_language": languageIdentifier(result.sourceLanguage),
            "model_name": "Apple Translation",
            "model_version": ProcessInfo.processInfo.operatingSystemVersionString
        ]
    } catch {
        if TranslationError.notInstalled ~= error {
            throw HelperFailure.coded("model_not_installed", error.localizedDescription)
        }
        if TranslationError.unsupportedSourceLanguage ~= error ||
            TranslationError.unsupportedTargetLanguage ~= error ||
            TranslationError.unsupportedLanguagePairing ~= error {
            throw HelperFailure.coded("unsupported_language", error.localizedDescription)
        }
        if TranslationError.nothingToTranslate ~= error {
            throw HelperFailure.coded("invalid_input", error.localizedDescription)
        }
        throw HelperFailure.coded("execution_failed", "Apple Translation failed: \(error.localizedDescription)")
    }
}

private func translate(_ command: Command) async throws -> [String: Any] {
    guard let text = command.text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        throw HelperFailure.coded("invalid_input", "text must not be empty")
    }
    guard let targetIdentifier = command.target_language, !targetIdentifier.isEmpty else {
        throw HelperFailure.coded("invalid_input", "target_language is required")
    }
    let source: Locale.Language
    if command.source_language == nil || command.source_language == "auto" {
        source = try inferredLanguage(for: text)
    } else {
        source = Locale.Language(identifier: command.source_language!)
    }
    let target = Locale.Language(identifier: targetIdentifier)
    return try await translateInstalled(text: text, source: source, target: target)
}

@main
private struct ScanOCRNativeHelper {
    static func main() async {
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            let command = try JSONDecoder().decode(Command.self, from: data)
            let result: [String: Any]
            switch command.operation {
            case "capabilities":
                result = try capabilities()
            case "ocr":
                result = try await recognize(command)
            case "translate":
                result = try await translate(command)
            default:
                throw HelperFailure.coded("invalid_input", "unknown operation: \(command.operation)")
            }
            respond(result)
        } catch HelperFailure.coded(let code, let message) {
            respond(["ok": false, "error_code": code, "error_message": message])
        } catch {
            respond(["ok": false, "error_code": "execution_failed", "error_message": error.localizedDescription])
        }
    }
}
