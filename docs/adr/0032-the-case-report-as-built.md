# ADR 0032: The case report as built (M6b)

- Status: accepted (built on the owner's request of 23 October 2026 to
  continue the plan)
- Date: 2026-10-23
- Plan section: §9, §11, §12, §13, §17; implements [ADR 0017](0017-per-case-pdf-report.md)

## Context

ADR 0017 decided what the per-case PDF holds, what it must not hold and how
it is tested, and left the spike (step 4) to decide between drawing in Swift
and the matplotlib fallback. Building steps 2 to 6 settled that and a few
questions ADR 0017 left open or answered differently from what the code
could do. This ADR records them, so that ADR 0017 stays the specification
and this one says where the build departs from it and why.

## Decision

1. **The spike passed; the app draws.** `CaseReportRenderer` (BCOAKit)
   draws with Core Graphics and Core Text. On the public ACRIN case it wrote
   14 A4 pages in 0.27 s with the cache in memory, 1.86 s with the cache
   read cold from the external disk (`docs/benchmarks.md`); PDFKit reads the
   text back; the text is fixed English with a decimal point whatever the
   region; the sandboxed app wrote it into `exports/`. The matplotlib
   fallback is not built.
2. **Fonts from their files, not registered.** ADR 0017 section 9 registers
   DejaVu Sans for the process. Core Text builds the font straight from the
   file's descriptor instead (`CTFontManagerCreateFontDescriptorsFromURL`),
   so nothing is registered at all, not even for the process; the PDF embeds
   subsets of DejaVu Sans and DejaVu Sans Bold. Without the files (the
   package's tests) it draws in Helvetica.
3. **The content file's place and spelling.** The files go into
   `exports/<stem>_reports/` as OPEN_QUESTIONS #23 recommended, so two
   exports never overwrite each other's reports. The content file carries
   the export's spelling of the MOOSE words (`vertebra_l3`, `legs`); the
   renderer compares the catalog's words in that spelling
   (`LabelNames.sanitized`, the same rule as `naming.sanitize`). The first
   run on the real case refused to draw because the two spellings were
   compared as they were, which is what the check is for.
4. **Reports alone.** The export option `tables` (default true) lets an
   export write the reports without a workbook. The results list's menu
   item is "Create Case Reports" for the selected patients, without an
   ellipsis because it asks nothing; it exports with `tables` off and the
   dialog's date choice, never the full date, which needs the dialog's
   warning.
5. **The face rule, as conservative as the landmarks allow.** Coronal and
   sagittal images end at the lowest of: one slice below the head-and-neck
   region of the body-region model, the top of C7, the top of the lungs. A
   CT with none of them but abdominal organs ends at the top of those organs;
   with no landmark at all there is no coronal or sagittal image. ADR 0017
   section 3 cuts coronal images below the mandible and lets a sagittal
   image reach C1, cropped to the vertebral bodies; the build cuts both at
   the same, lower level. The spine image therefore shows no cervical spine
   above the lung apex, and the tables still list C1 to C7. The projections
   (coronal MIPs of the skeleton) are not built. OPEN_QUESTIONS #38 asks
   whether the crop of section 3 is wanted for the cervical spine.
6. **Pages follow the groups.** Instead of the sample's nine fixed pages the
   report flows: an overview (notice, case, models, six volume tiles, a
   coronal image with legend), one section per group of the catalog with its
   table and images, then "Definitions and quality checks" and "Methods and
   references" on pages of their own. The public case has 14 pages. Every
   page has the header with pseudonym, time point and series, and the footer
   with the notice, the QC status and "Page x of y".
7. **Structures not found** stand in a sentence under their group's table,
   not as empty rows; an L6 vertebra or a 13th rib that is not there is
   named as a structure only some people have. An excluded structure stays
   in the table, empty, with "excluded in QC" and the reviewer's comment.
8. **Two models, one structure.** Where two models segment the same
   structure, the one with fewer labels is shown (the lungs model's lobes,
   not the organs model's); between the two vertebra models the clinical
   one.
9. **The cache of the run, else the series' old one.** Projects from before
   ADR 0031 decision 11 kept one viewer cache per series. The renderer falls
   back to it; when it belongs to another run with other masks, the voxel
   check refuses the report rather than draw images of another
   segmentation.
10. **The worker in the bundle is the checkout's.** The Debug app's first
    export with case reports failed while the command line succeeded: the
    bundle carried the worker as `make worker` had last copied it, which
    refused `case_reports` as an unknown option. `Scripts/embed_backend.sh`
    now copies `Worker/bcoa_worker` on every build, and the app logs an
    export's failure code.
11. **Names everywhere.** The viewer and the cohort view show the catalog's
    names (`StructureCatalog.displayName`); the export's columns keep the
    MOOSE words. The catalog is read as JSON from the bundle. The String
    Catalog tables of ADR 0017 section 4 are not generated yet: the report's
    texts are literals in `ReportText`, which no translation reaches, and a
    German interface will generate its table from the same JSON.

## Consequences

- M6b's acceptance criterion holds for the public case: the report and the
  workbook come from the same builder, 144 of 144 masks agree with the
  export's voxel counts, and every volume of the content file stands in the
  PDF as the report formats it (`reportsOfARealProject`).
- Still open before M6b is accepted: the owner's radiological review of the
  names and of a sample report (OPEN_QUESTIONS #20), a test print on A4, and
  the institution's classification (OPEN_QUESTIONS #21) before any report of
  a real case.
- Not built from ADR 0017 section 11: a tagged PDF for screen readers (M7),
  the pages as PNG files in CI, and a run with German as the system language
  (the number format is tested on its own, and the report uses no localized
  string).
