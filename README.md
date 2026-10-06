# undertest

Experiments on metagaming in language models: what causes it, how it behaves, which signals reveal it, and how it develops during training.

The source tree follows those four research questions. Study code, runnable entry points, analysis and protocol configuration live together. Common task, run and result utilities live in `src/shared/`.

```text
src/
  shared/             task, run and result utilities
  causes/             cues, task contexts, visible history, labels and controls
  characteristics/    resampling, coherence, continuations and downstream effects
  signals/            reasoning, self-reports, behavior and validation
  emergence/          released checkpoints and controlled training methods
  studies.json        implemented studies and their current scope
data/                 report task inputs, synthetic fixtures and source conventions
tests/                checks organized by the same research areas
docs/                 overview, methods, reproduction and data documentation
```

Start with the [project overview](docs/overview.md), [methods](docs/methods.md), [reproduction instructions](docs/reproduction.md), and [data guide](docs/data.md). The [study catalogue](src/studies.json) distinguishes runnable studies, saved-frame analysis and extracted calculation kernels.

With Python 3.11 or later and uv installed:

```bash
uv sync --locked
uv run python -m unittest discover -s tests -t .
uv run python -m shared.report_control.eval_cue --dry-run --output /tmp/undertest-report-preview
```

Live report tasks also require Docker and a configured model provider. Saved-data analyses and synthetic tests do not require model calls. Provide explicit input and output locations; retain original records, protocol versions and missing outcomes when comparing studies.
