# ADR 0014: What the licence audit of the macOS packages found, and what is removed

- Status: accepted
- Date: 2026-10-02
- Plan section: §2 rule 8, §4, §14, spike S3

## Context

`license_report.py` had only ever run on Linux. On 2 October 2026 the lock was
installed for `aarch64-apple-darwin` into a plain folder
(`uv pip install --target … --python-platform aarch64-apple-darwin`), which
gives the exact files the bundle will contain, and audited with the new
`--site` option. Every native library inside the wheels (`*/.dylibs`,
`blosc2/lib`) and every licence text that mentions the GPL was read as well.

## Findings

| package | finding | verdict |
|---|---|---|
| blosc2 4.14.1 | ships `lib/libtcc.dylib`, a fork of TinyCC (LGPL-2.1): a C compiler that blosc2's expression engine `dlopen`s to JIT-compile array expressions; it falls back to an interpreter when the library cannot be loaded | **remove** — LGPL in a signed, unmodifiable bundle, and a run-time compiler needs `allow-jit`, which the app does not and should not have (App Review 2.5.2) |
| connected-components-3d 4.1.0 | LGPL-3.0-or-later; pulled in by acvl-utils. Inference never calls it, but it is imported: nnU-Net finds the trainer class by importing its trainer modules, which import batchgeneratorsv2's training transforms, which import `acvl_utils.morphology`, which runs `import cc3d` at module level. The first macOS bundle run failed exactly there with `ModuleNotFoundError` | **replace by a stub** (`Scripts/stubs/cc3d`) that imports and fails loudly on any use |
| python-gdcm 3.2.6 | carries its own OpenSSL (`libssl.3`, `libcrypto.3`); pulled in by dicom2nifti; pydicom imports it inside `try` | **remove** — the app reads DICOM with dcm2niix, and one OpenSSL fewer matters for export compliance (#12) |
| setuptools 84 | build tool, vendors LGPL-3.0 code (autocommand); torch lists it for compiling C++ extensions only | **remove** |
| console scripts in `python/bin` | about 90 wrappers (`moosez`, `nnUNetv2_*`, `gdcm*`, `hf`, …) whose shebang names the build machine's interpreter, so none can run from the bundle; several are download tools (`nnUNetv2_download_pretrained_model_by_url`, `imageio_download_bin`, `hf`, `tiny-agents`). The worker starts `python3` itself and calls none of them; dcm2niix's binary is in `site-packages/dcm2niix` | **remove** everything in `bin` but the interpreter |
| torch 2.14.1, `torch/bin` and `torch/include` | three programs: `protoc` and `protoc-3.21.12.0`, a protobuf compiler, and `torch_shm_manager`, which `import torch` checks for (it raises if the file is missing) although only torch's own multiprocessing starts it; plus 64 MB of C++ headers for building extensions. Every program in a Store bundle must be sandboxed (ITMS-90296) and is one more thing App Review may ask about | **remove** `protoc*` and the headers; keep `torch_shm_manager`, signed like the other helpers |
| moosez 3.2.2 and the model archives | code Apache-2.0 (`LICENSE` in the PyPI source archive); weights CC BY 4.0 (the same archive's README, and `MODEL_LICENSE` in the MOOSE repository); the README also advertises a commercial version through Zenta. Every archive comes from MOOSE's GitHub releases; `clin_ct_body` alone holds 1 030 NIfTI validation predictions, labelmaps of the authors' validation patients | allowed; attribution and the changes to the checkpoints are in the notices (OPEN_QUESTIONS #7, #14). `fetch_models.py` prunes the predictions, and `verify_bundle.py` now fails on any image among the models |
| torch 2.14.1 | `Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND … BSL-1.0 AND MIT` was reported as unknown | permissive; the report now understands `WITH` exceptions and BSL-1.0 |
| batchgenerators, dynamic_network_architectures | the whole Apache-2.0 text pasted into `License`, reported as unknown | permissive; the report reads the first lines |
| dcm2niix | metadata names no licence; `license.txt` is BSD-2-Clause (Chris Rorden) with public-domain, MIT and BSD parts | approved in `license_approvals.json` |
| SciPy, NumPy | bundle libgfortran/libquadmath/libgcc_s: GPL-3.0 **with the GCC Runtime Library Exception** | allowed; named in the notices |
| Pillow, matplotlib | bundle FreeType, dual FTL/GPL | used under the FTL, whose credit line is in the notices |
| Pillow | its licence file mentions LGPL/GPL texts for XZ Utils tools; only public-domain liblzma is bundled | allowed |

Nothing else in the 104 packages is GPL, AGPL or LGPL. The string
`itms-services` occurs only in CPython's `urllib/parse.py`, which
`build_runtime.sh` already patches.

## Decision

`build_runtime.sh` removes the four parts, the console scripts, torch's
`protoc` and its headers after installing the lock, puts
the cc3d stub in place of the real package, and fails if gdcm or setuptools
is still importable or if `cc3d` is anything but the stub.
`verify_bundle.py` fails if libtcc, the real cc3d (its compiled module or its
dist-info), gdcm or setuptools reappears in a built app, or if `python/bin`
holds anything but the interpreter, so a lock update cannot bring them back
silently. It also fails on any program (a Mach-O of type `MH_EXECUTE`) in
`Resources` other than the interpreter, dcm2niix and `torch_shm_manager`, and
on any of them without exactly the sandbox and inherit entitlements. The lock itself is unchanged: the packages that need them still
declare them, and the hashes still prove what was downloaded.

## Evidence that nothing breaks

- Import level was not enough, and that is recorded here on purpose:
  importing `moosez` and nnU-Net's predictor loads neither `cc3d` nor
  `setuptools`, yet inference imports `cc3d` through the trainer lookup. Only
  a run with inference finds that, which is why the selftest runs one.
- Inference level: see `docs/benchmarks.md`, "Trimmed runtime against the
  full one", for the comparison on a real public CT.
- The macOS bundle workflow (`.github/workflows/bundle.yml`) builds the real
  runtime with the trim, signs it, and runs the worker inside the sandbox.

## Consequences

The app's licence list shrinks to permissive licences plus MPL-2.0 (certifi)
and the GCC runtime exception. If a future MOOSE or nnU-Net calls the removed
code, the worker fails with an ImportError naming the module, or with the
stub's RuntimeError naming this ADR, and the selftest's inference run is
where that shows first.
