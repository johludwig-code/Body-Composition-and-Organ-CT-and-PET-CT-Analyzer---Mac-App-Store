# ADR 0023: Auto-selection, reasons and selection state

- Status: accepted
- Date: 2026-10-08
- Plan section: §7, §12, §17

## Context

Plan §7 proposes one CT series per study, configurable, with its reason as a
tooltip: modality CT, an ImageType that contains ORIGINAL and AXIAL
(excluding LOCALIZER, DERIVED/SECONDARY, dose reports, RTSTRUCT, SEG and PR),
at least 50 slices, then the largest z coverage, the thinnest slices and a
soft-tissue kernel from an adjustable list. The chosen series is marked
primary for the export. The plan names three bulk actions, and checks that
warn without stopping anything. M2's acceptance (§17) asks for a reason
behind every selection.

What the v1 schema and the export assume:

- `series.selection_reason` is free text. BCOAKit, BCOAStore and the worker
  have no string resources, so text made there bypasses the String Catalog.
- The export takes the first series of a study with `is_primary`, then the
  lowest `series_key`, and assumes that series is selected. Nothing enforces
  at most one primary per study.

What BOCARTA-MOOSE (BTM) learned:

- Siemens writes the low-dose CT of a PET/CT with ImageType
  `DERIVED\CT_SOM5 SPI\PRIMARY\AXIAL`. BTM accepts it as a primary
  acquisition, still refuses SECONDARY, and holds that with a table test.
- Kernel codes must be compared exactly: prefix matching let the Philips
  code "b" match "BONE", so a bone kernel counted as soft. GE's soft list
  once held BODY and ABDOMEN, which are body parts.
- A scan that ticked its suggestions by itself was taken back; nothing
  unticks what the user ticked.

Judge 1 found the ImageType rule of both leading designs (ADR 0020) wrong
on BTM's own case, a fatal flaw in each. Design A accepted DERIVED only when
the second value is PRIMARY, and design C required the third value to be
AXIAL; in the Siemens CT the second value is `CT_SOM5 SPI` and the third
PRIMARY. Judge 1 also counted it fatal that design C's migration had no
constraint for the primary. Judge 2 required design A to give a reason to
every selection, not only to automatic ones, and to deselect a series that
vanished from a study chosen by hand.

## Decision

1. **The worker decides.** Eligibility, ranking and reasons are computed in
   the regroup (ADR 0020) and stored per part; the app applies the stored
   automatic choice and never ranks.
2. **Eligibility.** Every failing condition is recorded, in this order
   (ImageType values counted from 0):

   | code | excluded when |
   |---|---|
   | `select.excluded.not_image` | the SOP class is not an image storage class: dose report, SR, RTSTRUCT, SEG, PR, raw data |
   | `select.excluded.secondary_capture` | a Secondary Capture SOP class (1.2.840.10008.5.1.4.1.1.7 and its multi-frame variants) |
   | `select.excluded.not_ct` | modality ≠ CT |
   | `select.excluded.image_type_missing` | ImageType is empty |
   | `select.excluded.image_type_value` | a value is in `excluded_image_type_values` |
   | `select.excluded.derived` | value 0 is DERIVED, unless `accept_derived_primary` is set and PRIMARY is among values 1… |
   | `select.excluded.not_original` | value 0 is neither ORIGINAL nor DERIVED |
   | `select.excluded.not_axial_image_type` | AXIAL is not among values 2… (Enhanced CT `…1.2.1` and Legacy Converted CT `…1.2.2`: AXIAL or VOLUME) |
   | `select.excluded.no_geometry` | orientation or positions are missing or invalid, so `z_extent_mm` is NULL |
   | `select.excluded.not_axial` | \|n_z\| < 0.95 |
   | `select.excluded.mixed_frames` | `check.enhanced_mixed_frames` |
   | `select.excluded.description` | the description matches a term in `excluded_description_terms` |
   | `select.excluded.too_few_slices` | fewer distinct positions than `min_slices` |
   | `select.excluded.truncated` | any file has `pixel_data = 'truncated'` |
   | `select.excluded.missing_pixels` | any file has `pixel_data = 'missing'` |
   | `select.excluded.transfer_syntax` | the transfer syntax is not in the convertible set |
   | `select.excluded.nifti_not_3d` | the NIfTI file is not a single 3D volume |
   | `select.excluded.nifti_unnamed` | the NIfTI name is outside the `CT_<id>` convention |
   | `select.held.unconfirmed_patient` | eligible, but the patient's ID came from a folder and is not confirmed (applied by the merge) |

