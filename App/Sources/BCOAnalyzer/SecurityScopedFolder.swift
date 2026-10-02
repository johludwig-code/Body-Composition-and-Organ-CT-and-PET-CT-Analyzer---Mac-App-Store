import Foundation

/// A folder the user granted through an open panel, kept across launches as
/// an app-scoped security-scoped bookmark (entitlement
/// `files.bookmarks.app-scope`).
///
/// Access is started once and held while the app runs: a worker launched
/// while access is active inherits it (spike S2 verifies this), and stopping
/// access in the middle of a batch would make the next worker fail.
final class SecurityScopedFolder: Identifiable, @unchecked Sendable {
    let url: URL
    let bookmark: Data
    let readOnly: Bool
    private let accessing: Bool

    var id: URL { url }

    /// From a URL the open panel just returned.
    init(granted url: URL, readOnly: Bool) throws {
        var options: URL.BookmarkCreationOptions = [.withSecurityScope]
        if readOnly { options.insert(.securityScopeAllowOnlyReadAccess) }
        self.bookmark = try url.bookmarkData(options: options, includingResourceValuesForKeys: nil, relativeTo: nil)
        self.url = url
        self.readOnly = readOnly
        self.accessing = url.startAccessingSecurityScopedResource()
    }

    /// From a stored bookmark. A stale bookmark still resolves; it is used as
    /// is and refreshed the next time the user picks the folder.
    init(bookmark: Data) throws {
        var stale = false
        let url = try URL(
            resolvingBookmarkData: bookmark, options: [.withSecurityScope],
            relativeTo: nil, bookmarkDataIsStale: &stale)
        self.url = url
        self.bookmark = bookmark
        self.readOnly = false
        self.accessing = url.startAccessingSecurityScopedResource()
    }

    deinit {
        if accessing { url.stopAccessingSecurityScopedResource() }
    }
}
