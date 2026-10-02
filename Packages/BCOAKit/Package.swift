// swift-tools-version: 6.0
// BCOAKit holds everything of the app that can be tested without a window:
// the worker protocol, the worker process, the job queue, viewer geometry and
// windowing. Foundation only, so `swift test` runs on any Mac and in CI.
import PackageDescription

let package = Package(
    name: "BCOAKit",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "BCOAKit", targets: ["BCOAKit"]),
    ],
    targets: [
        // Tools version 6.0 compiles in the Swift 6 language mode, which is
        // complete strict concurrency checking; no extra flag is needed.
        .target(name: "BCOAKit"),
        .testTarget(
            name: "BCOAKitTests",
            dependencies: ["BCOAKit"]
        ),
    ]
)
