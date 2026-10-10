import ast
import copy
import datetime as dt
import hashlib
from pathlib import Path
import time
import types

from causes.pinned_preload import PinnedPreload
from causes import pinned_preload


def reference(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _same(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def _split(raw, filename, namespace):
    tree = ast.parse(raw, filename)
    original = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'compose_reservation')
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    shapes = [ast.If, ast.Assign, ast.If, ast.If, ast.Assign, ast.FunctionDef,
              ast.For, ast.Assign, ast.Assign, ast.For, ast.For, ast.Assign, ast.Return]
    if (len(original.body) != len(shapes)
            or not all(isinstance(n, shape) for n, shape in zip(original.body, shapes))
            or [a.arg for a in original.args.args] != ['config', 'captures', 'frozen_inputs']):
        raise ValueError('unsupported_census_composition_shape')
    count_guard = original.body[3]
    expected = ast.parse("set(captures) != {h['host'] for h in hosts} or len(frozen_inputs) != len(config['frozen_inputs'])", mode='eval').body
    if not _same(count_guard.test, expected):
        raise ValueError('unsupported_census_input_guard')
    frozen_nodes = [n for n in ast.walk(main) if isinstance(n, ast.Assign)
                    and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and n.targets[0].id == 'frozen']
    frozen_expected = ast.parse("frozen = [{'pin': spec['pin'], 'value': _load_pin(spec['pin'], config['max_frozen_bytes'])} for spec in config['frozen_inputs']]").body[0]
    if len(frozen_nodes) != 1 or not _same(frozen_nodes[0], frozen_expected):
        raise ValueError('unsupported_census_frozen_read')
    prefix = copy.deepcopy(original)
    prefix.name = '_prepare_immutable'
    prefix.args = ast.parse('def f(config, frozen_inputs): pass').body[0].args
    early_guard = copy.deepcopy(count_guard)
    early_guard.test = copy.deepcopy(count_guard.test.values[1])
    prefix.body = (copy.deepcopy(original.body[:3]) + [early_guard]
                   + copy.deepcopy(original.body[4:7])
                   + ast.parse('return by_id, locations, provenance').body)
    suffix = copy.deepcopy(original)
    suffix.name = '_finish_composition'
    suffix.args = ast.parse('def f(config, captures, frozen_inputs, prepared): pass').body[0].args
    suffix.body = (copy.deepcopy(original.body[:4])
                   + ast.parse('by_id, locations, provenance = prepared').body
                   + [copy.deepcopy(original.body[5])] + copy.deepcopy(original.body[7:]))
    consumer = copy.deepcopy(main)
    consumer.name = '_consume_main'
    replacements = []

    class MoveFrozenRead(ast.NodeTransformer):
        def visit_Assign(self, node):
            if _same(node, frozen_expected):
                replacements.append(node)
                return ast.copy_location(ast.parse('frozen = _prepared_frozen').body[0], node)
            return self.generic_visit(node)

    consumer = MoveFrozenRead().visit(consumer)
    if len(replacements) != 1:
        raise ValueError('census_frozen_read_not_unique')
    generated = ast.fix_missing_locations(ast.Module(body=[prefix, suffix, consumer], type_ignores=[]))
    exec(compile(generated, filename, 'exec'), namespace)
    frozen_code = compile(ast.fix_missing_locations(ast.Module(
        body=[copy.deepcopy(frozen_nodes[0])], type_ignores=[])), filename, 'exec')
    proof = dict(
        prefix_original_statement_indexes=[0, 1, 2, 4, 5, 6],
        suffix_original_statement_indexes=[0, 1, 2, 3, 5, 7, 8, 9, 10, 11, 12],
        extra_prefix_guard_is_original_frozen_count_operand=True,
        native_main_frozen_assignment_moved_unchanged=True,
        native_main_other_statements_unchanged=True,
        generated_ast_sha256=hashlib.sha256(ast.dump(generated, include_attributes=False).encode()).hexdigest(),
    )
    return frozen_code, proof


class CensusPreload:
    def __init__(self, source, configuration, *, guard_references=()):
        self.source = copy.deepcopy(source)
        self.configuration = copy.deepcopy(configuration)
        self.guards = copy.deepcopy(list(guard_references))
        self.preload = PinnedPreload()
        self._evidence = {}

    def prepare(self):
        started, clock = _stamp(), time.monotonic()

        def build(reader):
            raw = reader(self.source)
            native = types.ModuleType('_pinned_census_composition')
            native.__file__ = self.source['path']
            exec(compile(raw, native.__file__, 'exec'), native.__dict__)

            def bounded(ref, maximum):
                if ref['bytes'] > maximum:
                    raise ValueError('census_file_type_or_size')
                return native.strict_json(reader(ref))

            config = bounded(self.configuration, 8 * 1024**2)
            if (config.get('schema') != native.CONFIG_SCHEMA
                    or config['collector_sha256'] != self.source['sha256']):
                raise ValueError('pinned_census_source_required')
            namespace = dict(native.__dict__)
            frozen_code, proof = _split(raw, native.__file__, namespace)
            loading = dict(namespace, config=config, _load_pin=bounded)
            exec(frozen_code, loading)
            frozen = loading['frozen']
            prepared = namespace['_prepare_immutable'](config, frozen)
            return namespace, [{'pin': row['pin']} for row in frozen], prepared, proof

        self.preload.prepare(build, guard_references=[self.source, self.configuration,
            reference(__file__), reference(pinned_preload.__file__), *self.guards])
        self._evidence['preparation'] = dict(started_at=started, ended_at=_stamp(),
            elapsed_seconds=time.monotonic()-clock, immutable_only=True,
            capture_inputs_read=False, references=list(self.preload.references))

    def execute(self, arguments):
        if (len(arguments) != 7 or arguments[0:2] != ['compose', '--config']
                or arguments[2] != self.configuration['path']
                or arguments[3] != '--captures' or arguments[5] != '--output'):
            raise ValueError('preloaded_compose_arguments_required')
        started, clock = _stamp(), time.monotonic()
        namespace, frozen, prepared, proof = self.preload.take()
        taken = _stamp()
        namespace['_prepared_frozen'] = frozen
        suffix = namespace['_finish_composition']
        namespace['compose_reservation'] = lambda config, captures, inputs: suffix(config, captures, inputs, prepared)
        namespace['_consume_main'](arguments)
        self.preload.finish()
        self._evidence['composition'] = dict(started_at=started, take_checked_at=taken,
            finish_checked_at=_stamp(), elapsed_seconds=time.monotonic()-clock,
            execution_kind='in_process', native_cli_invoked=False,
            native_source=self.source, configuration=self.configuration,
            helper_source=reference(__file__), pinned_preload_source=reference(pinned_preload.__file__),
            split=proof, immutable_finish_verified=True, arguments=list(arguments))

    def evidence(self):
        return copy.deepcopy(self._evidence)
