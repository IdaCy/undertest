"""Source structure and renaming signatures; equal signatures are candidates, not proof of equivalence."""

from collections import Counter, defaultdict
import hashlib
import json

def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, ensure_ascii=True).encode()


def digest(value):
    return sha(canonical(value))


def problem_structure(model, mapping=None):
    mapping = mapping or {obj: obj for obj in model['objects']}
    def facts(items):
        return sorted((f[0], *(mapping[x] for x in f[1:])) for f in items)
    return {'objects': sorted((mapping[k], v) for k, v in model['objects'].items()), 'initial': facts(model['initial']), 'goals': facts(model['goals']), 'domain_sha256': model['source_sha256']['domain']}


def declared_alpha(model):
    counts = Counter()
    mapping = {}
    for obj, kind in model['objects'].items():
        mapping[obj] = kind + '__' + str(counts[kind])
        counts[kind] += 1
    return digest(problem_structure(model, mapping)), mapping


def refine(model):
    """16-round object-color refinement; equality is only an isomorphism candidate."""
    objects = model['objects']
    facts = [('initial', f) for f in model['initial']] + [('goal', f) for f in model['goals']]
    incidents = defaultdict(list)
    for channel, fact in facts:
        for position, obj in enumerate(fact[1:]):
            incidents[obj].append((channel, fact, position))
    colors = {obj: digest(['type', kind]) for obj, kind in objects.items()}
    for _ in range(16):
        next_colors = {}
        for obj in objects:
            neighbors = []
            for channel, fact, position in incidents[obj]:
                # Equality among arguments must survive renaming.
                equality = [i for i, value in enumerate(fact[1:]) if value == obj]
                neighbors.append([channel, fact[0], position, equality, [colors[x] for x in fact[1:]]])
            next_colors[obj] = digest([colors[obj], sorted(neighbors)])
        colors = next_colors
    signature = digest({'domain_sha256': model['source_sha256']['domain'], 'object_colors': sorted(colors.values()), 'facts': sorted([channel, fact[0], [colors[x] for x in fact[1:]]] for channel, fact in facts)})
    return signature, colors
