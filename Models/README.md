# Models

Not versioned: the weights are fetched at build time by
`Scripts/fetch_models.py` into `build/models`. Only `manifest.lock.json` is
committed. It records, per model, the release URL from the pinned moosez and
the SHA-256 of the archive; a build whose download differs stops.

Measured on 2 October 2026 with `clin_ct_organs` (moosez 3.2.2):

| stage | size |
|---|---|
| release archive | 459 MB |
| unpacked as MOOSE would keep it | 475 MiB |
| without `checkpoint_best.pth`, validation output, logs | 237 MiB |
| plus optimizer state removed from `checkpoint_final.pth` | **118 MiB** (122.7 MB) |

The unpacked sizes were measured with `du`, in binary units; `docs/benchmarks.md`
gives every model in decimal MB.

Weights are licensed CC BY 4.0 by the MOOSE authors; the changes are listed
in THIRD_PARTY_NOTICES and in `build/models/manifest.json`.
