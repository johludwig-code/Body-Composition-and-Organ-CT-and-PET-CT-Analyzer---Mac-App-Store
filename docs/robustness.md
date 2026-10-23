# Robustness

What the app does when things go wrong, how that is tested, and what the
tests found. Every row is a test that runs again; none is a one-off check.

## How to run

| suite | command | where |
|---|---|---|
| Worker payload fuzz, full disk, unwritable folder, orphaned worker | `make test-worker` (`Worker/tests/test_robustness.py`) | CI on Linux; the real disk image only on macOS |
| Damaged viewer cache, mutated report content, refusals | `make test-swift` (`CaseReportTests`) | CI on macOS |
| Crash and resume in the real, sandboxed app | `python3 Scripts/crash_test.py --project <public>.bcoaproj` | the owner's Mac, Debug build, public data only |
| The reports of a real project read back | `BCOA_REPORT_PROJECT=<project> swift test -c release --filter reportsOfARealProject` (in `Packages/BCOAKit`) | the owner's Mac, public data only |

## What is tested

| fault | what must happen | test |
|---|---|---|
| A job payload with missing keys, wrong types, paths out of the project, `..`, `/etc/passwd`, NUL bytes, 5 000 characters, formulas (68 deterministic payloads × 5 job kinds) | the job ends with `done`; a failure carries an error code; nothing is written outside the project; no event carries the project's absolute path | `test_every_job_ends_cleanly_whatever_its_payload` |
| A symlink inside the project pointing out of it | refused | `test_a_symlink_out_of_the_project_is_refused` |
| The disk fills during an export (XLSX, CSV, case reports, package) | `disk_full`; the previous export untouched; no half file, not even hidden | `test_a_full_disk_leaves_the_exports_as_they_were`, and on macOS `test_a_really_full_disk_image` with a 3 MB disk image filled to the brim |
| The exports folder cannot be written | `export_not_writable`, without the path | `test_an_export_folder_that_cannot_be_written_says_so` |
| Two exports of one run | both complete, neither overwrites the other's reports | `test_two_exports_of_one_run_do_not_disturb_each_other` |
| The app crashes or is force-quit during a segmentation | the worker leaves within a second and removes its scratch (App Store 2.4.5(iii)) | `test_a_worker_whose_app_is_gone_leaves_at_once`, `test_a_real_orphan_ends_within_seconds`, `Scripts/crash_test.py` |
| A worker is killed during a segmentation | that stage fails with `worker_exit_9`, the batch goes on, Retry Failed redoes it | `Scripts/crash_test.py` |
| The app is killed with stages running and queued | at the next launch they are interrupted and queued again; the run completes with every label of every model | `Scripts/crash_test.py` |
| A viewer cache whose `meta.json` has a size of 0, a negative or zero spacing, an absurd size, or is not JSON; a cache file cut short | refused, never a crash | `aDamagedCacheIsRefused`, `aCacheWhoseFilesAreCutShortIsRefused` |
| 120 mutated case report content files | each read or refused, each read one drawn or refused; no crash, no half PDF | `mutatedContentFilesAreDrawnOrRefusedNeverCrash` |
| A report whose mask disagrees with the export, a label without a name, swapped sides | not drawn, or the model hidden, with the reason | `CaseReportTests` |

## What the tests found, and what changed

| found | consequence | fix |
|---|---|---|
| A series key of `.` made the viewer cache job put its temporary folder in the project root and, on success, replace all of `viewer-cache/` with one series | every viewer cache of the project lost | series keys must have the index's shape wherever they become folder names; the cache folder must be the series' own below `viewer-cache/`; a failed job removes its partial folder |
| A full disk left half files in `exports/` and reported "internal error", because XlsxWriter wraps the OSError | a half workbook beside the last good one; an error nobody could act on | exports are staged in a hidden folder of the job and moved into place only when complete; `disk_full`, `export_not_writable`, `export_write_failed` |
| After a force quit the segmentation's worker ran on as an orphan of launchd | a process after the app ended, against App Store 2.4.5(iii), holding the GPU | the worker watches its parent and leaves when the app is gone, or when the protocol pipe breaks |
| A `meta.json` with a size of 0 reached the preconditions of `VolumeGeometry` | the app would end | refused when read |
| The Debug app's worker refused `case_reports` while the command line accepted it | the bundle carried the worker of an older `make worker` | the build phase embeds the checkout's worker every time; export failures are logged by code |
| `1.0` in a content file read as a flag, not a number | "Slice thickness: not recorded" | numbers are told from booleans by their CF type |
| 54.25 printed as 54.2 | a report that disagrees with a spreadsheet's rounding | half away from zero |
| The export spells MOOSE words in lower case, the catalog as MOOSE writes them | the first real report refused to draw | both compared in the export's spelling |

## The crash test on the owner's Mac

M4 Pro, macOS 27.0.1, 23 October 2026, the Debug app (sandboxed, ad hoc),
the public ACRIN case on the Ludwig T7 with its three CT series selected.

| run | models | what happened | result |
|---|---|---|---|
| before the fixes | `clin_ct_organs` | the first segmentation's worker killed after 20 s: its stage failed with `worker_exit_9`, the batch went on; the app killed during the second segmentation: **its worker ran on as an orphan of launchd** until the test killed it 60 s later; relaunched, the running and the queued segmentation came back as interrupted and ran again, Retry Failed redid the killed one | the run complete, 3 × 19 structures; **two scratch folders of killed workers left behind** |
| after the fixes | `clin_ct_body` | the same two kills | no worker outlived the app (gone within 0.3 s); the run complete, 3 × 4 structures, 86 s after the first launch; no job left running, no partial folder, no scratch: `PASS` |

## From the DMG

`build/BCOAnalyzer.dmg` of 23 October 2026 (1.89 GB, ad hoc, SHA-256
`3a7528e72714cbd42e38eb85e894d7278e4ebfd8b785ede0e0635041d89cb061`), mounted
read-only: `codesign --verify --deep --strict` passes; the app started from
the image, rescanned the test project's interrupted source with its
sandboxed worker, and left no process after Quit. The bundled Python cannot
be run from a shell (exit 133): it carries the sandbox's inherit
entitlement, which macOS honours only for a child of the sandboxed app. That
is intended; self-tests outside the app use the unsigned `build/runtime`.
