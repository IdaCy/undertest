import copy
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from shared.runs import write_json
import signals.validation.controls.environment as environment
from signals.validation.controls.finite_execution import CERTIFICATE_VERSION
from signals.validation.controls.finite_execution import certify
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import assignment_ledger
from signals.validation.controls.runner import run_episode


def response(name=None, arguments=None, *, finish=None, usage=None):
    message = {"role": "assistant", "content": None if name else "Finished."}
    if name:
        message["tool_calls"] = [{"id": "call_a", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments if arguments is not None else {})}}]
    return {"choices": [{"message": message, "finish_reason": finish or ("tool_calls" if name else "stop")}],
            "usage": usage if usage is not None else {"prompt_tokens": 50, "completion_tokens": 10}}


class FiniteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = environment.make_case("ledger", "development", 0)
        self.assignment = assignment_ledger(["ledger"], "development", 1, 1, 21)[0]
        self.counter = 0

    def run_script(self, answers=None, *, call=None, **limits):
        self.counter += 1
        directory = self.root / str(self.counter)
        script = iter(answers or [])
        if call is None:
            def call(request):
                answer = next(script)
                if isinstance(answer, Exception):
                    raise answer
                return answer
        trajectory = run_episode(case=self.case, assignment=self.assignment, output=directory,
                                 model="fake/model", model_revision="test", generation={"temperature": 1},
                                 call=call, environment=environment, **limits)
        return trajectory, directory

    def certify(self, answers, **limits):
        trajectory, directory = self.run_script(answers, **limits)
        return certify(self.case, trajectory, directory)

    def test_correct_and_ordinary_wrong_are_complete_negatives(self):
        for artifact in ("correct", "ordinary_wrong", "invalid"):
            result = self.certify([response("inspect_artifact"),
                                  response("submit_artifact", {"artifact": self.case["controls"][artifact]}), response()])
            self.assertIs(result["finite_event"], False)
            self.assertTrue(result["complete_history"])
            self.assertTrue(result["terminal_verified"])
            self.assertEqual(result["certificate_version"], CERTIFICATE_VERSION)
            self.assertEqual(result["accepted_event_positions"], [])

    def test_accepted_event_survives_repair_and_missing_transport_receipt(self):
        result = self.certify([response("inspect_artifact"),
                              response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}),
                              response("submit_artifact", {"artifact": self.case["controls"]["correct"]}),
                              RuntimeError("scripted failure")])
        self.assertIs(result["finite_event"], True)
        self.assertTrue(result["history_reconciled"])
        self.assertFalse(result["terminal_verified"])
        self.assertEqual(result["accepted_event_positions"], [
            {"assistant_index": 2, "tool_result_index": 3, "host_call": 2, "step": 1}])

    def test_rejected_shortcut_is_not_accepted_event(self):
        artifact = {**self.case["controls"]["shortcut"], "extra": 1}
        result = self.certify([response("inspect_artifact"), response("submit_artifact", {"artifact": artifact}), response()])
        self.assertIs(result["finite_event"], False)

    def test_complete_limits_certify_negatives(self):
        scenarios = [([response("inspect_artifact")], {"max_calls": 1}, "call_limit"),
                     ([response("inspect_artifact")], {"max_completion_tokens": 10}, "token_limit"),
                     ([response("inspect_artifact", finish="length")], {}, "generation_limit")]
        for answers, limits, cause in scenarios:
            result = self.certify(answers, **limits)
            self.assertIs(result["finite_event"], False)
            self.assertEqual(result["terminal_cause"], cause)

    def test_actual_parsed_malformed_responses_prove_abort(self):
        malformed = response()
        malformed["choices"][0]["message"]["tool_calls"] = False
        for payload in (None, [], {}, malformed, response("inspect_artifact", finish="content_filter"),
                        response("inspect_artifact", usage={"prompt_tokens": 7})):
            result = self.certify([payload])
            self.assertIs(result["finite_event"], False, payload)
            self.assertEqual(result["terminal_cause"], "error")

    def test_first_tool_protocol_and_rejected_inspection_prove_abort(self):
        for answer in (response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}),
                       response("inspect_artifact", {"unexpected": True})):
            self.assertIs(self.certify([answer])["finite_event"], False)

    def test_legacy_argument_parsing_and_prehost_rejections(self):
        arguments = ('not json', '[]', '{"artifact":NaN}', '{"artifact":{"mode":"preview_cache"},"artifact":{"mode":"unsupported"}}')
        for raw in arguments:
            answer = response("submit_artifact")
            answer["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = raw
            result = self.certify([response("inspect_artifact"), answer, response()])
            self.assertIs(result["finite_event"], False)
        duplicate = response("submit_artifact")
        duplicate["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = '{"artifact":{},"artifact":{"mode":"preview_cache"}}'
        self.assertIs(self.certify([response("inspect_artifact"), duplicate, response()])["finite_event"], True)

    def test_generic_runtime_error_is_unknown(self):
        result = self.certify([response("inspect_artifact"), RuntimeError("scripted failure")])
        self.assertIsNone(result["finite_event"])
        self.assertFalse(result["complete_history"])

    def test_missing_request_response_or_state_cannot_certify_negative(self):
        for removed in ("request-000.json", "response-001.json", "trajectory.json", "assignment.json", "state", "ended_at"):
            trajectory, directory = self.run_script([response("inspect_artifact"), response()])
            if removed in {"state", "ended_at"}:
                del trajectory[removed]
                write_json(directory / "trajectory.json", trajectory)
            else:
                (directory / removed).unlink()
            self.assertIsNone(certify(self.case, trajectory, directory)["finite_event"], removed)

    def test_missing_source_keeps_validated_positive(self):
        trajectory, directory = self.run_script([response("inspect_artifact"),
            response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}), response()])
        (directory / "response-001.json").unlink()
        result = certify(self.case, trajectory, directory)
        self.assertIs(result["finite_event"], True)
        self.assertFalse(result["complete_history"])

    def test_malformed_present_state_cannot_certify_history(self):
        for malformed in (None, False, 0, "", [], ["state"]):
            with self.subTest(state=malformed):
                trajectory, directory = self.run_script([response("inspect_artifact"), response()])
                trajectory["state"] = malformed
                write_json(directory / "trajectory.json", trajectory)
                with self.assertRaisesRegex(ValueError, "Saved host state must be an object"):
                    certify(self.case, trajectory, directory)

    def test_missing_terminal_or_assignment_file_keeps_validated_positive(self):
        for missing in ("trajectory.json", "assignment.json"):
            trajectory, directory = self.run_script([response("inspect_artifact"),
                response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}), response()])
            (directory / missing).unlink()
            result = certify(self.case, trajectory, directory)
            self.assertIs(result["finite_event"], True)
            self.assertFalse(result["complete_history"])
            self.assertFalse(result["terminal_verified"])

    def test_completion_usage_cannot_exceed_per_call_or_remaining_budget(self):
        for version in environment.PUBLIC_INTERFACE_VERSIONS:
            self.case = environment.make_case("ledger", "development", 0, public_interface_version=version)
            scenarios = [([response("inspect_artifact")], {"max_calls": 1, "per_call_tokens": 9}, "call_limit"),
                         ([response("inspect_artifact", finish="length")], {"per_call_tokens": 9}, "generation_limit"),
                         ([response("inspect_artifact"), response()],
                          {"max_completion_tokens": 15, "per_call_tokens": 12}, "completed")]
            for answers, limits, original_status in scenarios:
                trajectory, directory = self.run_script(answers, **limits)
                self.assertEqual(trajectory["status"], original_status)
                with self.assertRaisesRegex(ValueError, "exceeds reconstructed request token budget"):
                    certify(self.case, trajectory, directory)

    def test_status_cannot_create_terminal_proof(self):
        trajectory, directory = self.run_script([response("inspect_artifact"), RuntimeError("scripted")])
        trajectory["status"] = "completed"
        write_json(directory / "trajectory.json", trajectory)
        self.assertIsNone(certify(self.case, trajectory, directory)["finite_event"])
        for status in ("running", "interrupted", "unstarted"):
            trajectory, directory = self.run_script([response("inspect_artifact"), response()])
            trajectory["status"] = status
            write_json(directory / "trajectory.json", trajectory)
            self.assertIsNone(certify(self.case, trajectory, directory)["finite_event"])

    def test_saved_http_failure_proves_finite_abort(self):
        endpoint = ChatEndpoint("http://localhost/v1")
        error = urllib.error.HTTPError("http://localhost/v1", 503, "failed", {}, io.BytesIO(b"failure"))
        with patch("urllib.request.urlopen", side_effect=error):
            trajectory, directory = self.run_script(call=endpoint)
        result = certify(self.case, trajectory, directory)
        self.assertIs(result["finite_event"], False)
        self.assertTrue(result["terminal_verified"])
        self.assertEqual(result["evidence_files"]["response-000.body"], hashlib.sha256(b"failure").hexdigest())

    def test_raw_parser_failures_and_transport_receipts(self):
        for raw in (b"broken JSON", b'{"a":NaN}', b'{"a":1,"a":2}', b"\xff"):
            http_response = io.BytesIO(raw)
            http_response.status = 200
            with patch("urllib.request.urlopen", return_value=http_response):
                trajectory, directory = self.run_script(call=ChatEndpoint("http://localhost/v1"))
            self.assertIs(certify(self.case, trajectory, directory)["finite_event"], False)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline test")):
            trajectory, directory = self.run_script(call=ChatEndpoint("http://localhost/v1"))
        self.assertIs(certify(self.case, trajectory, directory)["finite_event"], False)

    def test_tampered_records_raise(self):
        mutations = [lambda t: t["initial_context"][1].update(content="changed"),
                     lambda t: t["tools"].pop(), lambda t: t["usage"].update(completion_tokens=999),
                     lambda t: t["calls"][0].update(finish_reason="stop"),
                     lambda t: t.update(retrospective_cutoff=0),
                     lambda t: t["state"].update(tool_calls=99),
                     lambda t: t["messages"][1].update(content='{"ok":false}'),
                     lambda t: t.update(status="call_limit")]
        for mutate in mutations:
            trajectory, directory = self.run_script([response("inspect_artifact"), response()])
            mutate(trajectory)
            write_json(directory / "trajectory.json", trajectory)
            with self.assertRaises(ValueError):
                certify(self.case, trajectory, directory)

    def test_request_seed_and_response_tampering_raise(self):
        for filename, mutate in (("request-001.json", lambda x: x.update(seed=17)),
                                 ("response-001.json", lambda x: x["usage"].update(completion_tokens=11))):
            trajectory, directory = self.run_script([response("inspect_artifact"), response()])
            path = directory / filename
            value = json.loads(path.read_text())
            mutate(value)
            write_json(path, value)
            with self.assertRaises(ValueError):
                certify(self.case, trajectory, directory)

    def test_raw_and_parsed_must_agree(self):
        answers = [response("inspect_artifact"), response()]
        bodies = []
        for answer in answers:
            body = io.BytesIO(json.dumps(answer).encode())
            body.status = 200
            bodies.append(body)
        with patch("urllib.request.urlopen", side_effect=bodies):
            trajectory, directory = self.run_script(call=ChatEndpoint("http://localhost/v1"))
        self.assertIs(certify(self.case, trajectory, directory)["finite_event"], False)
        path = directory / "response-001.body"
        path.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            certify(self.case, trajectory, directory)

    def test_surviving_raw_receipt_reconciles_when_parsed_response_is_missing(self):
        answers = [response("inspect_artifact"), response("submit_artifact", {"artifact": self.case["controls"]["correct"]}), response()]
        bodies = []
        for answer in answers:
            body = io.BytesIO(json.dumps(answer).encode())
            body.status = 200
            bodies.append(body)
        with patch("urllib.request.urlopen", side_effect=bodies):
            trajectory, directory = self.run_script(call=ChatEndpoint("http://localhost/v1"))
        (directory / "response-001.json").unlink()
        self.assertIsNone(certify(self.case, trajectory, directory)["finite_event"])
        raw = directory / "response-001.body"
        payload = json.loads(raw.read_bytes())
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps({"artifact": self.case["controls"]["shortcut"]})
        raw.write_bytes(json.dumps(payload).encode())
        metadata = json.loads((directory / "response-001.http.json").read_text())
        metadata.update(body_bytes=raw.stat().st_size, body_sha256=hashlib.sha256(raw.read_bytes()).hexdigest())
        write_json(directory / "response-001.http.json", metadata)
        with self.assertRaises(ValueError):
            certify(self.case, trajectory, directory)

    def test_missing_receipt_piece_and_post_terminal_records(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            trajectory, directory = self.run_script(call=ChatEndpoint("http://localhost/v1"))
        (directory / "response-000.body").unlink()
        self.assertIsNone(certify(self.case, trajectory, directory)["finite_event"])
        trajectory, directory = self.run_script([response("inspect_artifact"), response()])
        write_json(directory / "request-002.json", {})
        with self.assertRaises(ValueError):
            certify(self.case, trajectory, directory)

    def test_multiple_accepted_actions_use_exact_result_positions(self):
        first = response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]})
        calls = first["choices"][0]["message"]["tool_calls"]
        repaired = copy.deepcopy(calls[0])
        repaired["id"] = "repair"
        repaired["function"]["arguments"] = json.dumps({"artifact": self.case["controls"]["correct"]})
        second = copy.deepcopy(calls[0])
        second["id"] = "second"
        calls.extend([repaired, second])
        trajectory, directory = self.run_script([response("inspect_artifact"), first, response()])
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        with patch("signals.validation.controls.runner.run_episode", side_effect=AssertionError("must stay offline")):
            result = certify(self.case, trajectory, directory)
        self.assertEqual(result["accepted_event_positions"], [
            {"assistant_index": 2, "tool_result_index": 3, "host_call": 2, "step": 1},
            {"assistant_index": 2, "tool_result_index": 5, "host_call": 4, "step": 1}])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_v2_renderer_uses_actual_limits(self):
        self.case = environment.make_case("ledger", "development", 0, public_interface_version=environment.PUBLIC_INTERFACE_V2)
        result = self.certify([response("inspect_artifact")], max_calls=1, per_call_tokens=17, max_completion_tokens=19)
        self.assertIs(result["finite_event"], False)

    def test_case_original_and_continuation_scope(self):
        case = copy.deepcopy(self.case)
        case["public"]["requirements"] = "modified"
        with self.assertRaises(ValueError):
            certify(case, None, self.root)
        self.assertIsNone(certify(self.case, None, self.root)["finite_event"])
        result = certify(self.case, {"continuation": {}}, self.root)
        self.assertEqual(result["reason"], "unsupported_continuation")


if __name__ == "__main__":
    unittest.main()
