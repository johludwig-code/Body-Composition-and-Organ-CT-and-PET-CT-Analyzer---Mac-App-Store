# User Guide

*Body Composition and Organ CT and PET-CT Analyzer (working title "BCO
Analyzer"). For research use only. Not for clinical use. Not a medical
device; not for diagnosis or treatment decisions.*

Research software for the automated segmentation and quantification of
anatomical structures in CT data. Not intended for diagnosis, treatment
planning, or any other clinical decision-making. It carries no CE marking
and no FDA clearance, and its results are not validated for any clinical
purpose.

Every segmentation and every number is a research measurement that a
qualified person must check before it is used. Results do not belong in a
patient's record. Ethics approval, data protection and the choice of data are
the responsibility of the study's investigators. Provided as is, without
warranty of any kind, to the extent the law permits.

The app indexes folders of DICOM files, picks one CT per study, segments it
with MOOSE on the Mac's GPU, lets you check every segmentation in a viewer,
and exports the numbers to Excel or CSV, one row per patient. Nothing leaves
the Mac: the app has no network access at all.

## What you need

- A Mac with Apple Silicon (M1 or later) and macOS 14 or later.
- 16 GB of memory or more; MOOSE needs about 8 to 12 GB for a whole-body CT.
- About 3 GB of disk for the app, and room in each project folder for the
  segmentations (a few MB per CT and model) and the viewer copies (the CT
  once more, and half of that per model).

## Installing

Open the disk image and drag the app to Applications. A build signed by the
institution and notarised by Apple opens with a double click. A development
build (signed "ad hoc") opens on the Mac it was built on; on another Mac,
Control-click the app, choose Open, and confirm once.

## 1. A project

**File → Open or Create Project…** and choose an empty folder. A project
holds the index, the results, QC and exports of one study. Keep project
folders on a FileVault-encrypted disk: they hold patient IDs until you
remove them, and folder names often hold patient names.

## 2. Source folders

**File → Add Source Folders…** (⇧⌘A) and choose one or more folders with
DICOM or NIfTI files. They are only ever read. Choose the folder above the
per-patient folders; its name is kept in the project.

The app indexes the folders at once: it reads the headers, groups the files
into studies and series, and picks one CT per study. A network folder takes
longer. **Scan All** reads only what changed; a scan can be cancelled and
continues where it stopped.

## 3. Patients and series

**Patients & Series** shows patient → study → series under pseudonyms
(P0001 …). A star marks the primary series of a study, the one the export
uses; the status column says why a series was chosen or not, in full when
you point at it. A warning sign lists the checks of a series, such as gaps
between slices or a gantry tilt.

- Tick or untick series by hand; your choice survives every rescan.
- **Apply Auto-Selection to All** returns to the automatic choice.
- **Select All CT ≤ 3 mm** adds every eligible thin CT (the limit is in
  Settings → General).
- **Cohorts → Save Selection as Cohort…** keeps a selection to apply later.
- **Selection Settings…** changes how the automatic choice ranks series
  (minimum slices, tolerances, kernel before thickness, DERIVED\PRIMARY CT).
- **Confirm Patient IDs…** appears when studies have no usable patient ID
  (anonymised data): choose the folder level that names the patient. Until
  then those studies are not selected.

## 4. Analysis

**Results & QC → Analyze…**: choose the models and start. Every selected CT
is converted, checked, segmented with each model, measured and prepared for
the viewer. One segmentation runs at a time; the next CT is converted
meanwhile. The digestive and body composition models take several times
longer than the others, because MOOSE predicts them eight times over.

A series that fails stops only itself; the error is shown when you point at
the red mark, and **Retry Failed** queues it again from the step that
failed. If the app quits during a batch, the batch continues at the next
launch.

## 5. Checking the results

Double-click a series in **Results & QC** to open the viewer.

| | mouse or trackpad | keyboard |
|---|---|---|
| move through slices | scroll | ↑ ↓, Page Up/Down |
| window | right-drag (width across, level up and down) | 1–6: Soft Tissue, Lung, Bone, Liver, Brain, Mediastinum |
| zoom | pinch, ⌘-scroll | |
| crosshair | click (moves the other planes) | C shows or hides it |
| overlays | | S all on or off, O fill or contour, [ ] opacity |
| one structure only | ⌥-click it in the list | I |
| QC | buttons | A accept, R reject, F comment, N next, P previous |

The planes follow the radiological convention: the patient's right is on
the screen's left. **Neurological** mirrors axial and coronal; the letters
at the edges follow.

Accept a series, accept it with a comment, or reject it. A single wrong
structure need not cost the series: Control-click it in the list and choose
**Exclude from Export**; its values stay empty and the reason goes into the
qc sheet. **Cohort** shows one structure over all series with outliers
marked from 20 series on.

## 6. Export

**Export**: choose the format (XLSX, CSV for R and Python, CSV for Excel
with European settings), the layout (wide: one row per patient; long: one
row per structure), and how several studies or series per patient are
handled. By default rejected series are left out, patients are named by
pseudonym and dates are days since the first study. The files go into the
project's `exports/` folder, with a methods text to cite. The first export
locks its run: a later change of settings starts a new run, so the numbers
of an export can always be traced to one set of versions.

### Case reports

Turn on **Case report per segmented series (PDF)** in the export, or
Control-click series under **Results & QC** and choose **Create Case
Reports** to make them for those patients alone. Each report is one A4 PDF
per segmented series, in `exports/<name>_reports/`, named
`P0001_t1_s1_case_report.pdf`: the volume and density of every structure
with the catalog's names, the QC status, images of the segmentations, the
definitions, the checks the report ran, the methods text, the versions and
the references with the licence of the MOOSE weights.

A case report is research documentation, not a medical report. It shows
values as measured, without reference values and without any assessment,
and every page says that it is not for clinical use and not a medical
device. It does not belong in the patient record.

- Cases are named by pseudonym only, whatever the identifier setting.
  Dates are days since the first study; the year, or the full date after a
  warning, only when the export chose them.
- A rejected series gets no report. An excluded structure stays empty with
  its reason; an unreviewed series says so on every page.
- The report is not drawn when a structure has no name, or when a mask no
  longer has the voxel count of the export; the message names the reason.
  A model whose left and right labels lie mostly on the wrong side of the
  midline is not shown, and a single label on the wrong side is marked.
- Coronal and sagittal images end below the neck, at the lowest of the
  head region, the C7 vertebra and the top of the lungs, so that no image
  shows a face; without such a landmark there are none. Brain and thyroid
  appear in the tables only.
- **Open in Preview** opens the reports for reading and printing. **Save a
  Copy…** copies them to a folder you choose and warns before iCloud Drive,
  a network volume or a removable disk.
- A report shows the values when it is drawn. After a QC change, create it
  again.

## 7. Removing identifiers

**File → Remove Identifiers…** deletes every patient ID and accession
number, the links between studies and patients and the index with its
folder names. Pseudonyms, results and QC stay. It cannot be undone, and new
studies can no longer be added to the project. Copies of exports outside the
project folder are beyond the app's reach.

## Messages

| message | what to do |
|---|---|
| No air in the image; check rescale slope/intercept | the CT's values are not Hounsfield units; check the export settings of the scanner or PACS |
| The files of the series do not form one volume | the series mixes orientations or sizes; choose another series of the study |
| The index changed; retry after the next scan | a scan rebuilt the index meanwhile; choose Retry Failed |
| Files of the series changed or are gone | scan the source again, then Retry Failed |
| Not connected; its series are kept | the disk or share of a source is not mounted; connect it and choose Scan All |

## Citing and licences

Segmentations come from MOOSE (Shiyam Sundar et al., J Nucl Med 2022;
Ferrara et al., Sci Data 2026), based on nnU-Net (Isensee et al., Nat
Methods 2021). The model weights are licensed under CC BY 4.0 by their
authors (https://creativecommons.org/licenses/by/4.0/); the app ships them
with the optimizer state removed and the network weights unchanged. MOOSE's
code and nnU-Net are under the Apache License 2.0. The app is based on MOOSE
and is neither made nor endorsed by its authors. The export's methods text
states the versions, the hardware and the two corrections the app makes to
MOOSE at run time; each case report ends with the references and the
licences; **About** lists the licence of every component in the app, and
`docs/legal/README.md` says where each notice appears.
