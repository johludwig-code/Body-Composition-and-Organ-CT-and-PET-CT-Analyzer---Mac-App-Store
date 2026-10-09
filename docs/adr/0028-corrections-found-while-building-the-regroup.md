# ADR 0028: Two corrections found while building the regroup

- Status: accepted; corrects [ADR 0022](0022-reading-grouping-and-splitting.md) decision 9
- Date: 2026-10-09
- Plan section: §7

## Context

The regroup of the index job (`Worker/bcoa_worker/index/group.py`) was built
against ADRs 0020, 0022, 0023 and 0024 and held to the synthetic corpus of
ADR 0026, which it matches with no difference. In two places the text of
ADR 0022 decision 9, followed as written, gives a wrong result, and one
point it leaves open had to be decided. Each was found by a test and is
corrected here rather than in the code alone.

## Decision

1. **A repeat tag splits only with one value per repeat.** Step e of the
   cascade splits a group whose positions repeat by AcquisitionNumber,
   TemporalPositionIdentifier or EchoNumbers "when that tag separates the
   repeats". Read as "no position repeats within one value of the tag", that
   rule shatters a sequential scan that numbers every slice as an
   acquisition of its own and has one slice written twice: every value then
   holds one slice, nothing repeats within a value, and the series splits
   into one part per slice. The tag now also has to have exactly as many
   distinct values as instances share the most crowded position, so that a
   two-phase series with two values splits and the sequential scan stays
   whole. Step e also applies only when every instance of the group has a
   position along a common normal; without one, repeats cannot be told.
2. **Parts are numbered per series UID across studies.** Decision 9 forms
   parts per series UID within the study UID and numbers them 0…n−1. The
   same SeriesInstanceUID can arrive under two study UIDs (the files of one
   series exported twice with a new study UID, without a SOP UID in common,
   so that no duplicate rule applies). Numbered per study, both studies get
   a part 0 of that series, and `UNIQUE (series_uid, part)`, in the catalog
   and in the project, refuses the generation. Parts keep being formed per
   (study UID, series UID); only the numbering runs over all parts of the
   series UID, by study UID first, then by lowest position, lowest
   InstanceNumber and lowest instance key.
3. **A part with a file of mixed frames has no geometry.** Decision 9 gives
   such a file `check.enhanced_mixed_frames` and keeps it whole, but leaves
   the geometry of its part open (corpus rule C12). Its frames have no
   common normal, so positions along a normal mean nothing; the part gets
   `check.no_geometry` beside the mixed-frames codes, is excluded from
   automatic selection by both, and has no slice count, extent or spacing.

## Consequences

- The plan's §7 says that a repeated acquisition splits only with one value
  per repeat and that parts are numbered per series UID across studies.
- `Worker/tests/test_index_group.py` holds all three: a sequential scan
  with per-slice AcquisitionNumbers and one repeated slice stays one part,
  two phases with two values split, a series UID in two studies numbers its
  parts 0 and 1, and a mixed-frames part has no geometry.
- Points the ADRs leave open, decided in the code and documented there:
  a duplicate loser is a file that won no instance and is counted on the
  part of the winner of its first key (and, since [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md), a loser
  from another series on its own series' part as well); `check.issuer_conflict` counts the
  issuers among the files that carry the study's chosen patient link; a
  NIfTI volume's `image_count` is the product of its dimensions from the
  third on; folder candidates come from the source that holds most of the
  study's files (the lowest source on a tie), with the source root's folder
  name as the level-0 label; `check.values_vary` applies to any modality
  and `check.no_rescale` to CT only; rows of removed sources and files that
  vanished are deleted by the scan's own transactions, which set
  `regroup_due`, not by the regroup.

## Rejected alternatives

- **No guard on step e, only "repeats separated".** It is the shattering
  described above; the corpus does not contain that case, a real archive
  does.
- **Numbering per study and widening the unique key to include the study
  UID.** The project's `series` table, the merge and the result paths are
  keyed by (series UID, part); changing all of them to keep a numbering
  that nothing needs is a larger change than numbering across studies.
- **Taking the geometry of the majority of frames.** It would give a part
  a slice count that the converter cannot reproduce, since the file is
  converted whole.
