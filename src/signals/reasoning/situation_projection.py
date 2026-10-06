"""Bind runner-v1 original text projections to the captured trajectory content."""

from signals.validation.controls.runner import digest
from signals.reasoning.situation_annotation import UnsupportedSource
from signals.reasoning.situation_annotation import prepare_exp_source


PROJECTION_VERSION = "sigres-runner-text-projection-v1"


def project_original(original: dict, *, expected_trajectory_sha256: str,
                     generated_cutoff: int | None = None) -> dict:
    """Keep partial emitted text; this is observation preparation, never actor replay.

    The caller first verifies the raw file with read_original or equivalent byte pins.
    This function additionally binds the projection to that parsed source. Its cutoff
    is an exclusive index in runner-generated messages, excluding initial context.
    """
    if digest(original) != expected_trajectory_sha256:
        raise UnsupportedSource("Original trajectory content changed")
    if (type(original.get("schema_version")) is not int or original["schema_version"] != 1
            or "continuation" in original or "branch_id" in original):
        raise UnsupportedSource("This projection supports original runner-v1 episodes")
    initial, generated = original.get("initial_context"), original.get("messages")
    if type(initial) is not list or type(generated) is not list:
        raise UnsupportedSource("Original message arrays are missing")
    if generated_cutoff is None:
        generated_cutoff = original.get("retrospective_cutoff")
    if type(generated_cutoff) is not int or not 0 <= generated_cutoff <= len(generated):
        raise UnsupportedSource("Invalid original generated-message cutoff")
    assignment = original.get("assignment")
    if type(assignment) is not dict or type(assignment.get("assignment_id")) is not str:
        raise UnsupportedSource("Original assignment identity is missing")

    visible = initial + generated[:generated_cutoff]
    projected, mapping = [], []
    reasoning_known, public_known = [], []
    for index, message in enumerate(visible):
        if type(message) is not dict:
            raise UnsupportedSource("Unsupported native source message")
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise UnsupportedSource("Unsupported source speaker")
        if index < len(initial) and role not in {"system", "user"}:
            raise UnsupportedSource("Initial runner instructions cannot supply actor-authored evidence")
        if index >= len(initial) and role not in {"assistant", "tool"}:
            raise UnsupportedSource("Unsupported speaker in the generated runner history")
        message_id = f"m{index:04d}"
        channels, fields = {}, {}
        content = message.get("content")
        if content is not None and type(content) is not str:
            raise UnsupportedSource("This projection requires native text or null content")
        if content is not None:
            channels["public"] = {"text": content, "origin": "actor" if role == "assistant" else "context"}
            fields["public"] = "content"
        if role == "assistant":
            public_known.append("content" in message)
            first, alias = message.get("reasoning"), message.get("reasoning_content")
            if any(value is not None and type(value) is not str for value in (first, alias)):
                raise UnsupportedSource("Unsupported reasoning representation")
            if first is not None and alias is not None and first != alias:
                raise UnsupportedSource("Conflicting reasoning aliases")
            reasoning = alias if alias is not None else first
            reasoning_known.append(reasoning is not None)
            if reasoning is not None:
                channels["raw_reasoning"] = {"text": reasoning, "origin": "actor"}
                fields["raw_reasoning"] = "reasoning_content" if alias is not None else "reasoning"
        projected.append({"id": message_id, "speaker": role, "channels": channels})
        mapping.append({"message_id": message_id, "original_list": "initial_context" if index < len(initial) else "messages",
                        "original_index": index if index < len(initial) else index - len(initial),
                        "channel_fields": fields})
        refusal = message.get("refusal")
        if refusal is not None and type(refusal) is not str:
            raise UnsupportedSource("Unsupported refusal representation")
        if refusal:
            if role != "assistant":
                raise UnsupportedSource("Refusal text has no supported actor provenance")
            refusal_id = message_id + "_refusal"
            projected.append({"id": refusal_id, "speaker": "assistant",
                              "channels": {"public": {"text": refusal, "origin": "actor"}}})
            mapping.append({"message_id": refusal_id, "original_list": mapping[-1]["original_list"],
                            "original_index": mapping[-1]["original_index"], "channel_fields": {"public": "refusal"}})

    if not any(reasoning_known):
        reasoning_status, reasoning_reason = "unavailable", "No readable reasoning field at this cutoff"
    elif not all(reasoning_known):
        reasoning_status, reasoning_reason = "partial", "Some assistant items have no readable reasoning field"
    else:
        reasoning_status, reasoning_reason = "readable", "All observed assistant items provide a text reasoning field; this is not all internal reasoning"
    if not public_known:
        public_status, public_reason = "unavailable", "No captured actor return at this cutoff"
    elif all(public_known):
        public_status, public_reason = "readable", "Exact captured public text, including separate refusal fields"
    else:
        public_status, public_reason = "partial", "Some assistant items omit the native content field; absence is not established"
    source = prepare_exp_source(
        trajectory_id=assignment["assignment_id"], trajectory_sha256=expected_trajectory_sha256,
        cutoff_id="original-generated-prefix", cutoff_index=len(projected), messages=projected,
        channel_availability={
            "public": {"status": public_status, "reason": public_reason},
            "raw_reasoning": {"status": reasoning_status, "reason": reasoning_reason},
            "reasoning_summary": {"status": "unavailable", "reason": "No provider summary in runner-v1 capture"},
        },
    )
    return {"version": PROJECTION_VERSION, "original_trajectory_sha256": expected_trajectory_sha256,
            "original_status": original.get("status"), "generated_cutoff": generated_cutoff,
            "initial_context_count": len(initial), "source": source, "mapping": mapping,
            "coverage_claim": "Captured text through the stated cutoff; no assertion of task or internal-state completeness"}
