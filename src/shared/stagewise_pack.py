"""Substitute one authenticated pack admission without changing later guards.

The caller must authenticate the module, plan, selection and authorization, and
reject existing outputs before calling this function. Use a dedicated process:
temporarily changing a module global is not safe for concurrent dispatch.
"""

from types import FunctionType, ModuleType


class PackAdmissionError(RuntimeError):
    """The expected single initial admission was not followed."""


def pack_with_stage_admission(
    module,
    batch,
    bundle_id,
    *,
    expected_pack,
    expected_guard,
    expected_initial_bytes,
    compressed_limit_bytes,
):
    """Call the exact pack function once, replacing only its first guard input.

    The first call must be ``host_peak_guard(expected_initial_bytes)`` with no
    keyword arguments. Restore the original global before invoking that guard
    with ``compressed_limit_bytes``. A subsequent writer therefore captures the
    original guard. Saved references to the interceptor cannot substitute twice.

    Ordinary returns are accepted only after exactly one valid interception and
    with the original global still installed. Original body/guard exceptions,
    including control signals, propagate unchanged; restoration also runs then.
    This function neither retries nor removes any output after failure.
    """
    if (
        type(module) is not ModuleType
        or type(expected_pack) is not FunctionType
        or type(expected_guard) is not FunctionType
        or type(batch) is not str
        or not batch
        or type(bundle_id) is not str
        or not bundle_id
        or type(expected_initial_bytes) is not int
        or type(compressed_limit_bytes) is not int
        or not 0 < compressed_limit_bytes < expected_initial_bytes
    ):
        raise PackAdmissionError("invalid pack admission contract")
    namespace = vars(module)
    if (
        namespace.get("pack") is not expected_pack
        or namespace.get("host_peak_guard") is not expected_guard
        or expected_pack.__globals__ is not namespace
        or expected_guard.__globals__ is not namespace
        or expected_pack.__name__ != "pack"
        or expected_guard.__name__ != "host_peak_guard"
    ):
        raise PackAdmissionError("pack or guard identity changed")

    interceptions = 0
    refused = False

    def first_guard(*args, **kwargs):
        nonlocal interceptions, refused
        interceptions += 1
        correctly_routed = namespace.get("host_peak_guard") is first_guard
        namespace["host_peak_guard"] = expected_guard
        if (
            interceptions != 1
            or not correctly_routed
            or len(args) != 1
            or type(args[0]) is not int
            or args[0] != expected_initial_bytes
            or kwargs
        ):
            refused = True
            raise PackAdmissionError("unexpected initial pack guard call")
        try:
            return expected_guard(compressed_limit_bytes)
        except BaseException:
            refused = True
            raise

    namespace["host_peak_guard"] = first_guard
    try:
        result = expected_pack(batch, only=bundle_id)
        if (
            interceptions != 1
            or refused
            or namespace.get("host_peak_guard") is not expected_guard
        ):
            raise PackAdmissionError("pack guard dispatch did not complete exactly once")
        return result
    finally:
        namespace["host_peak_guard"] = expected_guard
