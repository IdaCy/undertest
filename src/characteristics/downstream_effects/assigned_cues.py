"""Tri-state summary of an already validated assigned cue/behavior frame."""



def summary(group, metric):
    n = len(group)
    true = sum(r[metric] is True for r in group)
    false = sum(r[metric] is False for r in group)
    unknown = sum(r[metric] is None for r in group)
    assert true + false + unknown == n
    return {'assigned': n, 'true': true, 'false': false, 'unknown': unknown, 'pending': 0,
            'lower': true / n, 'upper': (true + unknown) / n}
