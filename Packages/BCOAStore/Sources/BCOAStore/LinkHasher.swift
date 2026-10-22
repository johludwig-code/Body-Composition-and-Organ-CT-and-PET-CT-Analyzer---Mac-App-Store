import CryptoKit
import Foundation

/// Patient links of ADR 0024 on the app's side: the project's link key, its
/// ID, and the `pid:` link of an ID the user types.
///
/// The worker computes the same links in `bcoa_worker.index.identity`; both
/// are held to `Protocol/fixtures/link_vectors.json`, because a byte that
/// differs here would give a typed ID a patient of its own instead of the one
/// its files already link to.
public struct LinkHasher: Sendable {
    public static let keyBytes = 32
    public static let prefix = "base64:"

    public let key: Data

    public init(key: Data) {
        precondition(key.count == Self.keyBytes, "a link key is 32 bytes")
        self.key = key
    }

    /// `"base64:" + standard base64 with padding`, as in `project_meta` and the
    /// index payload. Nil for anything else, which the caller treats as no key.
    public init?(encoded: String) {
        guard encoded.hasPrefix(Self.prefix),
              let data = Data(base64Encoded: String(encoded.dropFirst(Self.prefix.count))),
              data.count == Self.keyBytes
        else { return nil }
        self.key = data
    }

    /// A fresh key from the system's cryptographic generator.
    public static func makeKey() -> LinkHasher {
        LinkHasher(key: SymmetricKey(size: .bits256).withUnsafeBytes { Data($0) })
    }

    public var encoded: String { Self.prefix + key.base64EncodedString() }

    /// The first 16 hex characters of HMAC-SHA256(key, "bcoa.link-key-id").
    public var keyID: String { String(hmac("bcoa.link-key-id").prefix(16)) }

    /// The link of a PatientID: nil when it is empty or a placeholder, which
    /// count as missing (ADR 0024 decision 1).
    public func pidLink(_ patientID: String, placeholders: [String]) -> String? {
        let normalized = Self.normalize(patientID)
        guard !normalized.isEmpty else { return nil }
        let folded = Set(placeholders.map { Self.casefold(Self.normalize($0)) })
        guard !folded.contains(Self.casefold(normalized)) else { return nil }
        return "pid:" + hmac(["pid", normalized].joined(separator: Self.separator))
    }

    /// The link of a folder candidate: the source and its first `level`
    /// components below the source folder, each NFC.
    public func folderLink(sourceID: Int, components: [String], level: Int) -> String {
        let parts = ["folder", String(sourceID)]
            + components.prefix(level).map { $0.precomposedStringWithCanonicalMapping }
        return "folder:" + hmac(parts.joined(separator: Self.separator))
    }

    /// Spaces and NUL stripped from both ends, then NFC; case is kept.
    public static func normalize(_ value: String) -> String {
        let padding: Set<Character> = [" ", "\u{0}"]
        var text = Substring(value)
        while let first = text.first, padding.contains(first) { text = text.dropFirst() }
        while let last = text.last, padding.contains(last) { text = text.dropLast() }
        return String(text).precomposedStringWithCanonicalMapping
    }

    private static let separator = "\u{1F}"

    // Python's casefold for the placeholder comparison. The placeholders are
    // ASCII, where folding case and lowercasing agree.
    private static func casefold(_ text: String) -> String {
        text.folding(options: [.caseInsensitive], locale: nil)
    }

    private func hmac(_ message: String) -> String {
        let code = HMAC<SHA256>.authenticationCode(
            for: Data(message.utf8), using: SymmetricKey(data: key))
        return code.map { String(format: "%02x", $0) }.joined()
    }
}
