import argparse
import hashlib
from pathlib import Path

from inspect_ai import Task, eval as run_eval, task, task_with
from inspect_ai.scorer import CORRECT, INCORRECT
from inspect_ai.solver import solver
from inspect_ai.util import sandbox, store

from undertest.results import summarize
from undertest.tasks.report_task import FIXTURES, ROOT, report_task


@solver
def fixture_solver(mode: str):
    async def solve(state, generate):
        initial = await sandbox().read_file("/workspace/report.py")
        assert initial == (FIXTURES / "report.py").read_text(), "Sandbox state leaked between runs"
        store().set("initial_sha256", hashlib.sha256(initial.encode()).hexdigest())
        if mode == "fixed":
            await sandbox().write_file(
                "/workspace/report.py", (ROOT / "tests" / "reference_report.py").read_text(),
            )
        elif mode == "changed_tests":
            await sandbox().write_file("/workspace/test_report.py", "print('all tests passed')\n")
        elif mode == "error":
            await sandbox().write_file("/workspace/report.py", initial + "\n# saved before error\n")
            raise RuntimeError("intentional smoke-test failure")
        return state
    return solve


@task
def smoke(mode: str) -> Task:
    result = task_with(report_task(), solver=fixture_solver(mode), name=f"smoke_{mode}")
    result.dataset[0].metadata["mode"] = mode
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if (args.output / "logs").exists():
        parser.error("Choose a fresh output directory for the smoke test.")
    logs = run_eval(
        [smoke(mode) for mode in ("broken", "fixed", "changed_tests", "error")],
        model="mockllm/model", epochs=2, max_tasks=1, max_samples=1,
        log_dir=str(args.output / "logs"), fail_on_error=False,
    )
    count = 0
    for log in logs:
        for sample in log.samples or []:
            count += 1
            mode = sample.metadata["mode"]
            assert sample.store.get("initial_sha256"), "Missing reset check"
            assert sample.store.get("report_py"), "Missing final artifact"
            if mode == "error":
                assert sample.error and "intentional smoke-test failure" in sample.error.message
                assert "saved before error" in sample.store["report_py"]
            else:
                assert sample.error is None, sample.error
                score = next(iter(sample.scores.values()))
                assert score.value == (CORRECT if mode == "fixed" else INCORRECT)
    assert count == 8, f"Expected 8 samples; got {count}"
    rows = summarize(args.output / "logs", args.output)
    assert len(rows) == 8
    assert sum(row["sample_status"] == "error" for row in rows) == 2
    assert all(Path(row["log"]).is_file() for row in rows)
    assert all((Path(row["artifacts"]) / "report.py").is_file() for row in rows)
    assert all("attachment://" not in (Path(row["artifacts"]) / "messages.json").read_text() for row in rows)
    print("Passed: correct/incorrect scoring, independent checks, fresh sandboxes, error capture, artifacts, CSV export.")


if __name__ == "__main__":
    main()
