import argparse
import hashlib
import json
from pathlib import Path

from shared.runs import run_directory
from shared.runs import write_json
import signals.self_reports.verbalization as coding
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import digest

if __package__:
    from signals.reasoning.run_monitor import ROOT
    from signals.reasoning.run_monitor import archive_sources
    from signals.reasoning.run_monitor import file_hash
    from signals.reasoning.run_monitor import load_actor_run
    from signals.reasoning.run_monitor import source_hashes
else:
    from signals.reasoning.run_monitor import ROOT, archive_sources, file_hash, load_actor_run, source_hashes


def read_frozen(path: Path, expected: str):
    coding._hash(expected, "external freeze")
    body = Path(path).read_bytes()
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError(f"External frozen artifact hash mismatch: {path.name}")
    return coding.strict_json(body.decode("utf-8"))


def load_inputs(actor, controls_path):
    control_body = controls_path.read_bytes()
    controls = coding.strict_json(control_body.decode("utf-8"))
    coding._keys(controls, {"schema_version", "controls"}, "constructed controls")
    if type(controls["schema_version"]) is not int or controls["schema_version"] != 1 or not isinstance(controls["controls"], dict):
        raise ValueError("Expected constructed controls v1")
    if any(row.get("split") != "development" for row in actor["assignments"]):
        raise ValueError("Development coding cannot inspect confirmation assignments")
    inputs = {("actor", key): value for key, value in actor["trajectories"].items()}
    for identifier, trajectory in controls["controls"].items():
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Control IDs must be nonempty")
        coding._keys(trajectory, {"initial_context", "tools", "messages", "retrospective_cutoff"}, "control input")
        inputs[("control", identifier)] = trajectory
    return inputs, hashlib.sha256(control_body).hexdigest()


def load_development_actor(directory):
    # Reject a confirmation ledger before reading any of its case/trajectory contents.
    assignments = coding.strict_json((Path(directory) / "assignments.json").read_text())
    if (not isinstance(assignments, list) or not assignments
            or any(not isinstance(row, dict) or row.get("split") != "development" for row in assignments)):
        raise ValueError("Only an explicit nonempty development actor ledger may be opened")
    actor = load_actor_run(directory)
    if actor["assignments"] != assignments:
        raise ValueError("Actor ledger changed while opening the frozen input")
    return actor


def validate_coverage(plan, inputs):
    assigned = {(task["input_kind"], task["input_id"]) for task in plan["assignments"]}
    if assigned != set(inputs):
        raise ValueError("Frozen plan must retain every actor assignment and every constructed control")


def _artifact(receipt_path, entry, name):
    coding._keys(entry, {"path", "sha256"}, name)
    coding._hash(entry["sha256"], name)
    if not isinstance(entry["path"], str) or not entry["path"]:
        raise ValueError(f"Missing {name} evidence path")
    path = Path(entry["path"])
    path = path if path.is_absolute() else receipt_path.parent / path
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != entry["sha256"]:
        raise ValueError(f"{name} evidence artifact changed")
    return body


