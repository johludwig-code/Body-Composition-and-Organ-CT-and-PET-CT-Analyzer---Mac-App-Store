import Foundation
import Testing
@testable import BCOAStore

/// The repository root, found from this file, so that the vectors are the
/// ones the worker's suite reads and not a copy.
let repositoryRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()

private struct Vectors: Decodable {
    struct Key: Decodable {
        struct Pid: Decodable {
            let patient_id: String
            let normalized: String
            let state: String
            let link: String?
        }

        struct Folder: Decodable {
            let source_id: Int
            let components: [String]
            let level: Int
            let link: String
        }

        let link_key: String
        let link_key_id: String
        let pid: [Pid]
        let folder: [Folder]
    }

    let placeholder_ids: [String]
    let keys: [Key]
}

private func vectors() throws -> Vectors {
    let url = repositoryRoot.appendingPathComponent("Protocol/fixtures/link_vectors.json")
    return try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: url))
}

@Test func keyIDsMatchTheVectors() throws {
    for key in try vectors().keys {
        let hasher = try #require(LinkHasher(encoded: key.link_key))
        #expect(hasher.keyID == key.link_key_id)
        #expect(hasher.encoded == key.link_key)
    }
}

@Test func pidLinksMatchTheVectors() throws {
    let file = try vectors()
    for key in file.keys {
        let hasher = try #require(LinkHasher(encoded: key.link_key))
        for vector in key.pid {
            #expect(LinkHasher.normalize(vector.patient_id) == vector.normalized)
            let link = hasher.pidLink(vector.patient_id, placeholders: file.placeholder_ids)
            #expect(link == vector.link, "\(vector.patient_id.debugDescription)")
            #expect((link != nil) == (vector.state == "present"))
        }
    }
}

@Test func folderLinksMatchTheVectors() throws {
    for key in try vectors().keys {
        let hasher = try #require(LinkHasher(encoded: key.link_key))
        for vector in key.folder {
            let link = hasher.folderLink(
                sourceID: vector.source_id, components: vector.components, level: vector.level)
            #expect(link == vector.link)
        }
    }
}

@Test func malformedKeysAreRefused() {
    #expect(LinkHasher(encoded: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=") == nil)
    #expect(LinkHasher(encoded: "base64:AAEC") == nil)
    #expect(LinkHasher(encoded: "base64:not base64!") == nil)
}

@Test func freshKeysDifferAndRoundTrip() throws {
    let one = LinkHasher.makeKey()
    let two = LinkHasher.makeKey()
    #expect(one.key != two.key)
    #expect(try #require(LinkHasher(encoded: one.encoded)).keyID == one.keyID)
}
