# ADR 0004: Embedded CPython 3.12 from python-build-standalone

- Status: proposed — spike S3 confirms or replaces it
- Date: 2026-10-02
- Plan section: §3, §4

## Context

The plan leaves the choice between python-build-standalone and the BeeWare
support package to spike S3. PyInstaller and conda are rejected because
`multiprocessing` (spawn) and Dask must work unchanged — MOOSE uses a spawn
`ProcessPoolExecutor` in `moosez.py` and `image_conversion.py`.

## Decision (proposed)

python-build-standalone, `install_only_stripped`, pinned:

| field | value |
|---|---|
| release | 20250902 |
| file | `cpython-3.12.11+20250902-aarch64-apple-darwin-install_only_stripped.tar.gz` |
| SHA-256 | `17aa38f6a06eefbaa7757d7f8aa9d7941f169aa9571127b8d346141d2aa532d1` (computed on download, 2026-10-02) |
| size | 15.6 MB packed, 48 MB unpacked |

Checked on that download: `lib/python3.12/urllib/parse.py` line 62 contains
`'itms-services'` in `uses_netloc` — exactly the string Apple's automated
review rejects. `build_runtime.sh` removes it the way CPython's
`--with-app-store-compliance` does, and `verify_bundle.sh` fails the build if
it appears anywhere in the bundle. Removed as unneeded: `tkinter`, `_tkinter`,
`idlelib`, `turtledemo`, `test`, `ensurepip`, `lib2to3`, the Tcl/Tk dylibs, `pip`.

Placement (to confirm in S3): `Contents/Resources/python/`. Apple's
"Placing content in a bundle" wants executables and dylibs under
`Contents/MacOS`, `Contents/Frameworks` or `Contents/Helpers`; if the Store
validator objects to Mach-O files under `Resources`, the runtime moves to
`Contents/Frameworks/Python.framework`-style layout (what BeeWare does).

## Why not BeeWare first

BeeWare's macOS support package is built for exactly this placement problem,
but ships as a framework without the conventional `bin/python3` executable the
worker launch needs. python-build-standalone is what `uv` itself installs, so
the build machine and the bundle use the same interpreter build.