def validate_execution_receipt(receipt_path, receipt_hash, plan, plan_hash, prepared, records):
    receipt = read_frozen(receipt_path, receipt_hash)
    expected = {
        "schema_version": 1, "plan_sha256": plan_hash,
        "annotation_id": prepared["task"]["annotation_id"], "request_sha256": prepared["task"]["request_sha256"],
        "rubric_sha256": plan["rubric_sha256"], "prompt_sha256": plan["prompt_sha256"],
        "source_sha256": plan["source_sha256"], "actor_snapshot_sha256": digest(plan["actor_snapshot"]),
        "controls_sha256": plan["controls_sha256"], "model": plan["model"], "model_revision": plan["model_revision"],
        "service_identity_sha256": plan["service_identity_sha256"], "generation_sha256": digest(plan["generation"]),
        "allocation_sha256": plan["allocation_sha256"],
    }
    coding._keys(receipt, set(expected) | {"base_url", "evidence"}, "execution receipt")
    if any(receipt[key] != value or type(receipt[key]) is not type(value) for key, value in expected.items()):
        raise ValueError("Execution receipt does not bind this exact frozen coding task")
    evidence = receipt["evidence"]
    coding._keys(evidence, {"allocation", "readiness_review", "context_preflight", "control_disposition", "resource_checkpoint"}, "execution evidence")
    if not isinstance(evidence["allocation"], dict) or evidence["allocation"].get("sha256") != plan["allocation_sha256"]:
        raise ValueError("Execution allocation differs from the frozen plan")
    artifacts = {}
    for name, entry in evidence.items():
        if entry is not None:
            artifacts[name] = _artifact(receipt_path, entry, name)
    if not {"allocation", "readiness_review", "context_preflight", "resource_checkpoint"} <= artifacts.keys():
        raise ValueError("Allocation, readiness, actual context, and resource checkpoint evidence are required")
    context = coding.strict_json(artifacts["context_preflight"].decode("utf-8"))
    coding._keys(context, {"request_sha256", "model", "model_revision", "service_identity_sha256", "tokenizer_sha256",
                          "chat_template_sha256", "prompt_tokens", "max_model_len", "max_completion_tokens", "truncated"}, "context preflight")
    for key in ("request_sha256", "model", "model_revision", "service_identity_sha256"):
        if context[key] != receipt[key]:
            raise ValueError("Context preflight identity differs from this request")
    for key in ("tokenizer_sha256", "chat_template_sha256"):
        coding._hash(context[key], key)
    if (type(context["prompt_tokens"]) is not int or context["prompt_tokens"] <= 0
            or type(context["max_model_len"]) is not int
            or type(context["max_completion_tokens"]) is not int or context["max_completion_tokens"] != 2048
            or context["prompt_tokens"] + 2048 > context["max_model_len"] or context["truncated"] is not False):
        raise ValueError("Actual full context must fit with the frozen completion cap, without truncation")
    control_records = {task["annotation_id"]: records[task["annotation_id"]]
                       for task in plan["assignments"] if task["input_kind"] == "control"}
    if prepared["task"]["input_kind"] == "actor":
        if any(record["status"] == "pending" for record in control_records.values()):
            raise ValueError("Every assigned constructed control must be terminal before actor transcript coding")
        if "control_disposition" not in artifacts:
            raise ValueError("Transcript execution needs an external reviewed control disposition")
        disposition = coding.strict_json(artifacts["control_disposition"].decode("utf-8"))
        if (disposition.get("plan_sha256") != plan_hash
                or disposition.get("control_results_sha256") != digest(control_records)
                or disposition.get("decision") != "continue"):
            raise ValueError("Control disposition does not authorize continuing from these exact retained results")
        coding._hash(disposition.get("review_sha256"), "control disposition review")
    # Evidence contents and scheduling decisions are reviewed externally; hashes alone are not scientific validation.
    return receipt, artifacts


def _index(plan, records):
    return [{"annotation_id": task["annotation_id"], "input_kind": task["input_kind"],
             "input_id": task["input_id"], "coder_id": task["coder_id"], "group": task["group"],
             "status": records[task["annotation_id"]]["status"],
             "episode_status": records[task["annotation_id"]]["episode_status"]}
            for task in plan["assignments"]]


def _validate_existing_index(output, plan, ledger):
    path = output / "annotation_index.json"
    if not path.exists():
        return
    rows = coding.strict_json(path.read_text())
    tasks = {task["annotation_id"]: task for task in plan["assignments"]}
    if not isinstance(rows, list) or len(rows) != len(tasks) or {row.get("annotation_id") for row in rows} != set(tasks):
        raise ValueError("Existing annotation index does not preserve the complete frozen ledger")
    for row in rows:
        task = tasks[row["annotation_id"]]
        if any(row.get(key) != task[key] for key in ("annotation_id", "input_kind", "input_id", "coder_id", "group")):
            raise ValueError("Existing annotation index assignment identity changed")
        if row.get("status") not in {"pending", "unavailable", "completed", "error", "interrupted"}:
            raise ValueError("Existing annotation index has an invalid state")
        if row["status"] in {"completed", "error", "interrupted"} and ledger.value["consumed"][row["annotation_id"]] is None:
            raise ValueError("Existing consumed index entry lost its durable registration")


