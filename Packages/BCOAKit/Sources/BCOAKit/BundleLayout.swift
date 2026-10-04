import Foundation

/// Where the bundled backend lives inside `Contents/Resources`, and the
/// environment a worker is started with.
///
/// The placement under `Resources` is what spike S3 has to confirm; if the
/// Store validator wants Mach-O files under `Frameworks`, only this type and
/// `build_runtime.sh` change.
public struct BundleLayout: Sendable {
    public let resources: URL

    public init(resources: URL) {
        self.resources = resources
    }

    public static var main: BundleLayout {
        BundleLayout(resources: Bundle.main.resourceURL ?? Bundle.main.bundleURL)
    }

    public var pythonExecutable: URL { resources.appendingPathComponent("python/bin/python3") }
    public var modelsDirectory: URL { resources.appendingPathComponent("models") }
    public var modelManifest: URL { modelsDirectory.appendingPathComponent("manifest.json") }
    public var thirdPartyNotices: URL {
        resources.appendingPathComponent("licenses/THIRD_PARTY_NOTICES.md")
    }

    /// The command line of a worker. `-I` isolates it from the user's Python
    /// setup (no PYTHONPATH, no user site, no current directory on the path);
    /// the worker package is installed in the bundled site-packages for that
    /// reason.
    public func workerArguments(jobFile: URL) -> [String] {
        ["-I", "-m", "bcoa_worker", "run", "--job", jobFile.path]
    }

    /// Must agree with `bcoa_worker.environment.runtime_environment`; the
    /// worker sets the same values again before importing MOOSE.
    public func workerEnvironment(projectDir: URL, jobID: String) -> [String: String] {
        let scratch = projectDir.appendingPathComponent("work/.scratch/\(jobID)")
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
            "JOBLIB_MULTIPROCESSING": "0",
            "nnUNet_n_proc_DA": "12",
            "LANG": "en_US.UTF-8",
        ]
        // The sandbox marker lets spike S2 report whether the worker really
        // runs inside the inherited sandbox.
        if let container = ProcessInfo.processInfo.environment["APP_SANDBOX_CONTAINER_ID"] {
            environment["APP_SANDBOX_CONTAINER_ID"] = container
        }
        return environment
    }
}
