import unittest
from types import ModuleType

from shared.stagewise_pack import PackAdmissionError, pack_with_stage_admission


GUARD = """
def host_peak_guard(additional_bytes, destination=None):
    events.append(('guard', additional_bytes, destination))
    if host_peak_guard is not original_guard:
        raise AssertionError('guard was not restored before invocation')
    required = floor + additional_bytes + margin
    if free < required:
        raise RuntimeError('floor refused')
    return required
"""


def fixture(body):
    module = ModuleType("synthetic_archive")
    module.events = []
    module.floor = 45 * 1024**3
    module.margin = 64 * 1024**2
    module.initial = 7008509952 - module.margin
    module.compressed = 2336169984
    module.free = module.floor + module.compressed + module.margin
    exec(GUARD, vars(module))
    module.original_guard = module.host_peak_guard
    exec("def pack(batch, only=None):\n" + "\n".join("    " + line for line in body.splitlines()), vars(module))
    return module


class StagewisePackTests(unittest.TestCase):
    def invoke(self, module, **changes):
        values = dict(
            expected_pack=module.pack,
            expected_guard=module.original_guard,
            expected_initial_bytes=module.initial,
            compressed_limit_bytes=module.compressed,
        )
        values.update(changes)
        return pack_with_stage_admission(module, "batch", "bundle", **values)

    def test_only_initial_guard_changes_and_writer_captures_original(self):
        m = fixture("events.append(('pack', batch, only))\n"
                    "first = host_peak_guard(initial)\n"
                    "writer_guard = host_peak_guard\n"
                    "events.append(('writer_original', writer_guard is original_guard))\n"
                    "writer_guard(17)\nreturn first")
        self.assertEqual(self.invoke(m), m.free)
        self.assertEqual(m.events, [('pack', 'batch', 'bundle'),
                                  ('guard', m.compressed, None),
                                  ('writer_original', True), ('guard', 17, None)])
        self.assertIs(m.host_peak_guard, m.original_guard)
        with self.assertRaisesRegex(RuntimeError, 'floor refused'):
            m.host_peak_guard(m.initial)

    def test_exact_boundary_and_one_byte_below(self):
        for difference in (0, -1):
            with self.subTest(difference=difference):
                m = fixture("host_peak_guard(initial)\nevents.append('opened')")
                m.free += difference
                if difference:
                    with self.assertRaisesRegex(RuntimeError, 'floor refused'):
                        self.invoke(m)
                    self.assertNotIn('opened', m.events)
                else:
                    self.invoke(m)
                    self.assertIn('opened', m.events)
                self.assertIs(m.host_peak_guard, m.original_guard)

    def test_future_guard_uses_full_original_arguments(self):
        m = fixture("host_peak_guard(initial)\nhost_peak_guard(initial, destination='future')")
        with self.assertRaisesRegex(RuntimeError, 'floor refused'):
            self.invoke(m)
        self.assertEqual(m.events[-1], ('guard', m.initial, 'future'))
        self.assertIs(m.host_peak_guard, m.original_guard)

    def test_wrong_initial_arguments_fail_before_original_guard(self):
        for expression in ("initial-1", "True", "float(initial)", "initial, None",
                           "additional_bytes=initial", "initial, destination=None", ""):
            with self.subTest(expression=expression):
                m = fixture(f"host_peak_guard({expression})\nevents.append('opened')")
                with self.assertRaises(PackAdmissionError):
                    self.invoke(m)
                self.assertEqual(m.events, [])
                self.assertIs(m.host_peak_guard, m.original_guard)

    def test_missing_interception_refuses(self):
        m = fixture("return 42")
        with self.assertRaises(PackAdmissionError):
            self.invoke(m)
        self.assertIs(m.host_peak_guard, m.original_guard)

    def test_saved_interceptor_cannot_substitute_twice_even_if_error_caught(self):
        for catch in (False, True):
            with self.subTest(catch=catch):
                body = "saved = host_peak_guard\nsaved(initial)\n"
                body += "try:\n    saved(initial)\nexcept RuntimeError:\n    pass" if catch else "saved(initial)"
                m = fixture(body)
                with self.assertRaises(PackAdmissionError):
                    self.invoke(m)
                self.assertEqual(m.events, [('guard', m.compressed, None)])
                self.assertIs(m.host_peak_guard, m.original_guard)

    def test_caught_wrong_first_call_cannot_appear_successful(self):
        m = fixture("try:\n    host_peak_guard(initial-1)\nexcept RuntimeError:\n    pass\nreturn 42")
        with self.assertRaises(PackAdmissionError):
            self.invoke(m)
        self.assertEqual(m.events, [])

    def test_caught_original_guard_failure_cannot_appear_successful(self):
        m = fixture("try:\n    host_peak_guard(initial)\nexcept RuntimeError:\n    pass\nreturn 42")
        m.free -= 1
        with self.assertRaises(PackAdmissionError):
            self.invoke(m)
        self.assertIs(m.host_peak_guard, m.original_guard)

    def test_rerouted_global_is_refused_and_restored(self):
        for when in ('before', 'after'):
            with self.subTest(when=when):
                body = "global host_peak_guard\nsaved = host_peak_guard\n"
                body += ("host_peak_guard = lambda *a: None\nsaved(initial)" if when == 'before'
                         else "saved(initial)\nhost_peak_guard = lambda *a: None")
                m = fixture(body)
                with self.assertRaises(PackAdmissionError):
                    self.invoke(m)
                self.assertIs(m.host_peak_guard, m.original_guard)

    def test_guard_exception_object_and_control_signals_propagate(self):
        for error in (RuntimeError('guard'), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(error=type(error).__name__):
                m = fixture("host_peak_guard(initial)\nevents.append('opened')")
                m.error = error
                exec("def host_peak_guard(additional_bytes, destination=None):\n"
                     "    assert host_peak_guard is original_guard\n"
                     "    events.append(additional_bytes)\n    raise error", vars(m))
                m.original_guard = m.host_peak_guard
                with self.assertRaises(BaseException) as raised:
                    self.invoke(m)
                self.assertIs(raised.exception, error)
                self.assertEqual(m.events, [m.compressed])
                self.assertIs(m.host_peak_guard, m.original_guard)

    def test_body_exception_before_or_after_admission_is_not_replaced(self):
        for prefix in ('', 'host_peak_guard(initial)\n'):
            for error in (RuntimeError('body'), KeyboardInterrupt(), SystemExit(9)):
                with self.subTest(prefix=prefix, error=type(error).__name__):
                    m = fixture(prefix + "raise error")
                    m.error = error
                    try:
                        self.invoke(m)
                    except BaseException as actual:
                        self.assertIs(actual, error)
                        names = []
                        tb = actual.__traceback__
                        while tb:
                            names.append(tb.tb_frame.f_code.co_name)
                            tb = tb.tb_next
                        self.assertEqual(names[-1], 'pack')
                    else:
                        self.fail('body error suppressed')
                    self.assertIs(m.host_peak_guard, m.original_guard)

    def test_dispatch_identity_and_invalid_contract_refuse_without_execution(self):
        m = fixture("events.append('pack')\nhost_peak_guard(initial)")
        foreign = fixture("host_peak_guard(initial)")
        for changes in ({'expected_pack': foreign.pack}, {'expected_guard': foreign.original_guard},
                        {'expected_initial_bytes': True}, {'compressed_limit_bytes': 0},
                        {'compressed_limit_bytes': m.initial}, {'compressed_limit_bytes': 1.5}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(PackAdmissionError):
                    self.invoke(m, **changes)
                self.assertEqual(m.events, [])
                self.assertIs(m.host_peak_guard, m.original_guard)


if __name__ == '__main__':
    unittest.main()