def main(argv=None, *, injected_endpoint_factory=None):
    parser = argparse.ArgumentParser(description="Export or execute one externally frozen machine-verbalization assignment")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--controls", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-task")
    parser.add_argument("--execution-receipt", type=Path)
    parser.add_argument("--execution-receipt-sha256")
    args = parser.parse_args(argv)
    if Path(coding.__file__).resolve().parents[3] != ROOT:
        parser.error("Use this worktree's Python environment")
    if bool(args.execute_task) != bool(args.execution_receipt) or bool(args.execute_task) != bool(args.execution_receipt_sha256):
        parser.error("Execute one explicit task only with its externally frozen execution receipt and hash")
    try:
        plan = read_frozen(args.plan, args.plan_sha256)
        rubric = args.rubric.read_bytes().decode("utf-8")
        actor = load_development_actor(args.input)
        output = args.output.resolve()
        if output == actor["directory"] or output.is_relative_to(actor["directory"]):
            raise ValueError("Coding outputs must be separate from immutable actor inputs")
        inputs, controls_hash = load_inputs(actor, args.controls)
        sources = source_hashes()
        coding.validate_plan(plan, actor_snapshot=actor["snapshot"], controls_sha256=controls_hash, rubric=rubric, sources=sources)
        validate_coverage(plan, inputs)
        prepared = {task["annotation_id"]: coding.prepare_assignment(task, plan, inputs, rubric) for task in plan["assignments"]}
        if args.execute_task and args.execute_task not in prepared:
            raise ValueError("Execution task is not assigned in the frozen plan")
        config = {"section": "signals", "run_type": "verbalization", "model": plan["model"],
                  "model_revision": plan["model_revision"], "plan_sha256": args.plan_sha256,
                  "actor_snapshot": actor["snapshot"], "source_sha256": sources, "prompt_sha256": plan["prompt_sha256"],
                  "rubric_sha256": plan["rubric_sha256"], "max_calls": 72, "max_completion_tokens": 147456,
                  "max_completion_tokens_per_call": 2048, "concurrency": 1, "implicit_retries": 0}
        create_consumption_ledger = not (output / "manifest.json").exists()
        with run_directory(output, config) as manifest:
            ledger = coding.ConsumptionLedger(output, prepared, args.plan_sha256, manifest["run_id"],
                                              create=create_consumption_ledger)
            _validate_existing_index(output, plan, ledger)
            coding.persist_once(output / "frozen_plan.json", plan)
            archive_sources(output, sources)
            rubric_path = output / "rubric.txt"
            if rubric_path.exists() and rubric_path.read_bytes() != rubric.encode("utf-8"):
                raise ValueError("Saved rubric bytes changed")
            rubric_path.write_bytes(rubric.encode("utf-8"))
            (output / "previews").mkdir(exist_ok=True)
            records = {}
            for identifier, item in prepared.items():
                coding.persist_once(output / "previews" / f"{identifier}.json", item)
                records[identifier] = coding.run_assignment(prepared=item, plan_sha256=args.plan_sha256,
                    run_id=manifest["run_id"], output=output / "annotations" / identifier, ledger=ledger)
            write_json(output / "annotation_index.json", _index(plan, records))
            if args.execute_task and records[args.execute_task]["status"] == "pending":
                selected = prepared[args.execute_task]
                receipt, artifacts = validate_execution_receipt(args.execution_receipt,
                    args.execution_receipt_sha256, plan, args.plan_sha256, selected, records)
                endpoint = (injected_endpoint_factory or ChatEndpoint)(receipt["base_url"])
                records[args.execute_task] = coding.run_assignment(prepared=selected, plan_sha256=args.plan_sha256,
                    run_id=manifest["run_id"], output=output / "annotations" / args.execute_task,
                    ledger=ledger, execute=True, call=endpoint, execution_receipt=receipt,
                    execution_receipt_bytes=args.execution_receipt.read_bytes(),
                    execution_receipt_file_sha256=args.execution_receipt_sha256, execution_artifacts=artifacts,
                    call_kind="injected_test" if injected_endpoint_factory is not None else "chat_endpoint")
                write_json(output / "annotation_index.json", _index(plan, records))
            index = _index(plan, records)
            print(json.dumps({"run_id": manifest["run_id"], "n_assignments": len(index),
                "executed_task": args.execute_task,
                "statuses": {status: sum(row["status"] == status for row in index) for status in sorted({row["status"] for row in index})},
                "output": str(output)}), flush=True)
    except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
