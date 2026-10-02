# ADR 0010: Python packages from a hashed uv lock

- Status: accepted
- Date: 2026-10-02
- Plan section: §4 step 3

`Worker/requirements.in` names the direct dependencies, `requirements.lock` is
resolved for `aarch64-apple-darwin`, Python 3.12, with hashes. The build
installs with `uv pip install --require-hashes --no-deps` into the bundled
interpreter, so nothing outside the lock can enter.

First resolution, 2026-10-02: 104 packages, among them moosez 3.2.2,
nnunetv2 2.8.1, torch 2.11.0, numpy 2.5.3, SimpleITK 2.5.6, dask 2026.8.0,
dcm2niix 1.0.20260724, XlsxWriter 3.2.9, pydicom 3.0.2.

Note on torch: BOCARTA-MOOSE measured moosez with torch 2.14.1 on MPS. The
resolver picks 2.11.0 here; spike S1 runs on the locked version, and if MPS
fails there the lock is regenerated with an explicit `torch==` pin.

Packages that contain network client code (requests, httpx, huggingface-hub,
urllib3) are present because MOOSE and nnU-Net import them. They are inert:
the app has no network entitlement, so the sandbox refuses any connection, and
the adapter turns an attempted model download into a clear error (ADR 0011).
