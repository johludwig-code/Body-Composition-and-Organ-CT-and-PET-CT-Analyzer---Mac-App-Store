# ADR 0026: How the import (M2) is tested and accepted

- Status: accepted
- Date: 2026-10-08
- Plan section: §16, §17

## Context

Plan §16 had no row for the import, and §17 accepted M2 when "the test
corpus is grouped correctly; every selection has a reason; 100 000 files
with progress and cancel". None of the three says how it is checked. The
M2 design made them measurable and changed both sections; ADR 0017 is the
precedent for recording such a change in an ADR, and this one does it for
ADRs 0020 to 0025.

What the budgets can rest on, measured on Linux (Xeon at 2.1 GHz, the locked
pydicom 3.0.2, synthetic files; ADR 0020, ADR 0022):

- walking 100 000 directory entries: 0.3–0.5 s;
- reading their headers: 62 s warm, 80 s cold, 100–111 s with GE-like
  private headers;
- merging 20 000 series in 5 000 studies: 1.04 s the first time, 0.40–0.49 s
  again;
- holding 100 000 records in memory, as the rejected design A did: a peak of
  556–571 MB.

Nothing else of the index has run yet. The batched read, the regroup,
cancel and resume exist as design only, and stat on APFS has not been
measured.

## Decision

1. **A synthetic corpus.** `Worker/tests/dicom_factory.py` builds the corpus
   with pydicom only: deterministic `2.25.` UIDs from a seed, 16 × 16 pixels,
   and optional GE-like private groups for slow headers. Its scenarios cover
   the rules of ADRs 0022 to 0024: the selection cases (localizer, thin and
   thick series, the three kernel classes, `DERIVED\PRIMARY`), the geometry
   checks, duplicates across folders, every split reason, Enhanced CT,
   DICOMDIR, PET/CT pairs, every transfer syntax, anonymous folders with
   placeholder IDs, symbolic links, unreadable and truncated files,
   archives, NIfTI, and the canary values. `corpus_expected.json` lists the
   patients, studies, parts, selection, reason codes and checks of each
   object, and `test_corpus.py` holds the index to it exactly.
2. **The owner reviews the expectation.** The generator and the expected
   file come from the same hands, so a rule misread in one is misread in
   the other. `corpus_expected.json` therefore becomes M2's acceptance gate
   only after the owner has reviewed it, as `acrin_expected.json` does
   (decision 5); until then `test_corpus.py` is a regression test. So that
   it can be reviewed, the file names series by their description and
   scenario, not by UID.
3. **Budgets, and what each rests on.**

   | budget | basis |
   |---|---|
   | 10 000 files: full scan < 60 s | the header read measured above, about 8 s per 10 000 files cold; the rest is room for a shared CI runner |
   | 10 000 files: unchanged rescan < 2 s | the walk measured above; the stat per file is not measured |
   | 10 000 files: merge < 1 s | 20 000 series merge in 1.04 s, and the corpus has far fewer |
   | 100 000 files: full scan ≤ 150 s | 80 s cold, 100–111 s with GE-like headers, plus the walk and one regroup |
   | 100 000 files: unchanged rescan ≤ 10 s | the walk measured above; the stat is not measured on Linux, and on APFS only by the sandboxed probe |
   | 100 000 files: regroup alone ≤ 20 s | not measured; the regroup is not built |
   | 100 000 files: peak RSS ≤ 500 MB | not measured. The only figure, 556–571 MB, is design A's, which held every record in memory; the catalog writes in batches of at most 1 000 files and is expected to stay well below |
   | cancel answered in ≤ 2 s | not measured; it follows from batches of at most 1 000 files or 2 s, of which a cancel rolls back one |
   | resume reads only the rest | a check, not a time |

   A budget that the first measurement misses is changed here, with the
   number that was measured, never by loosening the test unrecorded.
4. **Where they hold.** The budgets are gates on Linux: 10 000 files on
   every push, 100 000 files in a slow job (`-m slow`). The macOS runner runs
   a sandboxed probe of 100 000 files whose corpus the bundled Python makes
   inside the app container; its numbers go into `docs/benchmarks.md` and
   gate nothing, because a shared runner's times vary and the owner's Mac
   is not a runner.
5. **The public case end to end.** The IDC case ACRIN-NSCLC-FDG-PET-002 (4
   studies, 14 series, 1 958 files) is downloaded in CI only, by
   `Scripts/ci/real_case.sh`, and indexed on Linux without the sandbox and
   on the macOS runner with the sandboxed worker and the Swift merge. Its
   expectations in `Scripts/ci/acrin_expected.json` hold counts and
   descriptions only, no UIDs, and become a criterion after the owner has
   reviewed them.
6. **Acceptance on the owner's Mac.** M2 is accepted only after a run on the
   owner's Mac with a real folder: add it as a source, index it, read the
   tree, the selection and the reasons, cancel once and resume. No data
   leaves the Mac. The owner reports what was seen in words and counts, and
   the screenshots of the pull request show the synthetic corpus or the
   public case, never the real folder.

## Consequences

- Plan §16 gains the rows "Import", "Import-Leistung" and "Import
  Ende-zu-Ende", and §17's acceptance of M2 names `corpus_expected.json`
  after the owner's review, the budgets of §16 and the run on the owner's
  Mac.
- Three budgets (the regroup, the peak RSS and the cancel) rest on no
  measurement yet, and both rescan budgets rest on the walk alone, without
  the stat. As each part of the scan is built, `docs/benchmarks.md` gets
  the measured number beside its budget.
- M2 cannot be accepted from CI alone: the owner's review of two
  expectation files and the run on the Mac are part of it.

## Rejected alternatives

- **The plan's wording as the criterion.** "Grouped correctly" names no
  reference to compare with, so nobody could say when it holds.
- **`corpus_expected.json` as the gate without a review.** The implementers
  would check their own reading of the rules against itself.
- **Budgets measured on the owner's Mac only.** They would be checked once,
  by hand, and never again on a change.
- **Real folders as test data.** Plan §16 keeps patient data out of the
  repository and CI; the public case and the synthetic corpus stand in.
