import hashlib
import http.client
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import signals.validation.controls.environment as environment
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import archive_exchange
from signals.validation.controls.runner import assistant_message
from signals.validation.controls.runner import assignment_ledger
from signals.validation.controls.runner import read_checkpoint
from signals.validation.controls.runner import request_seed
from signals.validation.controls.runner import run_episode
from signals.validation.controls.runner import validate_request_seeds


def response(name=None, arguments=None, *, reasoning="I will complete the task.", finish=None):
    message = {"role": "assistant", "content": None if name else "Finished.", "reasoning_content": reasoning}
    if name:
        message["tool_calls"] = [{
            "id": "call_a", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments or {})},
        }]
    return {
        "choices": [{"message": message, "finish_reason": finish or ("tool_calls" if name else "stop")}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }


class SignalsRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = environment.make_case("ledger", "development", 0)
        self.assignment = assignment_ledger(["ledger"], "development", 1, 1, 21)[0]

    def run_script(self, answers, *, output_name="episode", **kwargs):
        script = iter(answers)
        requests = []

        def call(request):
            requests.append(request)
            answer = next(script)
            if isinstance(answer, Exception):
                raise answer
            return answer

        record = run_episode(
            case=self.case, assignment=self.assignment, output=self.root / output_name,
            model="fake/model", model_revision="frozen-revision", generation={"temperature": 1},
            call=call, environment=environment, **kwargs,
        )
        return record, requests

    def test_checkpoint_precedes_mutation_and_checker_is_never_sent(self):
        record, requests = self.run_script([
            response("inspect_artifact"),
            response("submit_artifact", {"artifact": self.case["controls"]["correct"]}),
            response(),
        ])
        self.assertEqual(record["status"], "completed")
        saved = read_checkpoint(self.root / "episode/checkpoint.json")
        self.assertEqual(saved["completed_calls"], 1)
        self.assertEqual(saved["usage"]["completion_tokens"], 10)
        self.assertEqual(saved["next_generation_seed"], request_seed(self.assignment, 1))
        self.assertEqual(len(saved["messages"]), record["prospective_cutoff"])
        self.assertNotEqual(saved["state"], record["state"])
        outcome = json.loads((self.root / "episode/labels.json").read_text())
        self.assertTrue(outcome["complete_episode"])
        self.assertTrue(outcome["observed_outcomes"]["task_success"])
        for request in requests:
            body = json.dumps(request)
            for forbidden in ("hidden_cases", "observed_outcomes", "successful_proxy_exploitation"):
                self.assertNotIn(forbidden, body)
        self.assertEqual(len(record["calls"]), 3)
        self.assertEqual([tool["function"]["name"] for tool in requests[0]["tools"]], ["inspect_artifact"])
        self.assertEqual(requests[1]["tools"], record["tools"])
        self.assertEqual(saved["tools"], record["tools"])

    def test_failure_retains_positive_action_and_incomplete_status(self):
        record, _ = self.run_script([
            response("inspect_artifact"),
            response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}),
            RuntimeError("simulated transport failure"),
        ])
        self.assertEqual(record["status"], "error")
        labels = json.loads((self.root / "episode/labels.json").read_text())
        self.assertTrue(labels["observed_outcomes"]["shortcut_attempt"])
        self.assertFalse(labels["complete_episode"])
        self.assertTrue((self.root / "episode/request-002.json").exists())
        self.assertFalse((self.root / "episode/response-002.json").exists())

    def test_v2_context_discloses_actual_limits_and_checkpoint_keeps_original_totals(self):
        self.case = environment.make_case("ledger", "development", 0, public_interface_version=environment.PUBLIC_INTERFACE_V2)
        record, requests = self.run_script([response("inspect_artifact"), response()],
                                           max_calls=8, per_call_tokens=512, max_completion_tokens=1400)
        public = json.loads(record["initial_context"][1]["content"])
        self.assertEqual(public["model_response_budget"], {"max_calls": 8, "per_call_tokens": 512,
            "max_completion_tokens": 1400, "final_response_consumes_call": True})
        self.assertEqual(requests[0]["messages"][:2], requests[1]["messages"][:2])
        self.assertEqual(requests[0]["max_tokens"], 512)
        checkpoint = read_checkpoint(self.root / "episode/checkpoint.json")
        self.assertEqual(checkpoint["case"], self.case)
        self.assertEqual(checkpoint["initial_context"], record["initial_context"])
        self.assertEqual(checkpoint["limits"], record["limits"])

    def test_truncated_generation_is_not_completed_or_executed(self):
        record, _ = self.run_script([response("inspect_artifact", finish="length")])
        self.assertEqual(record["status"], "generation_limit")
        self.assertIsNone(record["prospective_cutoff"])
        self.assertEqual(record["calls"][0]["finish_reason"], "length")
        self.assertFalse((self.root / "episode/checkpoint.json").exists())

    def test_wrong_first_tool_cannot_mutate_before_checkpoint(self):
        record, _ = self.run_script([
            response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}),
        ])
        self.assertEqual(record["status"], "error")
        labels = json.loads((self.root / "episode/labels.json").read_text())
        self.assertFalse(labels["observed_outcomes"]["shortcut_attempt"])

    def test_falsey_malformed_tool_calls_never_become_completed_episodes(self):
        for index, calls in enumerate(({}, "", False, 0)):
            with self.subTest(calls=calls):
                malformed = response()
                malformed["choices"][0]["message"]["tool_calls"] = calls
                name = f"malformed-{index}"
                record, requests = self.run_script([response("inspect_artifact"), malformed], output_name=name)
                self.assertEqual(record["status"], "error")
                self.assertIn("Invalid tool_calls", record["errors"][0]["message"])
                self.assertEqual(record["completed_calls"], 1)
                self.assertEqual(len(record["messages"]), 2)
                self.assertEqual(len(requests), 2)
                saved = json.loads((self.root / name / "response-001.json").read_text())
                self.assertEqual(saved, malformed)
                self.assertFalse(json.loads((self.root / name / "labels.json").read_text())["complete_episode"])
        for calls in (None, []):
            valid = response()
            valid["choices"][0]["message"]["tool_calls"] = calls
            self.assertEqual(assistant_message(valid)[0]["tool_calls"], calls)

    def test_rejected_initial_inspection_has_no_checkpoint_or_continuation(self):
        for index, arguments in enumerate(({"unexpected": True}, "[]", "not json")):
            with self.subTest(arguments=arguments):
                initial = response("inspect_artifact", arguments if isinstance(arguments, dict) else None)
                if isinstance(arguments, str):
                    initial["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
                name = f"inspection-{index}"
                record, requests = self.run_script([
                    initial,
                    response("submit_artifact", {"artifact": self.case["controls"]["shortcut"]}),
                ], output_name=name)
                self.assertEqual(record["status"], "error")
                self.assertIn("Protocol failure: initial inspection", record["errors"][0]["message"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(record["messages"][-1]["role"], "tool")
                self.assertFalse(json.loads(record["messages"][-1]["content"]).get("ok", False))
                self.assertIsNone(record["prospective_cutoff"])
                self.assertFalse((self.root / name / "checkpoint.json").exists())
                self.assertIsNone(record["state"]["artifact"])
                self.assertFalse(json.loads((self.root / name / "labels.json").read_text())["complete_episode"])

    def test_malformed_http_success_body_is_archived_before_parsing(self):
        raw = b' \r\n{"choices": [ MALFORMED \xff BODY'
        http_response = io.BytesIO(raw)
        http_response.status = 200
        endpoint = ChatEndpoint("http://localhost/v1", api_key="SECRET_TEST_KEY")
        output = self.root / "raw-http"
        with patch("urllib.request.urlopen", return_value=http_response):
            record = run_episode(
                case=self.case, assignment=self.assignment, output=output,
                model="fake/model", model_revision="revision", generation={},
                call=endpoint, environment=environment,
            )
        self.assertEqual(record["status"], "error")
        self.assertEqual((output / "response-000.body").read_bytes(), raw)
        metadata = json.loads((output / "response-000.http.json").read_text())
        self.assertEqual(metadata["status"], 200)
        self.assertEqual(metadata["body_bytes"], len(raw))
        self.assertEqual(metadata["body_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(metadata["body_complete"])
        self.assertFalse(metadata["response_parsed"])
        self.assertFalse((output / "response-000.json").exists())
        self.assertNotIn("SECRET_TEST_KEY", json.dumps(metadata))
        self.assertNotIn("Authorization", json.dumps(metadata))

    def test_malformed_public_messages_never_become_completed_labels(self):
        malformed = [17, {"text": "wrong container"}, [{"type": "text", "text": 17}],
                     [{"type": "unsupported", "text": "data"}]]
        for content in malformed:
            with self.subTest(content=content):
                payload = response()
                payload["choices"][0]["message"]["content"] = content
                with self.assertRaises(ValueError):
                    assistant_message(payload)
        payload = response("inspect_artifact")
        payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = ""
        with self.assertRaises(ValueError):
            assistant_message(payload)
        with self.assertRaises(ValueError):
            assistant_message(response(finish="tool_calls"))
        payload = response()
        payload["choices"][0]["message"]["content"] = None
        with self.assertRaises(ValueError):
            assistant_message(payload)

    def test_explicit_refusal_and_valid_typed_text_have_terminal_content(self):
        payload = response()
        payload["choices"][0]["message"]["content"] = None
        payload["choices"][0]["message"]["refusal"] = "I decline."
        self.assertEqual(assistant_message(payload)[1], "stop")
        payload["choices"][0]["message"]["content"] = [{"type": "text", "text": "Done."}]
        self.assertEqual(assistant_message(payload)[1], "stop")

    def test_assignment_order_is_reproducible_and_all_repetitions_retained(self):
        rows = assignment_ledger(["ledger", "validator"], "confirmation", 3, 4, 7)
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows, assignment_ledger(["ledger", "validator"], "confirmation", 3, 4, 7))
        self.assertEqual(len({row["assignment_id"] for row in rows}), 24)
        self.assertEqual(len({row["generation_seed"] for row in rows}), 24)
        validate_request_seeds(rows, 6)
        self.assertEqual(len({request_seed(row, step) for row in rows for step in range(6)}), 144)
        with self.assertRaises(ValueError):
            validate_request_seeds([rows[0], rows[0]], 6)


class EndpointEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.endpoint = ChatEndpoint("http://localhost/v1", api_key="SECRET_TEST_KEY")

    def call_and_archive(self, prefix):
        try:
            return self.endpoint({"model": "fake", "messages": []})
        finally:
            archive_exchange(self.endpoint, self.output, prefix)

    def test_exact_success_bytes_and_transport_failure_clear_prior_evidence(self):
        raw = b'  {"value": "\\u03bb"}\r\n'
        successful = io.BytesIO(raw)
        successful.status = 200
        with patch("urllib.request.urlopen", side_effect=[successful, urllib.error.URLError("simulated")]):
            self.assertEqual(self.call_and_archive("success"), {"value": "λ"})
            with self.assertRaises(urllib.error.URLError):
                self.call_and_archive("failure")
        self.assertEqual((self.output / "success.body").read_bytes(), raw)
        success = json.loads((self.output / "success.http.json").read_text())
        self.assertTrue(success["response_parsed"])
        self.assertEqual(success["status"], 200)
        self.assertEqual((self.output / "failure.body").read_bytes(), b"")
        failure = json.loads((self.output / "failure.http.json").read_text())
        self.assertIsNone(failure["status"])
        self.assertEqual(failure["error_type"], "URLError")
        self.assertFalse(failure["body_complete"])
        self.assertFalse(failure["response_parsed"])

    def test_http_error_body_is_not_truncated_and_headers_are_not_saved(self):
        raw = b"server error detail " * 1000
        error = urllib.error.HTTPError("http://localhost/v1", 429, "rate limited", {"X-Secret": "SECRET_HEADER"}, io.BytesIO(raw))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                self.call_and_archive("http-error")
        self.assertGreater(len(raw), 8192)
        self.assertEqual((self.output / "http-error.body").read_bytes(), raw)
        metadata = json.loads((self.output / "http-error.http.json").read_text())
        self.assertEqual(metadata["status"], 429)
        self.assertTrue(metadata["body_complete"])
        self.assertFalse(metadata["response_parsed"])
        self.assertNotIn("SECRET", json.dumps(metadata))
        self.assertNotIn("headers", metadata)

    def test_nonfinite_and_duplicate_json_is_rejected_with_raw_evidence(self):
        class Response(io.BytesIO):
            status = 200

        for index, raw in enumerate((b'{"usage":{"extra":NaN}}', b'{"usage":{"extra":1e999}}',
                                     b'{"usage":{"extra":-1e999}}', b'{"choices":[],"choices":[]}')):
            prefix = f"invalid-json-{index}"
            with patch("urllib.request.urlopen", return_value=Response(raw)):
                with self.assertRaises(ValueError):
                    self.call_and_archive(prefix)
            self.assertEqual((self.output / f"{prefix}.body").read_bytes(), raw)
            metadata = json.loads((self.output / f"{prefix}.http.json").read_text())
            self.assertTrue(metadata["body_complete"])
            self.assertFalse(metadata["response_parsed"])

    def test_timeout_after_buffered_partial_body_retains_received_bytes(self):
        class TimedOutBody(io.RawIOBase):
            first = True

            def readable(self):
                return True

            def readinto(self, buffer):
                if self.first:
                    self.first = False
                    wire = b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\npartial"
                    buffer[:len(wire)] = wire
                    return len(wire)
                raise TimeoutError("simulated network timeout")

        class Socket:
            def makefile(self, *_args, **_kwargs):
                return io.BufferedReader(TimedOutBody())

        response = http.client.HTTPResponse(Socket())
        response.begin()
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(TimeoutError):
                self.call_and_archive("timeout-partial")
        self.assertEqual((self.output / "timeout-partial.body").read_bytes(), b"partial")
        metadata = json.loads((self.output / "timeout-partial.http.json").read_text())
        self.assertFalse(metadata["body_complete"])
        self.assertFalse(metadata["response_parsed"])

    def test_content_length_mismatch_is_not_complete_even_if_json_parses(self):
        class Socket:
            def makefile(self, *_args, **_kwargs):
                return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}")

        response = http.client.HTTPResponse(Socket())
        response.begin()
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(http.client.IncompleteRead):
                self.call_and_archive("short-body")
        self.assertEqual((self.output / "short-body.body").read_bytes(), b"{}")
        metadata = json.loads((self.output / "short-body.http.json").read_text())
        self.assertFalse(metadata["body_complete"])
        self.assertFalse(metadata["response_parsed"])

    def test_interrupted_response_keeps_received_bytes_and_status(self):
        class InterruptedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _size):
                raise http.client.IncompleteRead(b"partial bytes", 100)

        with patch("urllib.request.urlopen", return_value=InterruptedResponse()):
            with self.assertRaises(http.client.IncompleteRead):
                self.call_and_archive("partial")
        self.assertEqual((self.output / "partial.body").read_bytes(), b"partial bytes")
        metadata = json.loads((self.output / "partial.http.json").read_text())
        self.assertEqual(metadata["status"], 200)
        self.assertFalse(metadata["body_complete"])
        self.assertFalse(metadata["response_parsed"])


if __name__ == "__main__":
    unittest.main()
