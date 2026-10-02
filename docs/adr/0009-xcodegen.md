# ADR 0009: XcodeGen

- Status: accepted
- Date: 2026-10-02

`project.yml` is the project; the `.xcodeproj` is generated and ignored by git.
Diffable and editable without Xcode, which is how this project was started (in
a Linux container). XcodeGen is a build tool only and does not ship.
