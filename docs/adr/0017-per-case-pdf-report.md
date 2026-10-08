# ADR 0017: A per-case PDF report, drawn from the export's numbers by code signed in the bundle

- Status: accepted; supersedes point 5 of [ADR 0003](0003-app-store-ground-rules.md)
- Date: 2026-10-04
- Plan section: §1, §3, §9, §11, §12, §13, §14, §16, §17, §18

## Context

On 3 October 2026 the owner asked for an optional PDF report per case,
"analogous to BOA": the key numbers and images of one case at a glance, with
the structures named the way a radiologist writes them. Later that day
(10:33 UTC) the owner ruled that the PDF is always in English, whatever the
app's language, and that it cites MOOSE with its source and the papers MOOSE
asks its users to cite. A plan in German, "PDF-Bericht pro Fall" (called the
report plan below), worked this out with a sample report; the owner approved
it on 4 October 2026 at 06:33 UTC. This ADR records its decisions. "PLAN §n"
is a section of `docs/PLAN.md`; "report plan §n" is a section of the approved
plan, which lives in the project thread, not in the repository.

**Why ADR 0003 excluded a PDF.** Point 5 of ADR 0003 ("No PDF output in v1")
came with the first commit, and OPEN_QUESTIONS #13 recommended the same. Both
were a proposal written in the cloud, not a decision of the owner, and their
reason was the tool, not the content. BOCARTA-MOOSE (BTM) renders BOA's
report with WeasyPrint, which needs Pango, GLib and Cairo from Homebrew and
finds them only through `DYLD_FALLBACK_LIBRARY_PATH`. The hardened runtime
ignores `DYLD_*` variables unless the app carries the
`cs.allow-dyld-environment-variables` exception, and library validation
refuses libraries that neither Apple nor the app's own team signed. So BTM
runs ad-hoc signed with the hardened runtime off and cannot be notarized
(ADR 0003, Context). A renderer that is already in
the signed bundle has none of these problems. Core Graphics and Core Text are
part of macOS. matplotlib is in the lock (3.11.2), signed with the rest of the
runtime, and runs under the worker's guard (ADR 0016, PR #3). Neither needs an
extra library, an environment variable or an exception from the hardened
runtime. The owner's message of 3 October answers OPEN_QUESTIONS #13.

