# ADR 0015: nnU-Net's export runs in the worker, not in a process pool

- Status: accepted
- Date: 2026-10-02
- Plan section: §5 (worker), spike S1 and S2

## Context

The first run of the signed, sandboxed worker on Apple Silicon (bundle.yml,
macos-15 runner, commit 479ee73) got through S2 — sandbox inherited, project
writable, an ungranted folder refused, MPS available — and then failed the
self-test inference with `PermissionError: [Errno 1] Operation not permitted`.

The traceback ends in `_multiprocessing.SemLock`. MOOSE calls nnU-Net's
`nnUNetPredictor.predict_from_data_iterator`, which opens
`multiprocessing.get_context("spawn").Pool(8)` to resample each predicted
chunk back to the input grid in the background. A pool needs POSIX
semaphores, and the App Sandbox refuses `sem_open` for any name that does not
begin with one of the app's application-group identifiers. Python names its
semaphores `/mp-<random>`, so every pool, queue and lock from
`multiprocessing` fails inside the sandbox. The same worker outside the
sandbox, on the same runner, ran the inference on MPS without error.

Nothing else on the inference path uses `multiprocessing`: MOOSE's
preprocessing runs on dask's threaded scheduler, and its process pools
(`moosez.py`, `image_conversion.py`) are only used for several subjects at
once or for DICOM input, neither of which the adapter calls.

## Decision

The adapter replaces the `multiprocessing` name inside
`nnunetv2.inference.predict_from_raw_data` with a stand-in that hands every
other attribute to the real module but gives `get_context()` a pool that runs
each task at once, in the calling thread, and returns an already finished
result. nnU-Net's own loop is unchanged: it predicts a chunk, exports it,
predicts the next. Neither MOOSE nor nnU-Net is modified on disk; the patch is
made in `moose_adapter.harden()` like the others in ADR 0011.

The next run's sandbox report (bundle.yml lists every refusal of the probe
and of python3) showed one more: joblib, imported through scikit-learn,
creates a semaphore at import to find out whether it may use processes. It
survives the refusal and stays serial, with a warning. The app and the worker
now both set `JOBLIB_MULTIPROCESSING=0`, so it no longer tries, and a test
holds the app's and the worker's environment to the same variables. The
run after that showed the last one: tqdm, which draws nnU-Net's progress bars,
asks for a multiprocessing lock when the first bar is made and goes on with
its thread lock alone when refused. The adapter sets that outcome beforehand
(`TqdmDefaultWriteLock.mp_lock = None`). A trace on Linux with every
`SemLock` refused, through MOOSE and nnU-Net on the organ model, found no
further request. The report step fails on any refused semaphore from now on.

The export of a chunk now waits for the chunk's prediction and the next
prediction waits for the export. What that costs in wall time is measured on
the runner and recorded in `docs/benchmarks.md`.

## Consequences

- The worker starts no further processes, so "no processes after quit"
  (App Store 2.4.5(iii)) depends on the worker alone, which the app already
  terminates.
- Peak memory is lower, not higher: no copy of each chunk's logits is pickled
  to a second process.
- Results are the same function applied to the same arrays. Bit-identity with
  the pool version is checked on the phantom on Linux.
- The fake site in `test_moose_adapter.py` has a predictor module of the same
  shape, and the tests hold that the stand-in never creates a semaphore.

## Rejected alternatives

- **Semaphores named under an app group.** Python takes the prefix from
  `current_process()._config["semprefix"]`, and an app group entitlement
  would allow `<TeamID>.<group>/…`. It needs the Team ID, which does not
  exist yet (OPEN_QUESTIONS #3), a private attribute of `multiprocessing`,
  names within macOS's 31-character limit, and eight spawned interpreters
  inside the sandbox, each one more process to account for at quit.
- **A thread pool.** `multiprocessing.pool.ThreadPool` still creates a
  `multiprocessing.SimpleQueue` in `Pool.__init__`, which takes a semaphore.
  `concurrent.futures` threads would work, but nnU-Net's export calls
  `torch.set_num_threads` and restores it afterwards; done in a second thread
  that would change the thread count of the prediction running at the same
  time, and with it possibly the order of floating-point sums on the CPU.
- **Copying `predict_from_data_iterator` into the adapter.** More code to keep
  in step with nnU-Net for the same effect.
