# undertest

Experiments on evaluation and simulation awareness in language models. We want to understand which cues models respond to, how this changes during training, and when it affects their behavior.

Starting with a working task, change one factor, and repeat the run while keeping everything else fixed.

Using:
Python and [Inspect](https://inspect.aisi.org.uk/) for model access, task execution, repeated runs, and logs. Each experiment will be a short Python script with settings set.

```text
undertest/
  pyproject.toml          # dependencies
  uv.lock                # pinned dependency versions
  src/undertest/tasks/    # task definitions, variants, and success checks
  data/                  # fixed task inputs and Docker files
  experiments/           # scripts to run comparisons and summarize results
  results/               # local logs, output artifacts, and result tables
```

Setup will need Python with `uv`, Inspect, and the packages for the model providers we use. Repository tasks also need Docker.
