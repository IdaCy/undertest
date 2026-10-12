import copy
import json
import unittest

from causes.campaign_prompt_tokens import count_text_chat, function_tools, render_text_chat, text_conversation


class Tokenizer:
    chat_template = "fixture"

    def apply_chat_template(self, conversation, tools, chat_template, tokenize, return_dict,
                            add_generation_prompt=True, continue_final_message=False, **kwargs):
        self.rendered = dict(conversation=conversation, tools=tools, kwargs=kwargs,
                             add_generation_prompt=add_generation_prompt)
        assert tokenize is False and return_dict is False and not continue_final_message
        return json.dumps(self.rendered, sort_keys=True)

    def encode(self, text, add_special_tokens):
        assert add_special_tokens is False
        return list(text.encode())


class PromptTokensTests(unittest.TestCase):
    def options(self):
        return dict(template_variables={"enable_thinking", "preserve_thinking", "reasoning_effort"},
                    base_parameters={"add_generation_prompt", "continue_final_message"}, content_format="string")

    def request(self):
        return dict(messages=[dict(role="user", content="hello")], tool_choice="none", max_tokens=8192)

    def test_tool_none_retains_native_function_defaults_and_order(self):
        request = self.request()
        request["tools"] = [dict(function=dict(name="f", strict=None))]
        tokenizer = Tokenizer()
        render_text_chat(request, tokenizer, **self.options())
        self.assertEqual(tokenizer.rendered["tools"], [dict(type="function", function=dict(name="f", description=None, parameters=None))])
        self.assertEqual(list(tokenizer.rendered["tools"][0]["function"]), ["name", "description", "parameters"])
        render_text_chat(request, tokenizer, exclude_tools_when_tool_choice_none=True, **self.options())
        self.assertIsNone(tokenizer.rendered["tools"])

    def test_tool_defer_propagation_keeps_explicit_function_override(self):
        self.assertEqual(function_tools([dict(function=dict(name="f"), defer_loading=True)])[0]["function"]["defer_loading"], True)
        self.assertEqual(function_tools([dict(function=dict(name="f", defer_loading=False), defer_loading=True)])[0]["function"]["defer_loading"], False)

    def test_text_parts_reasoning_and_arguments_normalize_without_mutation(self):
        messages = [dict(role="assistant", content=None, reasoning="thinking", tool_calls=[dict(type="function", function=dict(name="f", arguments='{"x":1}'))]),
                    dict(role="tool", content=[dict(type="text", text="a"), dict(type="text", text=""), "b"], tool_call_id="id")]
        original = copy.deepcopy(messages)
        normalized = text_conversation(messages)
        self.assertEqual(messages, original)
        self.assertEqual(normalized[0]["tool_calls"][0]["function"]["arguments"], {"x": 1})
        self.assertEqual(normalized[0]["reasoning_content"], "thinking")
        self.assertEqual(normalized[1]["content"], "a\nb")

    def test_empty_tool_calls_and_null_arguments_match_native(self):
        self.assertNotIn("tool_calls", text_conversation([dict(role="assistant", tool_calls=[])])[0])
        for arguments in (None, "", "null"):
            result = text_conversation([dict(role="assistant", tool_calls=[dict(function=dict(arguments=arguments))])])
            self.assertEqual(result[0]["tool_calls"][0]["function"]["arguments"], {})

    def test_openai_content_keeps_empty_parts_and_tool_normalization(self):
        messages = [dict(role="user", content=[dict(type="text", text="a"), dict(type="text", text=""), dict(type="text", text="b")]), dict(role="tool", content=[dict(type="text", text="a"), dict(type="text", text=""), dict(type="text", text="b")])]
        result = text_conversation(messages, content_format="openai")
        self.assertEqual(result[0]["content"], messages[0]["content"])
        self.assertEqual(result[1]["content"], "a\n\nb")

    def test_deprecated_reasoning_alias_fills_only_absent_canonical_value(self):
        message = dict(role="assistant", content="answer", reasoning_content="old")
        self.assertEqual(text_conversation([message])[0]["reasoning"], "old")
        message["reasoning"] = "new"
        self.assertEqual(text_conversation([message])[0]["reasoning_content"], "new")

    def test_api_effort_overrides_nested_and_filters_unused_kwargs(self):
        request = self.request()
        request.update(reasoning_effort="xhigh", chat_template_kwargs=dict(reasoning_effort="low", enable_thinking=False, preserve_thinking=True, ignored="value"))
        tokenizer = Tokenizer()
        render_text_chat(request, tokenizer, **self.options())
        self.assertEqual(tokenizer.rendered["kwargs"], dict(reasoning_effort="xhigh", enable_thinking=False, preserve_thinking=True))

    def test_unset_nested_kwargs_are_dropped_by_native_final_merge(self):
        for unset in (None, "auto"):
            tokenizer = Tokenizer()
            request = dict(self.request(), reasoning_effort="xhigh", chat_template_kwargs=dict(enable_thinking=unset, preserve_thinking=unset))
            render_text_chat(request, tokenizer, **self.options())
            self.assertEqual(tokenizer.rendered["kwargs"], dict(reasoning_effort="xhigh"))

    def test_tool_values_requiring_pydantic_coercion_refuse(self):
        for key, value in [("description", 1), ("parameters", []), ("strict", "true"), ("defer_loading", 1)]:
            with self.assertRaises(ValueError):
                function_tools([dict(function=dict(name="f", **{key: value}))])

    def test_unsupported_media_and_role_refuse(self):
        for message in [dict(role="developer", content="x"), dict(role="user", content=[dict(type="image_url", image_url="x")])]:
            with self.assertRaises(ValueError):
                text_conversation([message])

    def test_render_controls_cannot_silently_change_counts(self):
        for key, value in [("truncate_prompt_tokens", 100), ("add_special_tokens", True), ("continue_final_message", True), ("chat_template", "other"), ("tool_choice", "auto")]:
            with self.assertRaises(ValueError):
                render_text_chat(dict(self.request(), **{key: value}), Tokenizer(), **self.options())
        with self.assertRaisesRegex(ValueError, "reserved_chat_kwarg"):
            render_text_chat(dict(self.request(), chat_template_kwargs=dict(tokenize=True)), Tokenizer(), **self.options())

    def test_counts_do_not_emit_prompt_or_tokens(self):
        output = count_text_chat(self.request(), Tokenizer(), **self.options())
        self.assertEqual(output["maximum_total_tokens"], output["prompt_tokens"] + 8192)
        self.assertEqual(set(output), {"prompt_tokens", "output_allowance", "maximum_total_tokens", "rendered_utf8_bytes", "rendered_sha256", "token_ids_sha256"})


if __name__ == "__main__":
    unittest.main()
