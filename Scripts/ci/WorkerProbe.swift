// CI only, never shipped: a sandboxed stand-in for the app that starts the
// bundled worker the way BundleLayout does, so that spike S2's central
// question (does python3 with app-sandbox + inherit run, load torch and MOOSE,
// and write where the app may write?) is answered on every push instead of
// once on a Mac. Placed in Contents/MacOS of a built app by bundle.yml and
// signed with the app's own entitlements, it runs with the app's sandbox
// rules; the worker inherits them.
//
//   bcoa-probe <job kind> [payload JSON]
//
// Prints the worker's protocol lines and exits with the worker's status.

import Foundation

let arguments = CommandLine.arguments
let kind = arguments.count > 1 ? arguments[1] : "selftest"
let payloadJSON = arguments.count > 2 ? arguments[2] : "{}"

let executable = URL(fileURLWithPath: arguments[0]).resolvingSymlinksInPath()
let resources = executable.deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("Resources")
let python = resources.appendingPathComponent("python/bin/python3")

// Inside the sandbox NSHomeDirectory() is the container; a project there is
// writable without any user grant, like the app's own default project.
let project = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Probe.bcoaproj")
let jobID = "j_probe_\(kind)"
try FileManager.default.createDirectory(
    at: project.appendingPathComponent("logs"), withIntermediateDirectories: true)

let payload = try JSONSerialization.jsonObject(with: Data(payloadJSON.utf8))
let job: [String: Any] = [
    "protocol_version": 1,
    "job_id": jobID,
    "kind": kind,
    "project_dir": project.path,
    "log_path": project.appendingPathComponent("logs/\(jobID).log").path,
    "resources_dir": resources.path,
    "heartbeat_seconds": 30,
    "payload": payload,
]
let jobFile = project.appendingPathComponent("\(jobID).json")
try JSONSerialization.data(withJSONObject: job).write(to: jobFile)

let scratch = project.appendingPathComponent("work/.scratch/\(jobID)")
var environment: [String: String] = [
    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
    "MPLCONFIGDIR": scratch.appendingPathComponent("mpl").path,
    "TMPDIR": scratch.appendingPathComponent("tmp").path,
    "TORCH_HOME": scratch.appendingPathComponent("torch").path,
    "XDG_CACHE_HOME": scratch.appendingPathComponent("cache").path,
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "LANG": "en_US.UTF-8",
]
let sandboxed = ProcessInfo.processInfo.environment["APP_SANDBOX_CONTAINER_ID"]
if let sandboxed { environment["APP_SANDBOX_CONTAINER_ID"] = sandboxed }
FileHandle.standardError.write(Data("[probe] sandbox container: \(sandboxed ?? "none")\n".utf8))

let process = Process()
process.executableURL = python
process.arguments = ["-I", "-m", "bcoa_worker", "run", "--job", jobFile.path]
process.environment = environment
try process.run()
process.waitUntilExit()

if let log = try? String(contentsOf: project.appendingPathComponent("logs/\(jobID).log"), encoding: .utf8) {
    FileHandle.standardError.write(Data("[probe] worker log tail:\n\(log.suffix(4000))\n".utf8))
}
exit(process.terminationStatus)
