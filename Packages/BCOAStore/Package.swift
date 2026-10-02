// swift-tools-version: 6.0
// The project database (plan §6). Its own package so that the GRDB
// dependency stays out of BCOAKit, whose tests run without it.
import PackageDescription

let package = Package(
    name: "BCOAStore",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "BCOAStore", targets: ["BCOAStore"]),
    ],
    dependencies: [
        // MIT licence; ADR 0006. The exact version is pinned by the committed
        // Package.resolved once the first build on a Mac has resolved it.
        .package(url: "https://github.com/groue/GRDB.swift.git", from: "7.0.0"),
    ],
    targets: [
        .target(name: "BCOAStore", dependencies: [.product(name: "GRDB", package: "GRDB.swift")]),
        .testTarget(name: "BCOAStoreTests", dependencies: ["BCOAStore"]),
    ]
)
