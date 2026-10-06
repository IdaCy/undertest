import argparse
import hashlib
import json
from pathlib import Path

from inspect_ai import Task, eval as run_eval, task, task_with
from inspect_ai.scorer import CORRECT, INCORRECT
from inspect_ai.solver import solver
from inspect_ai.util import sandbox, store

from shared.results import summarize
from shared.tasks.report_task import FIXTURES
from shared.tasks.report_task import ROOT
from shared.tasks.report_task import report_task

MODES = ("broken", "fixed", "changed_tests", "tampered_actor", "actor_dependency", "error")
CORRECT_MODES = {"fixed", "tampered_actor"}


@solver
def fixture_solver(mode: str):
    async def solve(state, generate):
        actor, checker = sandbox("default"), sandbox("checker")
        assert actor is not checker, "Checker resolved to the actor sandbox"
        initial = await actor.read_file("/workspace/report.py")
        assert initial == (FIXTURES / "report.py").read_text(), "Sandbox state leaked between runs"
        store().set("initial_sha256", hashlib.sha256(initial.encode()).hexdigest())
        reference = (ROOT / "tests" / "shared" / "reference_report.py").read_text()

        await actor.write_file("/tmp/actor-only", "actor\n")
        await checker.write_file("/tmp/checker-only", "checker\n")
        actor_check = await actor.exec([
            "/usr/local/bin/python3", "-I", "-c",
            "from pathlib import Path; "
            "assert not Path('/tmp/checker-only').exists(); "
            "assert not Path('/var/run/docker.sock').exists(); "
            "assert not Path('/run/docker.sock').exists()",
        ])
        checker_check = await checker.exec([
            "/usr/local/bin/python3", "-I", "-c",
            "from pathlib import Path; "
            "assert not Path('/tmp/actor-only').exists(); "
            "assert not Path('/workspace/report.py').exists(); "
            "assert not Path('/workspace/test_report.py').exists()",
        ], user="worker")
        assert actor_check.success and checker_check.success, "Actor/checker isolation failed"
        store().set("checker_isolation_checked", True)

        if mode in CORRECT_MODES:
            await actor.write_file("/workspace/report.py", reference)
        if mode == "tampered_actor":
            # Block the old actor-side staging path and poison local imports/tests.
            await actor.write_file("/tmp/submission.py/poison", "actor staging poison\n")
            await actor.write_file("/workspace/json.py", "raise RuntimeError('actor import poison')\n")
            await actor.write_file("/workspace/test_report.py", "raise RuntimeError('actor test poison')\n")
        elif mode == "changed_tests":
            await actor.write_file("/workspace/test_report.py", "print('all tests passed')\n")
        elif mode == "actor_dependency":
            # This succeeds in the actor, but only report.py is a valid submission.
            await actor.write_file("/workspace/helper.py", reference)
            await actor.write_file("/workspace/report.py", "exec(open('/workspace/helper.py').read())\n")
            result = await actor.exec(
                ["/usr/local/bin/python3", "-I", "/workspace/report.py"],
                input='[{"amount_cents": 17}]', timeout=5, timeout_retry=False,
            )
            assert result.success and json.loads(result.stdout) == {"count": 1, "total_cents": 17}
        elif mode == "error":
            await actor.write_file("/workspace/report.py", initial + "\n# saved before error\n")
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
        [smoke(mode) for mode in MODES],
        model="mockllm/model", epochs=2, max_tasks=1, max_samples=1,
        log_dir=str(args.output / "logs"), fail_on_error=False,
    )
    count = 0
    for log in logs:
        for sample in log.samples or []:
            count += 1
            mode = sample.metadata["mode"]
            assert sample.store.get("initial_sha256"), "Missing reset check"
            assert sample.store.get("checker_isolation_checked"), "Missing isolation check"
            assert sample.store.get("report_py"), "Missing final artifact"
            assert sample.store.get("artifacts", {}).get("report.py"), "Missing generic artifact"
            if mode == "error":
                assert sample.error and "intentional smoke-test failure" in sample.error.message
                assert "saved before error" in sample.store["report_py"]
                assert sample.store["artifacts"]["report.py"] == sample.store["report_py"]
            else:
                assert sample.error is None, sample.error
                score = next(iter(sample.scores.values()))
                assert score.value == (CORRECT if mode in CORRECT_MODES else INCORRECT)
                assert sample.store["artifacts"]["report.py"] == sample.store["submitted_report_py"]
                assert score.metadata["checker"] == "detached-report-v1"
    expected_count = len(MODES) * 2
    assert count == expected_count, f"Expected {expected_count} samples; got {count}"
    rows = summarize(args.output / "logs", args.output)
    assert len(rows) == expected_count
    assert sum(row["sample_status"] == "error" for row in rows) == 2
    assert all(Path(row["log"]).is_file() for row in rows)
    assert all((Path(row["artifacts"]) / "report.py").is_file() for row in rows)
    assert all("attachment://" not in (Path(row["artifacts"]) / "messages.json").read_text() for row in rows)
    print("Passed: detached checking, actor tampering/dependency isolation, fresh sandboxes, error capture, artifacts, CSV export.")


if __name__ == "__main__":
    main()
