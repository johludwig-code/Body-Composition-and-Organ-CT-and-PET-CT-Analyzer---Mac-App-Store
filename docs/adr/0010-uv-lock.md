# ADR 0010: Python packages from a hashed uv lock

- Status: accepted
- Date: 2026-10-02
- Plan section: §4 step 3

`Worker/requirements.in` names the direct dependencies, `requirements.lock` is
resolved for `aarch64-apple-darwin`, Python 3.12, with hashes. The build
installs with `uv pip install --require-hashes --no-deps` into the bundled
interpreter, so nothing outside the lock can enter.

First resolution, 2026-10-02: 104 packages, among them moosez 3.2.2,
nnunetv2 2.8.1, torch 2.14.1, torchvision 0.29.1, numpy 2.5.3, SimpleITK
2.5.6, dask 2026.8.0, dcm2niix 1.0.20260724, XlsxWriter 3.2.9, pydicom 3.0.2.

The resolution needs `MACOSX_DEPLOYMENT_TARGET=14.0`. Without it uv assumes
macOS 13 and quietly resolves torch 2.11.0, because torch 2.12 and later ship
only `macosx_14_0_arm64` wheels. torch 2.14.1 is also what BOCARTA-MOOSE
measured moosez with on MPS, so S1 starts from a known-good combination.

Packages that contain network client code (requests, httpx, huggingface-hub,
urllib3) are present because MOOSE and nnU-Net import them. They are inert:
the app has no network entitlement, so the sandbox refuses any connection, and
the adapter turns an attempted model download into a clear error (ADR 0011).