3. **The ImageType rule** passes BTM's table, and the Enhanced
   `ORIGINAL\PRIMARY\VOLUME` case:

   | ImageType | `accept_derived_primary` | result |
   |---|---|---|
   | `DERIVED\CT_SOM5 SPI\PRIMARY\AXIAL` | on | passes |
   | `DERIVED\CT_SOM5 SPI\PRIMARY\AXIAL` | off | fails |
   | `ORIGINAL\PRIMARY\AXIAL` | either | passes |
   | `DERIVED\SECONDARY\AXIAL` | either | fails |
   | `DERIVED\PRIMARY\REFORMATTED` | either | fails |
   | `ORIGINAL\PRIMARY\LOCALIZER` | either | fails |

   With `accept_derived_primary = false`, the default until OPEN_QUESTIONS
   #24 is answered, this is the plan's rule ("ORIGINAL and AXIAL, without
   LOCALIZER and DERIVED/SECONDARY"), widened in one place and narrowed in
   three. Each is a deliberate deviation from the plan's wording:
   - Widened: Enhanced CT and Legacy Converted CT pass with VOLUME in place
     of AXIAL, because these objects can write VOLUME there, and the plan's
     wording would refuse every one of them; the geometry rule still
     requires axial slices.
   - Narrowed by position: value 0 must be ORIGINAL, where the plan said
     "contains", and AXIAL must be among values 2…, because that is where
     the standard puts these terms (value 0 ORIGINAL or DERIVED, value 1
     PRIMARY or SECONDARY, the plane from value 2 on), so a term in another
     position cannot pass the rule.
   - Narrowed by the exclusion list: a value from
     `excluded_image_type_values` excludes the series, because MIP, MPR,
     VRT, REFORMATTED, SCREEN SAVE and the like are reconstructions and
     pictures, not the acquisition the pipeline needs, even when value 0
     says ORIGINAL.
   - Narrowed by SOP class: Secondary Capture is excluded whatever its
     ImageType says (decision 2), because it is a saved picture.

   So `ORIGINAL\PRIMARY\AXIAL\MIP`, which the plan's wording accepts, fails
   here with `select.excluded.image_type_value`, and an Enhanced CT with
   `ORIGINAL\PRIMARY\VOLUME`, which the plan's wording refuses, passes.
4. **Description terms** are matched after NFC and case-folding. A term
   longer than 3 characters matches as a substring; a term of 3 characters
   or fewer matches only a whole token, splitting on anything that is not
   alphanumeric. So "Thorax MPR 3.0" matches `mpr` and "Compressed" does
   not.
5. **The convertible set** is every transfer syntax that the pinned
   dcm2niix 1.0.20260724 reads. It refuses Deflated
   (1.2.840.10008.1.2.1.99) and JPEG Extended 12-bit
   (1.2.840.10008.1.2.4.51); unknown and private syntaxes count as
   unreadable. The worker's guard lets the index job start no program, so a
   Linux test runs the pinned wheel on each syntax and holds the set to it.
