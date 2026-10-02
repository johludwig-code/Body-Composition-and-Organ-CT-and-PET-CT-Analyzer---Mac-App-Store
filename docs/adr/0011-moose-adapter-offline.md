# ADR 0011: How the adapter keeps MOOSE offline and out of the bundle's way

- Status: accepted. Confirmed by a real offline run on CPU (docs/spikes/M0.md); the MPS run on a Mac is outstanding
- Date: 2026-10-02
- Plan section: §4 "MOOSE-Adapter", spike S1

## What moosez 3.2.2 does (read from the wheel, 2 October 2026)

| question from S1 | answer | where |
|---|---|---|
| Where does it look for models? | `<site-packages>/moosez/models/nnunet_trained_models/<Dataset folder>` — **inside the package**, i.e. inside the signed bundle | `system.MODELS_DIRECTORY_PATH` |
| When does it download? | In `Model.__init__`, every time, unless the folder exists **and** holds `model_version.json` whose `url` equals the URL in `MODEL_METADATA`. A folder without that file or with another URL is **deleted** (`shutil.rmtree`) and fetched again. | `models.Model.__download` |
| Can the path be changed? | Only through `Model(…, base_directory=…)`. The default is bound when the module is imported, so changing `system.MODELS_DIRECTORY_PATH` afterwards has no effect, and `Workflow` never passes the argument. | `models.py:120`, `workflows.py` |
| Other network access | `requests.get` in `models.py` (weights) and `download.py` (training data, CLI only). nnU-Net's inference path has none. | |
| Other writes into site-packages | `add_custom_trainers_to_local_nnunetv2()` **copies a file into nnunetv2** on every `moose()` call unless it is already there. In a signed bundle this is a write into the code signature. | `nnUNet_custom_trainer/utility.py` |
| Multi-step models | `clin_ct_body_composition` first runs `clin_ct_fast_vertebrae` to crop the field of view; `clin_ct_face` first runs `clin_ct_body`. Bundling a model means bundling its dependencies. | `workflows.WORKFLOW_REGISTRY` |
| Library API | `moose(input, model_names, output_dir, accelerator)`. Only a **path** input is reoriented to RAS and the result back to the input's orientation; a `SimpleITK.Image` input is used as given. | `moosez.py:356` |
| Output name | `<output_dir>/clin_<MODALITY>_<region>_segmentation_<stem>.nii.gz` — modality in capitals, e.g. `clin_CT_organs_segmentation_ct.nii.gz` | `Model.multilabel_prefix` |
| Output space | Same grid as the input (BOCARTA-MOOSE measured size, spacing and origin identical) | `docs/moose-phase0.md` there |
| Label table | `dataset.json` → `labels` of the model folder; `Model.organ_indices` | |
| Checkpoints | `torch.load(..., weights_only=False)` — pickle. nnU-Net reads only `trainer_name`, `init_args.configuration`, `inference_allowed_mirroring_axes`, `network_weights`. | nnunetv2 2.8.1 `predict_from_raw_data.py:87–95` |
| Environment | sets `nnUNet_raw/preprocessed/results` to `""` at import; needs a writable `MPLCONFIGDIR` (matplotlib is imported) | `system.py:266` |

## Decision

All of it is handled in `bcoa_worker/moose_adapter.py`, without touching MOOSE:

1. **Model path.** Before any MOOSE call the adapter replaces the default of
   `models.Model.__init__`'s `base_directory` with the bundle's
   `Resources/models/nnunet_trained_models`. MOOSE's own package folder is
   never used, so no model can hide there.
2. **No download, ever.** `models.requests` and `download.requests` are replaced
   by a stub whose every call raises `ModelNotInBundleError("Model not found in
   app bundle: …")`. The sandbox would refuse the connection anyway; this turns
   a hang or an obscure `ConnectionError` into the message the plan asks for.
3. **No deletion.** `make models` writes `model_version.json` with exactly the
   URL from `MODEL_METADATA` of the pinned version. The adapter checks it before
   MOOSE sees the folder, and refuses to run if it differs — MOOSE would
   otherwise try to delete a read-only folder and re-download.
4. **No write into nnU-Net.** The build copies `MOOSE_custom_trainers.py` into
   `nnunetv2/training/nnUNetTrainer/variants/` before signing, so MOOSE's copy
   returns "already installed". The adapter additionally replaces
   `add_custom_trainers_to_local_nnunetv2` in the `moosez.moosez` namespace with
   a check that only verifies the file is present.
5. **Dependencies.** `fetch_models.py` expands `WORKFLOW_REGISTRY` so the
   manifest lists `clin_ct_fast_vertebrae` whenever `clin_ct_body_composition`
   is chosen.
6. **One model per call**, input as a NIfTI **path**, output into a per-model
   temporary folder and moved to `work/<series>/labels/<model>.nii.gz`.

## Consequences

The adapter depends on private details of moosez 3.2.2 (`Model.__init__`
signature, module-level `requests` import). `test_moose_adapter.py` holds each
assumption against a fake moosez with the same shape, and the adapter refuses
any other moosez version, so an upgrade breaks loudly in the build rather than
quietly at a user's desk.
