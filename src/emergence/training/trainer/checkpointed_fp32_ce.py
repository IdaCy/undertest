"""Prospective CE-only memory route; no runtime authority or native imports.

The caller computes exactly one ordinary full selected BF16 vocabulary-head
projection. This function never calls, splits, replaces or bypasses that head.
Only conversion to FP32 and unreduced full-vocabulary cross entropy are split.
This source is a new implementation hypothesis, not accepted equivalence.
"""

IMPLEMENTATION = 'dense_selected_bf16_projection_checkpointed_fp32_ce128_v1'
CHUNK_TOKENS = 128
VOCABULARY = 248320
MAX_TARGETS = 16383


def require(ok, message):
    if not ok:
        raise ValueError(message)


def chunk_ranges(count):
    require(type(count) is int and 0 < count <= MAX_TARGETS,
            'complete nonempty target extent within original context')
    return [(start, min(count, start + CHUNK_TOKENS))
            for start in range(0, count, CHUNK_TOKENS)]


def token_logprobs(torch, logits, targets, *, gradients, check):
    """Return one ordered FP32 log probability for every original target.

    ``logits`` is the caller's complete [targets, vocabulary] BF16 projection.
    It remains in memory and receives a complete BF16 gradient in backward.
    No per-chunk mean, detached cotangent, manual gradient, approximation,
    vocabulary subset, changed target order or alternate model forward exists.
    """
    require(type(gradients) is bool and callable(check), 'explicit mode/check')
    require(logits.ndim == 2 and targets.ndim == 1,
            'ordinary complete selected logits and target vector')
    count = len(targets)
    ranges = chunk_ranges(count)
    require(tuple(logits.shape) == (count, VOCABULARY),
            'full vocabulary and exact target alignment')
    require(logits.dtype == torch.bfloat16 and targets.dtype == torch.long,
            'original BF16 projection and integer target dtypes')
    require(logits.device == targets.device, 'common actual device')
    require(not gradients or (torch.is_grad_enabled() and logits.requires_grad),
            'differentiable full dense projection required')
    require(gradients or not torch.is_grad_enabled(),
            'non-gradient caller must explicitly disable autograd')

    from torch.utils.checkpoint import checkpoint

    def ce_chunk(actual_logits, actual_targets):
        check()
        require(0 < len(actual_targets) <= CHUNK_TOKENS
                and tuple(actual_logits.shape) == (len(actual_targets), VOCABULARY),
                'bounded aligned CE chunk')
        # Exact original expression, applied independently to complete rows.
        return -torch.nn.functional.cross_entropy(
            actual_logits.float(), actual_targets, reduction='none')

    parts = []
    for start, stop in ranges:
        # Actual tensor views are arguments: no late-bound loop closure and no
        # hidden/head projection in the checkpointed/recomputed region.
        actual_logits = logits[start:stop, :]
        actual_targets = targets[start:stop]
        if gradients:
            values = checkpoint(
                ce_chunk, actual_logits, actual_targets,
                use_reentrant=False, preserve_rng_state=True,
                determinism_check='default', early_stop=False)
        else:
            values = ce_chunk(actual_logits, actual_targets)
        require(tuple(values.shape) == (stop - start,) and values.dtype == torch.float32,
                'unreduced FP32 chunk output')
        parts.append(values)
    result = torch.cat(parts, dim=0)
    require(tuple(result.shape) == (count,) and result.dtype == torch.float32,
            'complete ordered FP32 output')
    return result