6. **Ranking.** Rank 1 is the part that survives this cascade over all
   eligible parts of a study; it is removed and the cascade runs again for
   rank 2, and so on. Only rank 1 is `auto_selected`.

   | step | rule |
   |---|---|
   | 1 | coverage: keep the parts with `z_extent_mm ≥ max − coverage_tolerance_mm` (1.0, for rounding only; OPEN_QUESTIONS #30) |
   | 2 | thinnest: keep the parts within `thickness_tie_mm` (0.02) of the smallest effective thickness; effective thickness is max(thickness, `thickness_floor_mm`) when a floor is set, and the spacing when the thickness is missing |
   | 3 | kernel class: soft > unknown > sharp |
   | 4 | ORIGINAL before DERIVED |
   | 5 | fewer warning-level checks |
   | 6 | lowest SeriesNumber (missing last) |
   | 7 | series UID, in string order |
   | 8 | `part` |

   With `kernel_before_thickness` (OPEN_QUESTIONS #28), steps 2 and 3 swap;
   the floor is OPEN_QUESTIONS #29, and a coverage tolerance beyond
   rounding is OPEN_QUESTIONS #30. The reason for rank 1 names the step that
   separated it from rank 2; every other eligible part's reason names the
   step at which it lost to rank 1.
7. **Kernel classes** take the lists of BTM's `KernelTable.swift` verbatim
   and in BTM's order (ADR 0012), and its matching with the differences
   listed below. The vendor comes from Manufacturer, upper-cased: SIEMENS is
   Siemens, GE as a word or GENERAL ELECTRIC is GE, PHILIPS is Philips,
   CANON or TOSHIBA is Canon. The kernel is the first value of
   ConvolutionKernel, lower-cased and trimmed. A code matches when the
   kernel equals it or, for codes of 3 or more characters, equals it plus
   one suffix letter from `fsdhqr`. As in BTM's code, the vendor's own sharp
   list is checked first, then its own soft list, then every vendor's sharp
   list, then every soft list; anything else is `unknown`. The vendor's
   table decides its own codes, because codes mean different things from
   vendor to vendor; for every other code the sharp lists come first,
   because a bone kernel taken for soft tissue corrupts the measurements
   without a sign.

   Where this differs from BTM:
   - Philips codes match exactly only (`"exact": true` in the settings).
     BTM's comment says so, but its code lets Philips codes of 3 or more
     characters (smooth, standard, sharp, detail, bone, lung) take a suffix
     letter. The suffix letters are the ones Siemens appends, and Philips
     appends none.
   - Without a ConvolutionKernel the class is `unknown`. BTM then guesses
     from words in the series description ("weichteil", "knochen", "lunge"
     and others). That fallback is left out: the plan names a list of
     kernels, a description names the body region as often as the filter
     (BTM removed BODY and ABDOMEN from GE's list for that reason), and
     `unknown` already ranks between soft and sharp.
   - BTM takes any manufacturer that starts with "ge" as GE; here GE must be
     a word of its own, because a prefix also matches names that only begin
     with those letters.

   | vendor | soft | sharp |
   |---|---|---|
   | Siemens | b08 b10 b19 b20 b26 b30 b31 b35 b40 b41 br32 br34 br36 br38 br40 br44 i26 i30 i31 i36 i40 i41 qr36 qr40 sa36 sa40 bf37 bf40 bv36 bv40 | b45 b46 b50 b60 b70 b75 b80 bl57 bl64 br49 br54 br59 br64 br69 i50 i70 bv49 bv59 hr40 hr49 hr59 hr64 hr68 qr49 qr59 qr69 u70 u90 s80 y80 |
   | GE | standard stnd std soft | bone boneplus bonesplus lung detail edge chest chst |
   | Philips (exact) | a b c d smooth standard | l ya yb yc yd e ea eb ec ub uc ud sharp detail bone lung |
   | Canon | fc01 fc02 fc03 fc07 fc08 fc11 fc12 fc13 fc14 fc17 fc18 fc21 fc22 fc26 | fc30 fc31 fc35 fc50 fc51 fc52 fc53 fc55 fc56 fc80 fc81 fc82 fc86 |

8. **Configuration.** `project_meta.selection_config` holds the settings as
   JSON, version 1, and every index payload carries them. Their sha256 goes
   into `catalog_meta`, and a change queues a `regroup`. The decoder is
   written by hand and lenient, so a missing key takes its default. The
   defaults: `min_slices` 50, `coverage_tolerance_mm` 1.0,
   `thickness_tie_mm` 0.02, `thickness_floor_mm` null,
   `kernel_before_thickness` false, `accept_derived_primary` false,
   `axial_min_abs_nz` 0.95, `bulk_thin_ct_max_mm` 3.0, the kernel lists
   above, `excluded_image_type_values` LOCALIZER, SCOUT, PROJECTION IMAGE,
   SCREEN SAVE, REFORMATTED, MPR, MIP, MINIP, VRT, CPR, CURVED, SECONDARY,
   and `excluded_description_terms` topogram, scout, localizer, surview,
   scanogram, dose report, patient protocol, screen save, mip, minip, mpr,
   vrt, 3d.

   The user edits them in a project sheet, "Selection Settings…", which is
   part of M2: the minimum number of slices, the tolerances and the
   thickness floor, the kernel lists, both exclusion lists,
   `kernel_before_thickness` and `accept_derived_primary`, and "Restore
   Defaults". Saving stores the JSON in `project_meta` and queues the
   regroup, so the owner's answers to #24 and #28 to #30 are a setting, not
   a change of code.
9. **Reasons are codes.** `series.selection_reason` holds
   `{"v": 1, "outcome": "chosen|eligible|excluded|held", "codes": […],
   "params": {…}}`. The app maps each code to an exhaustive Swift enum with
   an `.unknown(String)` fallback, and each case renders
   `String(localized: "<code>", defaultValue: "…")`, so the static keys are
   extracted into `Localizable.xcstrings`. Numbers are formatted in Swift and
   passed as strings, so every placeholder in the catalog is `%@`. The codes
   are registered in `Protocol/index_codes.json`; a test holds every code
   the worker emits to the registry, and the enums to it as well.

   *Completed by [ADR 0029](0029-corrections-found-in-the-review-of-the-index-job.md)
   on 2026-10-09: the merge writes the `held` reason, with the worker's
   own under `if_confirmed`, and the confirmation or assignment that
   releases the study puts that one back.*
10. **Selection state.** Each study has a `selection_mode` (`auto` or
    `user`), and each series a `selection_origin` (`auto`, `user`,
    `bulk_thin_ct` or `cohort`, with `selection_cohort_id`). The merge:
    - in auto studies clears the primaries, sets `selected = auto_selected`
      and the origin `auto`, then marks the automatic choice primary;
    - leaves user studies as the user left them, and reports new eligible
      series there with `check.new_series_since_manual` (count, and whether
      the automatic selection would now choose one of them);
    - returns a user study to auto when its primary disappears or moves
      to another study, with `check.primary_gone`; a series that moves
      leaves its primary flag behind;
    - deselects every gone series, and every series of a patient whose ID
      is unconfirmed: no PatientID, only folder candidates (ADR 0024).

    A rescan therefore never overrides a choice the user made.
11. **One selected primary per study, enforced.** Migration v2 adds a partial
    unique index (`series_one_primary_per_study`) and two triggers that
    refuse a primary that is not selected. Every statement clears the
    primary first, then sets `selected`, then sets the primary, because the
    unique index is checked row by row.

    Before it creates them, v2 repairs what a hand-made v1 file could hold,
    although no shipped version wrote it: an index that cannot be created
    would keep the project from opening at all. Per study it keeps the
    primary the export can see, a selected one or one with results, and
    among those the lowest `series_key`, which is the one the export already
    takes (exactly so in a project with one run, since the export counts
    results of the run it writes); only when no primary is visible does it
    keep the lowest
    `series_key` of all. It clears the other primaries and selects the one
    kept. Keeping the lowest key alone would hand the primary to a series
    the user never selected and select it.
12. **Edits.** `SelectionSQL` holds `makePrimary`, `select`, `deselect`,
    `bulkThinCT`, `applyAuto`, `repairPrimary`, `saveCohort` and
    `applyCohort`, with `inputs` for their temp tables, as Swift constants
    that pytest executes. Each edit is one transaction: the statement,
    `SelectionSQL.repairPrimary`, `IndexSQL.patientAges`, and an audit row
    `selection_changed` with the action and counts of
    studies and series only. The user may select an excluded series by hand,
    except `not_image` and `secondary_capture`; its exclusion is then shown
    before the run as a warning, "Selected by you, although: …", which is
    the plan's "warning, no abort".
13. **Bulk actions and cohorts.**
    - "Apply Auto-Selection to All" runs `SelectionSQL.applyAuto` over
      every current study, and asks first if any study is in user mode.
    - "Select All CT ≤ {max} mm…" (default 3) adds eligible CT series only
      (those with a rank), with a tolerance of 0.02 mm. It never removes a
      selection, never changes an existing primary, and puts the studies it
      touches in user mode.
    - "Save Selection as Cohort…" stores the selected series with their
      primary flags under a unique name. "Apply Cohort" replaces the whole
      selection, asks first, and reports members that have gone.
14. **Checks warn only.** No check stops anything. Only the eligibility
    table affects the automatic selection, and step 5 of the ranking counts
    warnings.

## Consequences

- Every series has a reason, including those chosen by hand, by a bulk
  action or from a cohort: the status line comes from `selection_origin`,
  `selected` and the patient's status, and the reason lines from the codes.
- A code without a text is a compile error in the app, and
  `BCOAnalyzerTests` holds that every case renders a non-empty string
  without an unfilled placeholder.
- The export's assumption about the primary is now a constraint: a second
  primary or an unselected primary aborts the statement that would make it.
- A change of the selection settings costs a regroup of the catalog, not a
  re-read of the files.
- Four owner questions decide defaults without blocking the work:
  `accept_derived_primary` (#24), `kernel_before_thickness` (#28),
  `thickness_floor_mm` (#29) and `coverage_tolerance_mm` (#30).
- The tests are `test_image_type.py`, `test_kernels.py`, `test_select.py`
  and `test_dcm2niix_syntaxes.py` in the worker, the merge and selection
  scenarios in `test_merge_sql.py`, and `SelectionSQLTests` and
  `CodeEnumTests` in Swift.

## Rejected alternatives

- **A score, as BTM's selector uses.** The plan prescribes an order, and a
  cascade's reason can name the one step that decided; a weighted sum can
  only list its terms.
- **The ImageType rules of designs A and C.** Both exclude the Siemens CT
  that `accept_derived_primary` exists for, and the plan says ImageType
  "contains" AXIAL, not that a given value is AXIAL.
- **Reasons as text.** Text made in the worker or in the Swift packages
  bypasses the String Catalog, cannot be localized later, and cannot be
  checked for completeness.
- **A reason for automatic choices only (design A).** A series ticked by
  "Select All CT ≤ 3 mm" showed "candidate: X ranks higher" while it was
  selected. The origin per series comes from design B.
- **Keeping a vanished series selected in a study chosen by hand (design
  A).** The pipeline would then try to convert files that are gone.
- **The primary rule left to the code (design C).** The export depends on
  it, and nothing else would hold it.
- **Kernel class before thickness, a thickness floor, or a coverage
  tolerance beyond rounding, by default.** Each changes the plan's order,
  so they are settings with the plan's behavior as the default until the
  owner answers. The design proposed 10 mm of coverage tolerance without a
  measurement or a case behind it, and a series 10 mm short can miss the
  lung apex or the liver dome.
- **`cor` and `sag` as description terms.** Coronal and sagittal stacks
  already fail the geometry rule (`not_axial`), so these terms could only
  ever exclude axial series, and in German "Cor" names the heart: an axial
  series called "Thorax_Cor 1.0" would lose its automatic selection.