**What BOA's report is.** Read from BTM's code (BOCARTA-MOOSE is read, never
changed; [ADR 0012](0012-what-came-from-bocarta-moose.md)): BOA writes a
`report.pdf` beside `output.xlsx` for each case. It is the body composition
report: eight tissues (bone, muscle, SAT, VAT, IMAT, EAT, PAT, TAT) as volume
and mean HU, per region (thorax, abdomen, mediastinum, pericardium) and per
vertebral level from C1 to S1. TotalSegmentator's organ values are in the
workbook; whether they are also in the PDF cannot be read from BTM. It is
rendered from HTML with WeasyPrint. Page layout and images are not visible in
BTM. The report plan describes them from memory of the BOA work, and that
description is still to be checked against the article (Haubold et al., Invest
Radiol 2024, PMID 37994150; OPEN_QUESTIONS #22). "Analogous to BOA" therefore
means the same idea, one case with its numbers and images at a glance, applied
to MOOSE's structures. It leaves out what BTM saw go wrong with BOA's report:
HTML to PDF, the patient's name and date of birth in the file name, a PDF
written before the table and not rebuilt after a correction, empty rows for
vertebral levels that were never measured, and markers such as "sarcopenia
yes".

**The sample.** A scratch generator (Python and matplotlib, not in the
repository, because the ADR comes before the code) drew the report for the
public PET/CT case P0001 from the app's own long export and labelmaps. The
result, `sample-report-P0001.pdf` in the project thread, has nine A4 pages.
All 415 printed values were checked against the export, and all match. It is
the reference for what the checks below look like. Building it found the two
MOOSE faults that ADR 0018 (PR #3) corrects in the adapter. The sample was
drawn again from the corrected run; it shows the lungs model's own lobes, and
that model passes the side check.

## Decision

### 1. What the report is

- On request, the app writes one PDF per case **in addition to** the XLSX/CSV
  export; it never replaces it. A case is one segmented CT series of one
  patient at one time point, in one run. Pseudonym, timepoint and series name
  it (`P0001_t1_s1`), as the export's suffixes do (PLAN §11).
- It holds the key numbers of every structure (volume in mL, density in HU),
  color segmentation images that show what the numbers rest on, and the
  methods, versions and QC status. The numbers are exactly the export's, read
  from the same project database.
- It is not a finding: no reference values, no traffic lights, no judgment such
  as "abnormal" (section 8).
- The sample's pages are where the implementation starts:

| page | content | images |
|---|---|---|
| 1 Overview | research notice and "does not belong in the patient record"; pseudonym, timepoint (day 0), series, slice thickness, kernel, kV, compute device, QC status; which models ran and how many structures each found; MOOSE with its version and a pointer to the References page; six key-figure tiles (liver, spleen, right and left kidney, skeletal muscle and visceral fat at the L3 level); body regions | coronal overview with organ overlay and legend |
| 2 Organs | volume (mL), density (HU, mean ± SD), median, note | four axial slices: liver and spleen, kidneys, pancreas, urinary bladder |
| 3 Lungs and airways · Heart and vessels | two tables | coronal in the lung window, axial through the heart |
| 4 Digestive tract · Muscles | right and left muscles side by side | coronal slices |
| 5 Spine | vertebrae from C1 to the sacrum in three columns | sagittal in the bone window with vertebra names, coronal MIP |
| 6 Sternum and ribs · Other bones | right and left side by side; missing bones in a short list instead of empty rows | coronal whole-body MIP |
| 7 Body composition at the L3 level | box "L3 level only" (20 slices); skeletal muscle, subcutaneous and visceral fat with volume and density | axial slice at mid-L3, sagittal locator with the measured slab |
| 8 Definitions and quality checks | the definition of every value; QC flags in plain words; the side check | – |
| 9 Methods and references | methods text with the adapter's corrections (ADR 0018), QC status, versions, model checksums, intended use; References (section 6) | – |

- Every page has a header with pseudonym, timepoint and series, and a footer
  with the per-page notice (section 8), "Page x of y" and the QC status.
- Where two models segment the same structure (the lung lobes, in the organ
  and the lungs model), the report shows the dedicated model's.

### 2. Renderer: the worker supplies the content, the app draws

- **Worker.** As an option of the export job, the worker writes one small
  content file per case, `<pseudonym>_t1_s1.report.json`, below
  `<project>/exports/` (ADR 0013 point 3, PR #2). It holds the values, the QC
  status, the notes, the methods text and the provenance. The worker computes
  them with the export's own loader and QC logic (ADR 0013), so the report and
  the workbook cannot drift apart. The methods text is the export's paragraph,
  never retyped.
- **App.** BCOAKit draws the PDF from that file with Core Graphics (a PDF
  context) and Core Text. The images come from the viewer's building blocks:
  orientation and letters from `PlaneMapping`, windows from `Windowing` and
  `WindowPreset`, colors from the color table the viewer gets in M4 (planned).
  Core Text wraps lines and measures columns. A tagged PDF for screen readers
  is possible.
- **Why not everything in Python.** Orientation, windows and colors would
  exist twice, and [ADR 0007](0007-viewer.md) deliberately put them in one
  tested place. matplotlib also has no layout engine: in the measurement a
  column heading of the German prototype ran over its column (75.6 pt of text
  in 65.5 pt), and the sample had to measure every cell itself.
  **Why not everything in Swift.** The export's QC and exclusion logic would
  have to be written a second time.
- **A spike comes first** (step 4 in section 12), on the CI's macOS runner. It
  draws in Swift the same two-page report for P0001 that matplotlib was
  measured with. It passes only when all four criteria hold:
  1. under 1 s per case;
  2. PDFKit reads the text back, and the text is English even when macOS runs
     in German;
  3. the layout holds with the longest structure name of the catalog;
  4. the PDF is written in the sandboxed export folder.
- **If the spike fails**, the whole report is drawn with matplotlib in the
  worker, which the sample proved possible: one multi-page A4 file, TrueType
  fonts embedded by file, fixed metadata. The checks and tests of this ADR stay
  the same; only the place where the drawing happens changes.

### 3. Images

- **Source.** The converted CT is deleted after the pipeline (PLAN §8, cleanup
  rule). The report therefore takes its images where the viewer takes them:
  from the viewer cache in canonical LPS (PLAN §8, stage 5), which is rebuilt
  from the DICOM files when it is missing.
- Radiological orientation with R/L or A/P letters and a 5 cm scale bar. The
  window follows the content: soft tissue W 400/L 40, lung W 1500/L −600, bone
  W 1800/L 400 (the presets of PLAN §9).
- A semi-transparent overlay; every structure has the same color on every
  page.
- Coronal and sagittal images keep their true aspect ratio. No image shows a
  face, from the front or in profile. "Cut off below the skull base" alone
  would not ensure that, because nose, lips and chin lie below it, so the
  report follows the rules the sample drew by. The cut levels come from the
  masks (in the sample, from the vertebra and bone models), so they follow the
  patient rather than a fixed slice number:
  - every coronal and sagittal image ends at the top of the atlas (C1), at the
    skull base; no image passes through the head, and there is no 3D surface
    image;
  - coronal images and projections end lower still, below the lowest point of
    the mandible (about C6 in the sample), because a frontal view of the jaw
    is a face too;
  - a sagittal image that reaches above the mandible is cropped front to back
    to a narrow margin in front of the vertebral bodies, so that lips, chin
    and nose never appear (the sample's spine image runs up to C1 this way);
  - a projection that has to reach above the mandible (the whole-body bone MIP
    up to the shoulders) shows there only the labeled bones other than the
    skull (spine, ribs, shoulder girdle and limbs), never the skull or the soft
    tissue of the head.

  Brain and thyroid therefore appear in the tables only.
- Images are on by default, because the owner wants them.

### 4. Structure names

- A name catalog covers all **172 structures of the 11 MOOSE models** the app
  bundles: the ten clinical CT models with their 144 reported labels, and
  `clin_ct_fast_vertebrae`, which body composition runs first (ADR 0011
  point 5). Each structure has an English name for the report and the app, a
  short form of at most 18 characters for narrow columns, an anatomical group
  and an order. The German name stays in the catalog for a later German
  interface and never appears in the report.
- **Rules.** The side comes first, as English reports write it ("Left kidney",
  short "L kidney"). Right comes before left; within a group the order runs
  cranial to caudal, and the heart follows the direction of flow. Muscles and
  vessels carry the names of everyday English reports ("Left iliopsoas",
  "Portal and splenic veins", "Left iliac artery"), Latin only where English
  uses it too ("Inferior vena cava"). Vertebrae and ribs read "C1 vertebra
  (atlas)", "L3 vertebra" (short "L3") and "Left 1st rib"; variants are named
  as such ("L6 vertebra (variant)", "Left 13th rib (accessory)"). Body
  composition always carries "(L3 level)", because MOOSE measures it only
  there. Groups follow anatomy, not models: Organs; Lungs and airways;
  Digestive tract; Heart and vessels; Muscles; Spine; Sternum and ribs; Other
  bones; Body regions; Body composition.
- Where the MOOSE word could mislead, the name says what the mask holds,
  checked on the public case's masks: `heart_myocardium` is "Left ventricular
  myocardium" (only the wall of the left ventricle), `pulmonary_artery`
  "Pulmonary trunk and main branches" (it reaches into both hila), `colon`
  "Colon and rectum", `head` "Head and neck" (it reaches down to C5/C6),
  `hip_left` "Left hip bone", `autochthon_left` "Left paraspinal muscles",
  `skeletal_muscle` "Skeletal muscle (L3 level)".
- The names apply in the whole app, in the viewer and the lists as well as in
  the report. They replace the mechanical names of `LabelNames.displayName`
  (BCOAKit) and `naming.display_name` (worker), which turn `kidney_left` into
  "Kidney left". The catalog has one source, the reviewed catalog file that
  enters the repository with step 2 (planned). The app gets the names through
  the String Catalog: once in the app's own table, which a later German
  interface translates, and once in the report's table (section 5), which is
  never translated; both tables are generated from that file. The worker needs
  the names too, for the `labels` sheet and for the matplotlib fallback, and a
  compiled String Catalog cannot be read from the bundled Python, so it reads
  the same file from the bundle. A test holds both tables and the worker to
  the catalog file.
- The export's column names stay as they are. They keep the sanitized MOOSE
  names, because analyses and citations are keyed on them; the catalog names
  are for display only.
- A test fails the build when a label of a bundled model has no name
  (section 11).
- The catalog was drafted first and then reviewed label by label. The
  radiological review by the owner is still open (OPEN_QUESTIONS #20). The
  catalog enters the repository with step 2 (section 12).

### 5. Language, numbers and paper

- The report is always in US English, whatever the app's or the system's
  language (the owner's decision of 3 October 2026). Its texts and structure
  names live in a String Catalog table of their own (for example
  `Report.xcstrings`, planned), whose entries are never translated and which
  the report always reads in English. A later localization of the app
  therefore leaves the report as it is.
- Numbers always carry a decimal point and no thousands separator, whatever
  the system region. Report plan §5 rules the separator out below 10 000; the
  sample prints none at all (25746.3 mL for the trunk), and the report follows
  the sample, because many readers outside the US take a separator between
  thousands for a decimal mark (a working decision, section 14). A date, where
  one is shown at all (section 8), follows ISO 8601 (2026-10-03).
- The paper size is A4.

### 6. The References page

The last page, "Methods and references", carries these lines, printed
verbatim in every report (the 18 of 18F is set as a superscript). As in the
sample, the last paragraph stands under a heading of its own, "Software and
licenses", and the intended use follows it. The References test (section 11)
checks against this text:

> Segmentations were produced with MOOSE. If you use these results, please cite the three works the MOOSE authors ask for:
>
> [1] Shiyam Sundar LK, Yu J, Muzik O, et al. Fully automated, semantic segmentation of whole-body 18F-FDG PET/CT images based on data-centric artificial intelligence. J Nucl Med. 2022;63(12):1941-1948. doi:10.2967/jnumed.122.264063
>
> [2] Ferrara D, Pires M, Gutschmayer S, et al. Sharing a whole-/total-body [18F]FDG-PET/CT dataset with CT-derived segmentations: an ENHANCE.PET initiative. Sci Data. 2026. doi:10.1038/s41597-026-07218-y
>
> [3] Isensee F, Jaeger PF, Kohl SAA, Petersen J, Maier-Hein KH. nnU-Net: a self-configuring method for deep learning-based biomedical image segmentation. Nat Methods. 2021;18(2):203-211. doi:10.1038/s41592-020-01008-z
>
> MOOSE 3.2.2 (package moosez), source: https://github.com/ENHANCE-PET/MOOSE. MOOSE code: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0). Model weights © the MOOSE authors (release moosez-v.3.1.3), licensed under CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/), provided as is, without warranties. Modified for this app: optimizer and training state removed from each checkpoint; network weights unchanged. nnU-Net v2 (package nnunetv2) is licensed under the Apache License 2.0.

Where each part comes from:

- **The three works.** Which works, their years and DOIs, and the titles of
  [2] and [3] are as the "Citations" section of the bundled moosez 3.2.2's
  README gives them. That README lists them in the order Ferrara, Shiyam
  Sundar, Isensee; the approved page prints them in the order above. The title
  of [1] follows the journal's spelling; the README hyphenates
  "Fully-automated". The README gives the nnU-Net paper's volume and pages but
  not its issue; the issue and the five authors come from nnU-Net's own
  citation request. Volume, issue and pages of the MOOSE paper (J Nucl Med
  2022;63(12):1941-1948) were confirmed by the owner on the journal's article
  page on 4 October 2026. MOOSE gives no article number for the dataset paper,
  and PubMed and Crossref are blocked from the cloud, so the report prints its
  DOI, which finds the article unambiguously.
- **"MOOSE 3.2.2 (package moosez)".** The version comes from the run's
  provenance (`runs.versions_json`). The texts are written for 3.2.2, the only
  version the adapter accepts (`SUPPORTED_MOOSEZ` in `moose_adapter.py`). The
  source is MOOSE's repository.
- **The license paragraph.** The code license is Apache License 2.0 (`LICENSE`
  in moosez's source archive). The weights are under CC BY 4.0 (MOOSE's
  `MODEL_LICENSE` and the README); `moosez-v.3.1.3` is the GitHub release in
  the archives' download URLs (`Models/manifest.lock.json`). "Provided as is,
  without warranties" carries the license's disclaimer. The last sentence
  records what `Scripts/slim_checkpoints.py` changes (PLAN §4, build step 5).
  Slimming is treated, to be safe, as a modification in the sense of CC BY 4.0
  §3(a)(1)(B), which is why the report states it. Whether that would be
  required can be settled by the legal office together with OPEN_QUESTIONS
  #14 (PR #3).
- **A change to the approved wording.** Report plan §3 approved the license
  paragraph without the two URLs and without the nnU-Net sentence; the sample
  added both, and the report keeps them. This ADR records that as a change to
  report plan §3. The URL after "CC BY 4.0" is not optional: CC BY 4.0
  §3(a)(1)(C) asks for the license text or its URI with the attribution, and
  PLAN §14 asks for the license link of the weights, so a later trim back to
  the shorter wording would break the attribution. The Apache URL treats the
  code license the same way, and the nnU-Net sentence gives nnU-Net v2, which
  the bundle carries and [3] cites, its license too.

The methods text before the references names the adapter's corrections to
MOOSE (ADR 0018: both when the lungs model is in the export, otherwise the
block-edge fill alone), so that nobody takes the numbers for unchanged MOOSE output. The report is never called a "MOOSE report": CC BY 4.0 §2(a)(6) rules
out any suggestion of endorsement, and Apache-2.0 §6 grants no use of the
licensor's names beyond describing the origin of the work. Apple 4.1(c), which
keeps "MOOSE" out of the app's name and icon (PLAN §14), points the same way.

### 7. Checks before drawing, and the side check

The report is not drawn when:

- a structure has no name;
- label number and name disagree between the model, the export and the
  catalog;
- the voxel count of a mask does not match the export.

The sample checked the voxel counts of all 144 structures against the
labelmaps before drawing anything, and stopped at a label without a name
rather than print a raw MOOSE name.

**Side check.** For every model with left and right labels, the renderer
checks that the sides agree with the anatomy. The sample compares the
centroid of each such label along the patient's left-right axis with the
midline of the trunk (from the body-region model); a label whose centroid lies
within 15 mm of the midline counts for neither side and is named as unchecked.
A model whose labels lie mostly on the side opposite their names (in the
sample: two or more, and more than lie on their named side) is not shown at
all and is named in the quality checks. A single label on the wrong side is
marked beside its value and explained; in the sample that was the right finger
phalanges, cut off at the image edge, of arms raised above the head and
reaching across the midline. The check was added because `clin_ct_lungs` put
all five lobes on the opposite side. ADR 0018 corrects that model in the
adapter, so the report plan's interim rule (take the lobes from the organ
model) is obsolete. The check stays as a safeguard against the next model or
weight set that sees the image mirrored.

### 8. What the report must and must not contain

**Why a case report is more delicate than the table.** A cohort table looks
like research data. A PDF per patient is the form in which information arrives
at a decision about a person. Under MDR Art. 2(1) and MDCG 2019-11 the
intended purpose decides whether software is a medical device, and that
purpose is read from what the product says and shows, the content of this PDF
included. A "research only" notice protects only as long as the content agrees
with it. With a medical purpose, quantitative image analysis would fall under
Rule 11 into class IIa at least, which requires a notified body; BTM's README
says the same. These legal statements are from knowledge and **still to be
checked against the sources**: the EUR-Lex and Apple pages could not be opened
from the cloud.

**Must contain:**

- on **every** page, inside the printable area, the notice "For research use
  only. Not for clinical use. Not a medical device; not for diagnosis or
  treatment decisions." Its first two sentences are `IntendedUse.short`, the
  notice PLAN §2 rule 5 puts on every export, word for word, so the report
  carries that notice like any other export. The rest is what report plan §6
  adds for a case report, in the wording the sample prints on pages 1 and 9.
  The report takes the whole notice from one constant built from
  `IntendedUse.short` (for example `IntendedUse.reportNotice`, planned), and a
  test checks every page against it;
- on page 1 the full intended-use statement, verbatim from
  `IntendedUse.statement`, checked by a test against the constant. The sample
  does not yet do this or the per-page notice: its footer reads "Research use
  only. Not for diagnostic use. Not part of the patient record.", which is
  neither `IntendedUse.short` nor says "not a medical device", and its page 1
  carries a shorter notice instead of the statement;
- a sentence that the document does not belong in the patient record;
- the QC status, clearly visible; no report at all for a rejected series;
- a unit and a definition for every value; a missing value stays empty and
  gives its reason; technical notes stand beside the value in plain words:
  "cut off at the image edge, volume incomplete", "outside the field of view",
  "not found, an accessory structure that only some people have" (the L6
  vertebra, the 13th rib);
- the provenance, the References page (section 6) and the CC BY attribution of
  the MOOSE weights;
- the pseudonym and "Page x of y" on every page.

**Must not contain:**

- reference values, percentiles, z-scores, thresholds (sarcopenia, steatosis,
  osteoporosis, SUV categories), or traffic-light or arrow marks on values;
- headings such as "Findings", "Impression" or "Recommendation"; a signature
  field, a referring physician, a letterhead;
- the planned `volume_outlier` flag as an "abnormal organ". At most it appears
  as a note on the segmentation ("volume unusual for this project, check the
  mask"), or it stays out;
- sending to a PACS, a DICOM encapsulated PDF, mail, iCloud;
- JavaScript, forms or attachments in the PDF.

**Privacy defaults.** GDPR Art. 25 asks for data protection by default, and a
pseudonymized report is still health data under Art. 9.

| what | default |
|---|---|
| identifier | the pseudonym only (P0001); no PatientID in the report, whatever the export's identifier option (PLAN §11) |
| date | days since the first study ("Day 0"); the year as an option; the full date only after a warning |
| age, sex | whole years at the first study; sex as recorded |
| file name | `P0001_t1_s1_case_report.pdf`, checked against a fixed pattern |
| PDF metadata | the title with the pseudonym only, the author empty, the subject the research notice, no XMP patient fields |
| storage | `<project>/exports/`; "Save a Copy…" to a folder the user chooses, with a warning for iCloud, network or removable volumes |
| printing | from Preview; printing inside the app would need the sandbox entitlement `com.apple.security.print` and therefore an ADR of its own |
| images | on, because the owner wants them; no face from the front or in profile, cut as section 3 describes; no sagittal midline slice through the head, no 3D surface image |

**What only the owner or the institution can decide.** The institution's
regulatory office gives a written classification "not a medical device" along
MDCG 2019-11, with the case report as part of the product, before reports on
real cases are made (OPEN_QUESTIONS #21). The data protection officer decides
the legal basis and the data protection impact assessment, whether a PatientID
or a full date may ever appear, and how reports may be passed on and printed;
that belongs to OPEN_QUESTIONS #6. Until that decision the report shows no
PatientID at all. The report never calls the person a "patient" or a
"subject": it speaks of a case with a pseudonym, as the sample does. The
phrase "patient record", in the required sentence under "Must contain", is
the one exception.

### 9. Output, copies, printing and font

- The app writes the PDF into `<project>/exports/` as
  `P0001_t1_s1_case_report.pdf`, a name checked against a fixed pattern. The
  report plan wrote it as `P0001_t1_s1_fallbericht.pdf` in its German text;
  the file name follows the report's language (a working decision,
  section 14).
- "Save a Copy…" copies it to a folder the user chooses, with a warning when
  that folder lies on iCloud, a network volume or a removable volume.
- Printing happens in Preview, where the app opens the finished file. The app
  gets no `com.apple.security.print` entitlement.
- The report uses a font from the bundle, registered for the app's process
  only (`CTFontManagerRegisterFontsForURL` with the process scope), so that it
  looks the same on every macOS and nothing is installed for the user. The
  font is DejaVu Sans, which the sample used and which matplotlib already ships
  inside the bundle. Its license, matplotlib's `LICENSE_DEJAVU` (the Bitstream
  Vera license, the Arev fonts license for the glyphs taken from Arev, and the
  DejaVu changes in the public domain), goes into the third-party notices;
  `Scripts/license_report.py` already copies that file, because matplotlib's
  `RECORD` lists it.

### 10. The report's place in the export, the run and the protocol

- The content file is an **option of the export job**, not a new job kind.
  Only the export options change; the file is announced with the existing
  `artifact` event, whose kind `report` the v1 schema already lists. The IPC
  protocol therefore stays at version 1 ([ADR 0005](0005-process-model-and-ipc.md),
  PLAN §5).
- A report counts as an export. It locks the run (PLAN §6), goes into the
  audit log with its scope, destination and the SHA-256 of the file, and never
  goes into the reproducibility package.
- A report shows the values as they are when it is drawn. When a value it
  shows changes (a QC decision, a label exclusion), the report is drawn again
  rather than kept. BOA writes its PDF before the table, and BTM does not
  rebuild it after a correction.

### 11. Tests

The tests are written before the code, as PLAN §20 asks for export and
orientation.

**Worker (pytest), the content file:**

- **Same numbers.** Every number in the content file equals the matching row
  of the export of the same project, checked on the public case with its 144
  rows.
- **Honest QC.** An excluded label appears empty with its reason, a rejected
  series not at all, and an unreviewed case says "not visually reviewed".
- **Notes.** `empty_label` and `truncated` arrive with their plain-word text.
- **Privacy.** The file holds no piece of a path, no UID and no PatientID
  (section 8), and no date other than relative days unless the export option
  explicitly allows the year or the full date.
- **Determinism.** Two runs write byte-identical files.

**App (swift test on the macOS runner), the PDF:**

- **Complete name catalog.** Every label of every bundled model has an English
  name, no name occurs twice within a model, and no short form is longer than
  18 characters. CI has no bundle with every model (the `swift` job fetches
  none, `bundle.yml` the organ model), so the test reads the labels from
  `Models/manifest.lock.json`: step 2 extends `fetch_models.py --update-lock`
  to record each model's labels there (today it records only URL and
  checksum, and only for a model not yet in the lock). A model cannot be
  fetched without a lock entry, so a new model without names turns the build
  red. (Changed after the merge audit of 4 October 2026; the first version
  read the bundle's `manifest.json`, which CI never has for every model.)
- **Content read back.** PDFKit reads the PDF back. The test checks the page
  count, the per-page notice of section 8 on **every** page against its
  constant, and that every value of the content file stands in the text as the
  report formats it ("809.1" for the liver of the public case after ADR 0018).
- **Orientation.** A phantom with a marker only in the patient's right must
  appear on the left of the report's image, with the letter R on the correct
  side. It is the same test as the viewer's.
- **No face.** The renderer records the extent of every image in patient
  coordinates, and a test on the public case checks it against section 3: no
  image above the atlas; above the lowest point of the mandible no coronal
  slice, nothing in a projection but the labeled bones other than the skull,
  and every sagittal image cropped to the margin in front of the vertebral
  bodies.
- **Layout.** No text is clipped or runs over its cell. Core Text measures this
  for the longest structure name of the catalog too (today "Pulmonary trunk and
  main branches").
- **English and sources.** One test draws the report with German as the system
  language and checks that the text stays English and the numbers keep a
  decimal point. A second checks the References page against the fixed text
  of section 6, with MOOSE's version taken from the run's provenance: the
  source, the three works with their DOIs, the license URLs and the CC BY
  attribution.
- **Metadata.** Title, author, subject and keywords hold nothing that
  identifies a person beyond the pseudonym.
- **No byte comparison.** Quartz writes the date and the macOS version into the
  file, so the content is compared instead. CI also keeps the pages as PNG
  files, so that they can be looked at.

**By hand, once before acceptance:** the owner or another radiologist checks
the name list and the sample report; a test print on A4; the report and the
workbook of the public case side by side show the same numbers.

**Measured and recorded in `docs/benchmarks.md`:** time and size per report on
the Mac.

### 12. Milestone and order

The rule "M0 first, then features" still holds, and the report is built after
M0. Steps 2 and 3 are export logic and fall under the plan's exception "tests
first for metrics, export and orientation". Acceptance is a milestone of its
own, **M6b** ("Fallbericht", the case report, in PLAN §17), between M6 and M7.

| # | step | depends on | size |
|---|---|---|---|
| 1 | this ADR and the plan changes | the owner's approval | small |
| 2 | the name catalog in the String Catalog; the test "every label of every model has an English name" | 1 | small; also helps the viewer in M4 |
| 3 | the content file from the export code; a golden test on the public case | 1, PR #2 | medium |
| 4 | spike: drawing in Swift on the macOS runner | 1 | small |
| 5 | the renderer with tables and images | 3, 4, M3 (conversion, metrics, viewer cache), M4 (colors) | large |
| 6 | QC status and exclusions in the report; the menu item "Create Case Report…" | M6 | medium |
| 7 | acceptance as M6b before M7; M7 (privacy review, VoiceOver, user guide) then covers the report as well | 5, 6 | – |
| 8 | Phase 2: a body composition page with L3 areas and SMI (needs the body height) and a PET page with SUV per organ | PLAN §18, OPEN_QUESTIONS #8 | medium per page |

Acceptance of M6b: for the public case, the report and the workbook show the
same numbers and QC states, and time and size per report are in
`docs/benchmarks.md`.

### 13. Not in the report

- Body composition per vertebral level as in BOA: MOOSE measures it only at
  L3.
- The Phase 2 pages: L3 areas in cm² and the skeletal muscle index (they need
  the body height), and PET with SUV per organ (PLAN §18). Which comes first is
  OPEN_QUESTIONS #8; the report plan recommends PET, because the app carries
  PET-CT in its name and BOA has no PET values.
- The course over several time points.
- Any evaluation of the numbers.

### 14. Working decisions taken from the approved plan

The owner approved item 1 of report plan §9 (the plan itself), and item 2
(the report's language) was the owner's decision of 3 October. The plan says
a yes to item 1 is enough to start this ADR and the plan changes and that the
other items can follow. This ADR therefore adopts the plan's recommendations
for these items as working decisions; the owner can still overrule any of
them through OPEN_QUESTIONS:

- item 3, technique: the app draws, the worker supplies the numbers;
  matplotlib if the spike fails (section 2);
- item 4, everyday names in the viewer and the lists as well, not only in the
  report (section 4);
- item 6, images on by default, with no face, from the front or in profile
  (section 3);
- item 8, A4 (section 5);
- item 9, printing from Preview, no print entitlement (section 9);
- item 10, the report never calls the person a "patient" or a "subject" but
  speaks of a case with a pseudonym; "patient record" in the required sentence
  is the one exception (section 8);
- item 12, cite the dataset paper (Ferrara et al., Sci Data 2026), as MOOSE
  now asks, also on the app's About page and in the export's methods text. The
  first methods sentence, used alike in PLAN §11 and in the export code, is:
  "Segmentations were generated with MOOSE v\<version> (Shiyam Sundar et al.,
  J Nucl Med 2022; Ferrara et al., Sci Data 2026), based on nnU-Net (Isensee
  et al., Nat Methods 2021), using \<AppName> v\<version> on \<chip> (PyTorch
  \<device>)." The About view says the same in the present tense.

Where the approved sample differs from the report plan's text, this ADR
follows the sample, and these are working decisions the owner can overrule in
the same way:

- numbers carry no thousands separator at all (25746.3 mL), where report plan
  §5 rules it out only below 10 000 (section 5);
- the file name is English, `P0001_t1_s1_case_report.pdf`, where report plan
  §6 wrote `P0001_t1_s1_fallbericht.pdf` (section 9).

The third difference, the two license URLs and the nnU-Net sentence on the
References page, is not a choice; section 6 gives the reason.

Still open, because only the owner or the institution can answer them: item 5,
the radiological review of the name catalog (OPEN_QUESTIONS #20); item 11, the
regulatory classification by the institution before reports on real cases
(#21); the check of the BOA comparison against the article (#22). Item 7, the
order of the Phase 2 pages, is OPEN_QUESTIONS #8 (section 13).

## Consequences

- **What the app gains.** The per-case view the owner asked for, with images
  that show what each number rests on and with the export's numbers by
  construction. The name catalog improves the viewer and the lists as well,
  and its test makes a model without names a failed build.
- **What it costs.** A renderer to build and test (step 5 is large), a second
  consumer of the export's loader, and a font in the bundle. A PDF per patient
  also gives a regulator more to read the intended purpose from (section 8),
  which is why the institution's classification comes before reports on real
  cases. A report needs the viewer cache or the source DICOM: with the cache
  evicted and the source folder offline, there is nothing to draw the images
  from.
- **ADR 0013 (export, PR #2).** The content file is built from the export's
  loader, its QC decisions and its methods text (points 1 and 6), its region
  rule shows up as "(L3 level)" and the "L3 level only" box (point 7), and the
  worker writes the file below `exports/` (point 3). The export's methods text
  gains the dataset citation in PR #2, and the report prints that text. The
  `labels` sheet's `display_name` column, defined as the label name shown in
  the app, follows the catalog once step 2 lands; no column name changes.
- **ADR 0018 (PR #3).** The export's methods text names the adapter's
  corrections (both when the lungs model is in the export) since PR #2 met
  PR #3, held to PLAN §11 by a test, and the report prints it from there. The side check stays although the lungs model
  is now corrected.
- **ADR 0016 (PR #3).** The matplotlib fallback runs under the worker's guard.
  In the measurement the guard refused one call (`fc-list`, when the first
  font cache was built), and the PDF was written anyway.
- **ADR 0003.** Point 5 is superseded; its actual rule stays in force: no
  WeasyPrint, Pango or Cairo, no `DYLD_*` variables, and a PDF only from code
  that is signed in the bundle. ADR 0012 is unaffected, since BTM's WeasyPrint
  report is still not taken over.
- **The intended-use statement** says "in CT data". It has to grow with PET in
  Phase 2, in PLAN §13, `Worker/bcoa_worker/intended_use.py` and
  `IntendedUse.swift` together, and the report prints whatever the constant
  says.
- **Open items.** OPEN_QUESTIONS #20 (review of the name catalog), #21
  (regulatory classification), #22 (the BOA article), #8 (the order of the
  Phase 2 pages), #14 (CC BY 4.0 and the App Store license, PR #3), #23
  (where the report files of two runs or exports go, before step 3) and #6
  (data protection officer).

## Rejected alternatives

- **Everything in Python with matplotlib, as the primary path.** Measured and
  working on Linux with four cores on the public case P0001: two pages with two
  images in 4.3 to 5.7 s, of which 0.6 to 0.8 s for writing; 266 kB, 85 % of it
  images; DejaVu embedded and extractable as text; byte-identical with a fixed
  date. The nine-page sample took 48 to 61 s per build, 21 to 30 s of it for
  reading and checking the CT and the masks, and came to 3.5 MB, almost all of
  it images; two runs of the German eight-page prototype before it wrote
  byte-identical files. It is already in the signed bundle, runs under the
  guard and adds no dependency. It is not the primary path because
  orientation, windows and colors would exist a second time and matplotlib has
  no layout engine (section 2). It stays as the fallback if the spike fails.
- **SwiftUI `ImageRenderer`.** Fine for the Store and no new dependency, but a
  variant of the chosen path that puts the layout logic into views, where it
  cannot be tested the way BCOAKit can.
- **WebKit and HTML.** WebKit starts helper processes of its own, and the
  result is hard to test.
- **ReportLab.** Fine for the Store, but a new dependency that needs an ADR and
  a license check, and the chosen path does not need it.
- **fpdf2.** LGPL-3.0 and a new dependency.
- **WeasyPrint.** Needs Pango, GLib and Cairo (LGPL) and blocks notarization;
  it is the reason for ADR 0003.
- **No PDF in v1** (ADR 0003 point 5, OPEN_QUESTIONS #13). It rested on
  WeasyPrint, not on the content, and the owner wants the report.
