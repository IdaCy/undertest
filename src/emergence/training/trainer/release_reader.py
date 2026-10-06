"""Portable compact-record reader; no project paths, models or tokenizer imports."""
import hashlib
import json
from pathlib import Path
import struct
import zlib


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _read(root, ref):
    relative = Path(ref['path'])
    _require(not relative.is_absolute() and '..' not in relative.parts, 'unsafe portable path')
    path = (Path(root) / relative).resolve()
    _require(path.is_relative_to(Path(root).resolve()), 'portable path leaves release')
    with path.open('rb') as f:
        f.seek(ref['offset'])
        raw = f.read(ref['bytes'])
    _require(len(raw) == ref['bytes'] and hashlib.sha256(raw).hexdigest() == ref['sha256'], 'portable span hash mismatch')
    return raw


def read_record(root, reference):
    return json.loads(_read(root, reference))


def token_ids(root, reference):
    raw = _read(root, reference)
    _require(len(raw) == 4 * reference['tokens'], 'uint32 token count mismatch')
    return list(struct.unpack('<' + str(reference['tokens']) + 'I', raw))


def original_source_row(root, reference):
    # Exact input row bytes, including its original trailing newline.
    raw = zlib.decompress(_read(root, reference))
    _require(len(raw) == reference['raw_bytes'] and hashlib.sha256(raw).hexdigest() == reference['raw_sha256'], 'original source row mismatch')
    return raw


def _sequence(root, prompt, response, context):
    prefix = token_ids(root, prompt['token_span'])
    completion = token_ids(root, response['token_span'])
    _require(prefix and completion and completion[-1] == 248046 and 248046 not in completion[:-1], 'prompt or EOS mismatch')
    _require(len(prefix) + len(completion) <= context, 'sequence exceeds frozen context')
    return {'response_id': response['response_id'], 'input_ids': prefix + completion,
            'attention_mask': [1] * (len(prefix) + len(completion)),
            'labels': [-100] * len(prefix) + completion,
            'response_loss_mask': [0] * len(prefix) + [1] * len(completion),
            'prompt_tokens': len(prefix), 'completion_tokens': len(completion),
            'causal_prediction_count': len(completion),
            'causal_shift': 'loss(logits[:-1], labels[1:]); response_loss_mask[1:] aligns prediction positions'}


def arm_view(root, record, arm):
    _require(record['pool'] == 'train' and arm in ('sft', 'dpo', 'grpo'), 'invalid training view')
    prompt = token_ids(root, record['prompt']['token_span'])
    common = {'record_id': record['record_id'], 'component_id': record['component_id'], 'prompt_token_ids': prompt}
    if arm == 'grpo':
        # No teacher responses are exposed to the online generation view.
        return {**common, 'messages': record['prompt']['messages'], 'rendered_prompt': record['prompt']['rendered_text'], 'private_gold': record['private_gold'], 'requires_fresh_current_policy_generation': True}
    chosen = _sequence(root, record['prompt'], record['chosen'], record['context'])
    if arm == 'sft':
        return {**common, 'positive': chosen}
    return {**common, 'chosen': chosen, 'rejected': _sequence(root, record['prompt'], record['rejected'], record['context'])}


def iter_view(root, view):
    _require(view in ('sft', 'dpo', 'grpo', 'development', 'capability_holdout'), 'unknown view')
    root = Path(root)
    manifest = json.loads((root / 'manifest.json').read_bytes())
    relative = 'views/' + view + '.jsonl'
    pin = manifest['artifacts'][relative]
    path = root / relative
    raw_view = path.read_bytes()
    _require(len(raw_view) == pin['bytes'] and hashlib.sha256(raw_view).hexdigest() == pin['sha256'], 'view manifest pin mismatch')
    _require(len(raw_view.splitlines()) == pin['rows'], 'view manifest row count mismatch')
    with path.open('rb') as f:
        for raw in f:
            ref = json.loads(raw)
            record = read_record(root, ref['record'])
            _require(record['record_id'] == ref['record_id'] and record['component_id'] == ref['component_id'], 'view identity mismatch')
            if view in ('sft', 'dpo', 'grpo'):
                yield arm_view(root, record, view)
            else:
                _require(record['pool'] == view, 'heldout view pool mismatch')
                yield record
