"""Explicit, byte-identical relocation of a pinned native source dependency.

The caller owns acceptance of the original reference and relocation declaration.
This helper neither changes old references nor falls back to ambient code.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path
            and not any(item.is_symlink() for item in (path, *path.parents)), 'canonical_source_path')
    return path


def checked_pin(ref):
    data = canonical(ref['path']).read_bytes()
    require(len(data) == ref['bytes'] and hashlib.sha256(data).hexdigest() == ref['sha256'], 'pin_changed')
    return data


def physical_source(original, relocation):
    """Verify a declared source copy while preserving its logical original pin."""
    require(set(relocation) == {'original', 'physical'}, 'relocation_fields')
    physical = relocation['physical']
    require(relocation['original'] == original, 'relocation_original_mismatch')
    require(set(original) == set(physical) == {'path', 'bytes', 'sha256'}, 'source_pin_fields')
    require(type(original['bytes']) is int and original['bytes'] >= 0
            and original['bytes'] == physical['bytes'] and original['sha256'] == physical['sha256'],
            'relocation_content_changed')
    old_path = canonical(original['path'])
    new_path = canonical(physical['path'])
    require(old_path != new_path and old_path.suffix == new_path.suffix == '.py', 'explicit_source_relocation_required')
    checked_pin(physical)
    return dict(physical)


def checked_source(original, relocation):
    """Read the declared physical bytes, without rewriting logical evidence."""
    return checked_pin(physical_source(original, relocation))


def context_modules(binding, binding_reference, relocation):
    """Construct the pinned saved-context bundle using only an exact core copy.

    Mirrors the saved-context binder's three native loading operations. All
    functions for jobs and requests remain on the original binder object. The
    imported core's real ``__file__`` is the physical source, never the old path.
    """
    require(canonical(binding.__file__) == Path(binding_reference['path']),
            'context_binding_module_path')
    checked_pin(binding_reference)
    physical = physical_source(binding.KNOWN['core'], relocation)
    parent = binding.module(binding.KNOWN['parent'], 'saved_corpus_reader_parent')
    modules = parent.native_modules()
    modules['runner'] = binding.module(binding.KNOWN['runner'], 'saved_corpus_runner')
    modules['core'] = binding.module(physical, 'saved_corpus_supplement')
    require(canonical(modules['core'].__file__) == Path(physical['path']),
            'relocated_core_module_path')
    checked_pin(physical)
    return modules


def constructor_dependencies(declaration, constructors):
    """Validate v2 copies scoped to exact native constructor/dependency pairs."""
    rows = declaration['dependency_relocations']
    require(type(rows) is list and bool(rows), 'dependency_relocations_required')
    selected = {}
    for row in rows:
        require(type(row) is dict and set(row) == {'constructor', 'relocation'},
                'dependency_relocation_fields')
        constructor, copy = row['constructor'], row['relocation']
        require(type(constructor) is dict and set(constructor) == {'path', 'bytes', 'sha256'}
                and type(constructor['bytes']) is int
                and constructors.get(constructor['path']) == constructor,
                'exact_dependency_constructor')
        checked_pin(constructor)
        physical_source(copy['original'], copy)
        require(type(copy['physical']['bytes']) is int, 'dependency_pin_type')
        key = (constructor['path'], copy['original']['path'])
        require(key not in selected, 'duplicate_dependency_relocation')
        require(copy['original']['path'] not in constructors, 'dependency_is_constructor')
        require(not (constructor == declaration['materializer']
                     and copy['original']['path'] == declaration['relocation']['original']['path']),
                'dependency_overlaps_native_core')
        selected[key] = {name: dict(copy[name]) for name in ('original', 'physical')}
    return selected


@contextmanager
def bind_constructor(root, root_reference, declaration):
    schema = declaration.get('schema')
    fields = {'schema', 'materializer', 'relocation', 'constructors'}
    if schema == 'causes.native_constructor_source_binding.v2':
        fields = fields | {'dependency_relocations'}
    require(set(declaration) == fields,
            'constructor_binding_fields')
    require(schema in ('causes.native_constructor_source_binding.v1',
                       'causes.native_constructor_source_binding.v2'),
            'constructor_binding_schema')
    materializer = declaration['materializer']
    relocation = declaration['relocation']
    physical = physical_source(relocation['original'], relocation)
    constructors = {ref['path']: ref for ref in declaration['constructors']}
    require(len(constructors) == len(declaration['constructors'])
            and constructors.get(materializer['path']) == materializer
            and constructors.get(root_reference['path']) == root_reference,
            'exact_constructor_scope')
    dependencies = (constructor_dependencies(declaration, constructors)
                    if schema == 'causes.native_constructor_source_binding.v2' else {})
    changes = []
    resolved = []

    def bind(module, reference):
        require(constructors.get(reference['path']) == reference
                and canonical(module.__file__) == Path(reference['path']),
                'constructor_source_identity')
        checked_pin(reference)
        require(all(item[0] is not module for item in changes), 'constructor_bound_twice')
        original = module.module
        require(getattr(original, '__globals__', None) is vars(module)
                and canonical(original.__code__.co_filename) == Path(reference['path']),
                'original_native_loader_required')
        if reference == materializer:
            require(module.KNOWN['core'] == relocation['original'], 'native_core_reference_changed')

        def load(ref, name):
            if reference == materializer and ref == relocation['original']:
                selected = physical_source(ref, relocation)
                loaded = original(selected, name)
                require(canonical(loaded.__file__) == Path(physical['path']),
                        'physical_module_identity')
                resolved.append({'original': dict(ref), 'physical': dict(selected)})
                return loaded
            copy = dependencies.get((reference['path'], ref['path'])) if dependencies else None
            if copy is not None:
                require(type(ref.get('bytes')) is int and ref == copy['original'],
                        'dependency_original_mismatch')
                selected = physical_source(ref, copy)
                loaded = original(selected, name)
                require(canonical(loaded.__file__) == Path(selected['path']),
                        'physical_module_identity')
                checked_pin(selected)
                resolved.append({'constructor': dict(reference),
                                 'original': dict(ref), 'physical': dict(selected)})
                return loaded
            loaded = original(ref, name)
            if constructors.get(ref['path']) == ref:
                bind(loaded, ref)
            return loaded

        module.module = load
        changes.append((module, original, load))

    try:
        bind(root, root_reference)
        yield resolved
    finally:
        unchanged = all(module.module is bound for module, _, bound in changes)
        for module, original, _ in reversed(changes):
            module.module = original
        require(unchanged, 'constructor_binding_changed')
