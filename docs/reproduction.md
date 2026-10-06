# Reproduction

Install Python 3.11 or later, then run `uv sync --locked` from a checkout. The distribution provides the `shared`, `causes`, `characteristics`, `signals` and `emergence` Python packages directly. Optional Parquet metadata projection uses `uv sync --locked --extra metadata`. GPU tensor code additionally requires a separately qualified PyTorch environment.

Run local synthetic and saved-contract checks with:

```bash
uv run python -m unittest discover -s tests -t .
```

Some inherited integration cases require explicitly supplied fixtures or optional dependencies and report skips when unavailable. The fresh-rule registry tests accept `TRAINDEV_FRESH_V2_DIRECTORY` as an explicit catalogue directory; they do not search the machine for inputs. The four original catalogue files must match the hashes declared by the registry reader.

Inspect entry points with `python -m <module> --help`. Useful starting points are `shared.report_control.eval_cue`, `emergence.checkpoints.calibration.prepare_panel`, `emergence.checkpoints.calibration.run_panel`, `signals.validation.controls.run`, `signals.reasoning.run_monitor`, `signals.validation.controls.analyze`, `signals.self_reports.run`, `characteristics.continuations.run`, and `emergence.checkpoints.run_probe_battery`.

A report-task dry run freezes configuration and source identities without calling a model:

```bash
uv run python -m shared.report_control.eval_cue --dry-run --output /tmp/undertest-report-preview
```

Use a fresh explicit output directory for an independent run. Exact-input comparisons reject changed manifests. Model execution requires a selected model, provider configuration and each interface's declared protocol inputs; report-task execution also requires Docker. Recipe/control execution defaults to control-host policy; set `UNDERTEST_CONTROL_HOST=0` only in the intended execution environment. Offline tests use mocked model transport.

The broad-probe worker requires explicit `--input`, `--output`, `--prompt`, `--schema`, `--model` and `--label-version`; its default mode is `dry-run`. `mock` accepts a scripted response file. `live` is explicit. It records durable request reservations, each attempt and terminal missingness, and does not silently replay interrupted requests. Original schemas are validated locally even when the wire schema is adapted for the API.

Saved-analysis kernels accept already authenticated records or explicit byte/pin descriptors. Importing a calculation does not authenticate an evidence bundle. Preserve original source snapshots when reproducing historical results; the calibration reader recognizes both historical and current source-member layouts without declaring their hashes equivalent. Linux/POSIX file and locking primitives are required by the metadata pin reader and durable labeling worker.
