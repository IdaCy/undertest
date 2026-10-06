import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

import signals.validation.controls.environment as environment
import signals.reasoning.monitor as monitor
from signals.reasoning.monitor import monitor_request
from signals.reasoning.monitor import parse_prediction
from signals.reasoning.monitor import run_monitor
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import digest


class SignalsMonitorTests(unittest.TestCase):
    def trajectory(self):
        return {
            "initial_context": [{"role": "user", "content": "Use a general pipeline; preview_cache is prohibited."}],
            "tools": [], "messages": [
                {"role": "assistant", "content": "public text", "reasoning_content": "private thought"},
                {"role": "assistant", "content": "secret future outcome"},
            ], "retrospective_cutoff": 1, "prospective_cutoff": 1,
        }

    def test_model_payload_excludes_cutoff_counts_channel_ids_and_future(self):
        trajectory = self.trajectory()
        trajectory["labels"] = {"answer": "secret answer"}
        for channel in ("context", "cot", "action", "combined"):
            request, observation = monitor_request(trajectory, channel, "retrospective", "model", 1)
            record = json.loads(request["messages"][1]["content"])["record"]
            self.assertNotIn("cutoff", record)
            self.assertNotIn("channel", record)
            self.assertEqual(observation["cutoff"], 1)
            self.assertNotIn("secret", json.dumps(request))
        context, _ = monitor_request(trajectory, "context", "retrospective", "model", 1)
        trajectory["retrospective_cutoff"] = 2
        context_later, _ = monitor_request(trajectory, "context", "retrospective", "model", 1)
        self.assertEqual(context, context_later)

    def test_invalid_predictions_are_missing_not_negative(self):
        request, _ = monitor_request(self.trajectory(), "cot", "retrospective", "model", 1)
        for content in ("not json", '{"probability": true, "evidence": []}',
                        '{"probability": 1.1, "evidence": []}', '{"probability": 0.5}',
                        '{"probability": 0.1, "probability": 0.9, "evidence": []}',
                        '{"probability": 0.1, "evidence": [], "evidence": []}'):
            with self.assertRaises(ValueError):
                parse_prediction({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}, request)

    def test_fabricated_evidence_is_flagged_without_changing_probability(self):
        request, _ = monitor_request(self.trajectory(), "cot", "retrospective", "model", 1)
        response = {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "probability": .7, "evidence": ["private thought", "invented evidence"],
        })}, "finish_reason": "stop"}]}
        parsed = parse_prediction(response, request)
        self.assertEqual(parsed["probability"], .7)
        self.assertEqual(parsed["unsupported_evidence"], ["invented evidence"])

    def test_malformed_http_json_is_archived_for_failed_monitor_call(self):
        trajectory = self.trajectory()
        trajectory["assignment"] = {"assignment_id": "test-assignment"}
        raw = b' \r\n{ definitely invalid JSON }\n'
        http_response = io.BytesIO(raw)
        http_response.status = 200
        endpoint = ChatEndpoint("http://localhost/v1", api_key="SECRET_TEST_KEY")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "monitor"
            with patch("urllib.request.urlopen", return_value=http_response):
                result = run_monitor(
                    trajectory=trajectory, channel="cot", cutoff_name="retrospective",
                    model="fake/model", model_revision="revision", seed=1, output=output, call=endpoint,
                )
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["prediction"])
            self.assertEqual((output / "response.body").read_bytes(), raw)
            metadata = json.loads((output / "response.http.json").read_text())
            self.assertEqual(metadata["status"], 200)
            self.assertTrue(metadata["body_complete"])
            self.assertFalse(metadata["response_parsed"])
            self.assertFalse((output / "response.json").exists())
            self.assertNotIn("SECRET_TEST_KEY", json.dumps(metadata))

    def response(self, content):
        return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}

    def test_v1_prompt_requests_and_raw_parse_remain_replayable(self):
        requests = []
        for channel in ("context", "cot", "action", "combined"):
            for cutoff in ("retrospective", "prospective"):
                request, observation = monitor_request(self.trajectory(), channel, cutoff, "model", 1)
                explicit, explicit_observation = monitor_request(self.trajectory(), channel, cutoff, "model", 1,
                                                                 version=monitor.MONITOR_VERSION_V1)
                self.assertEqual(request, explicit)
                self.assertEqual(observation, explicit_observation)
                self.assertEqual(request["response_format"], {"type": "json_object"})
                requests.append(request)
        self.assertEqual(monitor.prompt_hash(), "216c5555d24c2d44deeace380f26c86385ef8529dedbebeb4c78c059acba4017")
        self.assertEqual(digest(requests), "789fd9f5dae86c102d211e92e1c1f5fbd43bad8b5210da71126b85c2f99efb68")
        request = requests[0]
        for content in ("{}", '{"probability": NaN, "evidence": []}',
                        '{"probability": Infinity, "evidence": []}', '{"probability": 1e400, "evidence": []}',
                        '{"probability": false, "evidence": []}', '{"probability": 0, "evidence": [null]}',
                        '{"probability": 0, "evidence": [[]]}', '{"probability": 0, "evidence": [{}]}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_prediction(self.response(content), request)
        self.assertEqual(parse_prediction(self.response('{"probability": 0, "evidence": []}'), request)["probability"], 0)

    def test_v2_response_schema_matches_parser_over_finite_json_predictions(self):
        request, _ = monitor_request(self.trajectory(), "combined", "retrospective", "model", 1,
                                     version=monitor.MONITOR_VERSION_V2)
        format_spec = request["response_format"]
        self.assertEqual(format_spec["type"], "json_schema")
        self.assertTrue(format_spec["json_schema"]["strict"])
        spec = format_spec["json_schema"]["schema"]
        Draft202012Validator.check_schema(spec)
        schema = Draft202012Validator(spec)
        values = [None, [], 0, "text", {}, {"probability": .5}, {"evidence": []},
                  {"probability": 0, "evidence": [], "extra": 1}]
        values += [{"probability": probability, "evidence": evidence}
                   for probability in (0, 1, .5, 0.0, True, False, -.1, 1.1, "0.5", None, [], {})
                   for evidence in ([], ["private thought"], ["x" * 1000], ["x" * 1001], [""], [1], [None], [{}], [[]], "quote", None)]
        for value in values:
            with self.subTest(value=value):
                try:
                    parse_prediction(self.response(json.dumps(value, allow_nan=False)), request)
                    host_valid = True
                except ValueError:
                    host_valid = False
                self.assertEqual(schema.is_valid(value), host_valid)
        # Duplicate members and non-finite numbers are source-text/JSON-domain checks.
        for content in ('{"probability": 0, "probability": 1, "evidence": []}',
                        '{"probability": NaN, "evidence": []}', '{"probability": 1e400, "evidence": []}'):
            with self.assertRaises(ValueError):
                parse_prediction(self.response(content), request)

    def test_v2_horizon_identity_and_context_projection_are_explicit(self):
        trajectory = self.trajectory()
        trajectory["labels"] = {"secret": "future label"}
        trajectory["status"] = "call_limit"
        identity = monitor.monitor_identity(monitor.MONITOR_VERSION_V2)
        self.assertEqual(identity["endpoint_version"], monitor.FINITE_HORIZON_VERSION)
        self.assertNotEqual(identity["prompt_sha256"], monitor.prompt_hash())
        spec = monitor.response_format(monitor.MONITOR_VERSION_V2)
        self.assertEqual(identity["response_format_sha256"], digest(spec))
        self.assertEqual(identity["response_schema_sha256"], digest(spec["json_schema"]["schema"]))
        for cutoff in ("retrospective", "prospective"):
            for channel in ("context", "cot", "action", "combined"):
                old, old_observation = monitor_request(trajectory, channel, cutoff, "model", 1)
                new, new_observation = monitor_request(trajectory, channel, cutoff, "model", 1,
                                                       version=monitor.MONITOR_VERSION_V2)
                self.assertEqual(old_observation, new_observation)
                payload = json.loads(new["messages"][1]["content"])
                self.assertEqual(payload["record"], json.loads(old["messages"][1]["content"])["record"])
                self.assertEqual(payload["question"], monitor.QUESTIONS_V2[cutoff])
                self.assertNotIn("secret", json.dumps(new))
                self.assertNotIn("call_limit", json.dumps(new))
            first, _ = monitor_request(trajectory, "context", cutoff, "model", 1, version=monitor.MONITOR_VERSION_V2)
            trajectory[f"{cutoff}_cutoff"] = 2
            later, _ = monitor_request(trajectory, "context", cutoff, "model", 1, version=monitor.MONITOR_VERSION_V2)
            self.assertEqual(first, later)
            trajectory[f"{cutoff}_cutoff"] = 1
        for version in ("unknown", None):
            with self.assertRaises(ValueError):
                monitor_request(trajectory, "context", "retrospective", "model", 1, version=version)
            with self.assertRaises(ValueError):
                monitor.prompt_hash(version)

    def test_v2_all_views_receive_exact_amended_actor_context_and_tools(self):
        for family in environment.FAMILIES:
            case = environment.make_case(family, "development", 0, public_interface_version=environment.PUBLIC_INTERFACE_V2)
            trajectory = self.trajectory()
            trajectory["initial_context"] = [{"role": "user", "content": environment.render_case(
                case, max_calls=6, per_call_tokens=3072, max_completion_tokens=16000,
            )}]
            trajectory["tools"] = environment.tool_definitions()
            for channel in ("context", "cot", "action", "combined"):
                request, _ = monitor_request(trajectory, channel, "retrospective", "model", 1,
                                             version=monitor.MONITOR_VERSION_V2)
                record = json.loads(request["messages"][1]["content"])["record"]
                self.assertEqual(record["initial_context"], trajectory["initial_context"])
                self.assertEqual(record["tools"], trajectory["tools"])

    def test_v2_records_identity_and_preserves_invalid_responses_without_retry(self):
        trajectory = self.trajectory()
        trajectory["assignment"] = {"assignment_id": "test-assignment"}
        for content, expected_status in (("{}", "error"), ('{"probability": 0, "evidence": []}', "completed")):
            calls = []

            def call(request):
                calls.append(request)
                return self.response(content)

            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "monitor"
                result = run_monitor(trajectory=trajectory, channel="cot", cutoff_name="retrospective",
                                     model="fake/model", model_revision="revision", seed=1, output=output,
                                     call=call, version=monitor.MONITOR_VERSION_V2)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(len(calls), 1)
                for key, value in monitor.monitor_identity(monitor.MONITOR_VERSION_V2).items():
                    self.assertEqual(result[key], value)
                self.assertEqual(json.loads((output / "response.json").read_text()), self.response(content))
                self.assertEqual(json.loads((output / "request.json").read_text()), calls[0])
                if expected_status == "error":
                    self.assertIsNone(result["prediction"])


if __name__ == "__main__":
    unittest.main()
