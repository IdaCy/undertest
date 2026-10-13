"""Copy original early self probes for an already registered finite actor roster."""

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types

from causes import campaign_selected_reader_feed as reader


def validate_registration(registration, read=reader.checked):
    """Keep the reviewed producer graph; change only the local evidence profile."""
    r = registration
    reader.require(r['schema'] == 'causes.selected_actor_self_feed_registration.v1',
                   'self_registration_schema')
    parent = read(r['reader_registration'])
    original = reader.validate_registration(parent, read)
    config = read(r['configuration'])
    reader.require(r['selection'] == parent['selection']
                   and r['execution_host'] == original['host']
                   and r['remote_base'] == original['remote_base'], 'self_parent_registration_join')
    expected = dict(original, destination=r['destination'], reader_inputs_only=False,
                    evidence_profile='early_self_probes')
    reader.require(reader.encoded(config) == reader.encoded(expected)
                   and config['destination'] != original['destination'], 'self_configuration')
    reader.require(r['self_probe_custody'] == dict(originals_retained_on_producer=True,
                   local_copy_included=True, evidence_profile='early_self_probes'), 'self_custody_scope')
    return config


def load(plan_ref, review_ref, adoption_ref):
    captured = {}

    def read(reference):
        value = reader.checked(reference)
        previous = captured.setdefault(reference['path'], dict(reference))
        reader.require(previous == reference, 'conflicting_reference')
        return value

    p, review, adoption = map(read, (plan_ref, review_ref, adoption_ref))
    reader.require(p['schema'] == 'causes.selected_actor_self_feed_plan.v1'
                   and p['runtime_authorized'] is True
                   and p['producer_plan_binding_complete'] is True, 'pending_or_untyped_self_plan')
    reader.require(type(p['maximum_local_copy_bytes']) is int
                   and 2 * 1024**3 < p['maximum_local_copy_bytes'] <= 10_000_000_000,
                   'invalid_self_copy_budget')
    reader.require(p['source'] == reader.pin(__file__)
                   and p['reader_feed_source'] == reader.pin(reader.__file__)
                   and review.get('accepted_selected_actor_self_feed_preparation') is True
                   and review['plan'] == plan_ref and review['source'] == p['source']
                   and not review['findings'], 'self_feed_source_review')
    reader.require(adoption['root_authorized'] is True and adoption['no_user_revocation'] is True
                   and adoption['plan'] == plan_ref and adoption['review'] == review_ref
                   and adoption['maximum_invocations'] == 1 and adoption['automatic_retry'] is False,
                   'self_copy_authority')
    registration = read(p['registration'])
    native = validate_registration(registration, read)
    reader.require(p['configuration'] == registration['configuration']
                   and p['remote_base'] == native['remote_base']
                   and p['destination'] == native['destination'], 'self_feed_configuration_join')
    parent_ref = p['transport_parent']
    reader.require(reader.pin(parent_ref['path']) == parent_ref, 'transport_parent_changed')
    spec = importlib.util.spec_from_file_location('selected_self_feed_native_parent', parent_ref['path'])
    parent = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = parent
    spec.loader.exec_module(parent)
    h, snapshot = parent.h, parent.snapshot
    for ref in p['sources'].values():
        h.checked_bytes(ref)
    reader.require(p['sources']['handoff'] == h.pin(h.__file__)
                   and p['sources']['snapshot'] == h.pin(snapshot.__file__), 'native_source_identity')
    spec = importlib.util.spec_from_file_location('selected_self_feed_native_watch', p['sources']['watch']['path'])
    watch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watch)
    reader.require(watch.snapshot is snapshot and p['interval_seconds'] == 180, 'native_watch_binding')
    config = snapshot.SnapshotConfig(**dict(native, destination=Path(native['destination']),
            roots=tuple(native['roots']), selected_runs=tuple(native['selected_runs'])))
    config.validate()
    watch_root = snapshot.canonical_path(p['watch_directory'])
    reader.require(watch_root != config.destination
                   and not watch_root.is_relative_to(config.destination)
                   and not config.destination.is_relative_to(watch_root), 'overlapping_self_copy_roots')
    for ref in [p['source'], p['reader_feed_source'], parent_ref, p['ssh_config'], *p['sources'].values()]:
        previous = captured.setdefault(ref['path'], dict(ref))
        reader.require(previous == ref, 'conflicting_reference')
    refs = list(captured.values())

    def authorize(actual):
        reader.require(actual == config, 'native_configuration_changed')
        reader.check_local_budget([config.destination, watch_root], p['maximum_local_copy_bytes'])
        for ref in refs:
            h.checked_bytes(ref)
        return dict(host=config.host, remote_base=config.remote_base, roots=list(config.roots),
                    selected_runs=list(config.selected_runs), evidence=refs,
                    custody_kind='actor_self_probe_evidence',
                    scope='Finite original early self-probe evidence and five identity files only. '
                          'No generation, model call, workspace, checkpoint or analysis is authorized.')

    snapshot.checked_authorization(config, authorize)
    return p, config, watch, authorize, parent


def main(plan_ref, review_ref, adoption_ref):
    p, config, watch, authorize, parent = load(plan_ref, review_ref, adoption_ref)
    reader.require(not config.destination.exists() and not Path(p['watch_directory']).exists(),
                   'fresh_snapshot_namespace_required')
    snapshot = parent.snapshot
    native_subprocess = snapshot.subprocess

    def run(argv, **kwargs):
        parent.h.checked_bytes(p['ssh_config'])
        command = parent.strict_transport(argv, p)
        reader.check_local_budget([config.destination, p['watch_directory']], p['maximum_local_copy_bytes'])
        return subprocess.run(command, **kwargs)

    snapshot.subprocess = types.SimpleNamespace(run=run)
    try:
        result = watch.watch_completed(config, authorize, Path(p['watch_directory']),
                                       interval_seconds=p['interval_seconds'])
        print(json.dumps(result, sort_keys=True))
    finally:
        snapshot.subprocess = native_subprocess


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('plan', 'review', 'adoption'):
        parser.add_argument('--' + name, required=True, type=json.loads)
    args = parser.parse_args()
    main(args.plan, args.review, args.adoption)
