"""Select and verify saved original self probes without running actors or inference.

The caller supplies accepted immutable cuts and source-bound native helpers. The
existing read guard governs their file access; this module has no transport,
parser, model client, file discovery, or measurement-store mutation.
"""

import hashlib
import json
from pathlib import Path

from causes import campaign_measurement_reuse as reuse
from causes import campaign_snapshot as snapshot


SELF = ('evaluation_presence', 'evaluation_strongly_favors', 'evaluation_leans_towards_or_favors')
READOUTS = tuple('self_' + name for name in SELF)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def select_batch(ordered_ids, self_copied, reader_copied, occupied_keys, *,
                 initial_ids=None, batch_size=512, final=False):
    """Choose fixed-order complete copy pairs; never select using probe outcomes."""
    require(type(ordered_ids) is list and ordered_ids and len(set(ordered_ids)) == len(ordered_ids)
            and all(isinstance(x, str) and x for x in ordered_ids), 'fixed_order_required')
    assigned = set(ordered_ids)
    require(type(self_copied) is set and type(reader_copied) is set
            and self_copied <= assigned and reader_copied <= assigned, 'foreign_copy_membership')
    require(type(occupied_keys) is set and type(batch_size) is int and batch_size > 0
            and type(final) is bool, 'batch_policy')
    unoccupied = []
    for run in ordered_ids:
        group = {(run, name) for name in READOUTS}
        overlap = group & occupied_keys
        require(not overlap or overlap == group, 'partial_original_self_group')
        if not overlap:
            unoccupied.append(run)
    available = self_copied & reader_copied
    if initial_ids is not None:
        require(type(initial_ids) is list and initial_ids and len(set(initial_ids)) == len(initial_ids)
                and set(initial_ids) <= assigned and not final, 'initial_batch_membership')
        require(set(initial_ids) <= set(unoccupied), 'initial_batch_already_occupied')
        selected = [run for run in ordered_ids if run in set(initial_ids)]
        if not set(selected) <= available:
            selected = []
    else:
        ready = [run for run in unoccupied if run in available]
        if final:
            require(set(unoccupied) <= available, 'final_copy_pair_incomplete')
            selected = ready
        else:
            selected = ready[:batch_size] if len(ready) >= batch_size else []
    return dict(selected_run_ids=selected,
                pending_copy_ids=[run for run in unoccupied if run not in available],
                ready_unselected_ids=[run for run in unoccupied if run in available and run not in set(selected)],
                already_occupied=len(ordered_ids) - len(unoccupied))


def accepted_snapshots(cut_ref, peer_ref, selection_ref, configuration_ref, *, kind, read, raw, pin):
    """Resolve only named native metadata from an independently accepted copy cut."""
    require(kind in ('self', 'reader'), 'snapshot_kind')
    cut, peer, selection, config = map(read, (cut_ref, peer_ref, selection_ref, configuration_ref))
    flag = ('accepted_requested_actor_self_probe_input_cut' if kind == 'self'
            else 'accepted_requested_actor_reader_input_cut')
    require(peer.get(flag) is True and not peer['findings'] and peer['actual'] == cut_ref
            and peer['selection'] == cut['selection'] == selection_ref
            and peer['configuration'] == cut['configuration'] == configuration_ref
            and peer['index'] == cut['index']['saved']
            and peer['accepted'] == cut['accepted'], 'accepted_copy_cut_join')
    require(config['selected_runs'] == selection['selected_runs']
            and config['destination'] == cut['destination'], 'copy_configuration_join')
    require(config['reader_inputs_only'] is (kind == 'reader')
            and config.get('evidence_profile') == ('early_self_probes' if kind == 'self' else None),
            'copy_profile_changed')
    cfg = snapshot.SnapshotConfig(**dict(config, destination=Path(config['destination']),
          roots=tuple(config['roots']), selected_runs=tuple(config['selected_runs'])))
    cfg.validate()
    body = raw(cut['index']['saved'])
    require((not body or body.endswith(b'\n')) and len(body) == cut['index']['bytes']
            and hashlib.sha256(body).hexdigest() == cut['index']['sha256']
            and cut['index']['original_path'] == str(cfg.destination / cfg.index_name), 'copy_index_changed')
    receipts = [json.loads(line) for line in body.splitlines()]
    require(len(receipts) == cut['accepted'], 'copy_count_changed')
    selected = {row['relative_run']: row for row in selection['rows']}
    require(len(selected) == len(selection['rows']), 'duplicate_selected_relative_run')
    manifests, verified, result = {}, {}, {}
    profile = 'self_probe' if kind == 'self' else 'reader_input'
    for receipt in receipts:
        relative = receipt['run']
        require(relative in selected and receipt['schema'] == 'caus.' + profile + '_receipt.v1'
                and receipt['custody_kind'] == ('actor_self_probe_evidence' if kind == 'self'
                                              else 'actor_reader_inputs'), 'foreign_snapshot_receipt')
        run = selected[relative]['run_id']
        require(run not in result and Path(relative).name == run, 'duplicate_snapshot_run')
        manifest_path = Path(receipt['manifest'])
        attempt = manifest_path.parent
        require(manifest_path.name == 'MANIFEST.json' and attempt.parent == cfg.destination / (profile + '_attempts')
                and Path(receipt['path']) == attempt / 'data' / relative, 'snapshot_attempt_path')
        if manifest_path not in manifests:
            mr = pin(manifest_path)
            manifest = read(mr)
            snapshot.validate_manifest(manifest, cfg, set())
            vr = pin(attempt / 'VERIFIED.json')
            value = read(vr)
            require(value['schema'] == 'caus.' + profile + '_verified.v1', 'verified_profile_changed')
            manifests[manifest_path] = (mr, manifest)
            verified[manifest_path] = (vr, value)
        mr, manifest = manifests[manifest_path]
        vr, value = verified[manifest_path]
        require(receipt['manifest_sha256'] == mr['sha256'] and receipt in value['runs'], 'verified_receipt_join')
        matches = [item for item in manifest['runs'] if item['run'] == relative]
        require(len(matches) == 1, 'snapshot_member_unique')
        item = matches[0]
        require(receipt['files'] == len(item['files']) and receipt['bytes'] == item['bytes']
                and receipt['summary_sha256'] == item['summary_sha256'], 'snapshot_receipt_counts')
        result[run] = dict(run_id=run, manifest=mr, verified=vr, receipt=receipt,
            files={row['path']: dict(path=str(Path(receipt['path']) / row['path']),
                    bytes=row['bytes'], sha256=row['sha256']) for row in item['files']})
    return result


def prepare_groups(selected_ids, jobs, configs, self_snapshots, reader_snapshots, *,
                   self_cut, self_peer, membership_ref, runner_source, read):
    """Project source-bound rows and a finite allowlist before reading any labels."""
    require(type(selected_ids) is list and selected_ids and len(set(selected_ids)) == len(selected_ids),
            'explicit_selected_batch')
    membership = read(membership_ref)
    require(membership['actual'] == self_cut and membership['review'] == self_peer
            and set(membership['terminal_run_ids']) == set(self_snapshots)
            and len(membership['terminal_run_ids']) == len(self_snapshots), 'terminal_copy_membership')
    groups, files, directories = [], {}, set()

    def add(ref):
        require(ref['path'] not in files or files[ref['path']] == ref, 'conflicting_file_pin')
        files[ref['path']] = ref

    for run in selected_ids:
        require(run in self_snapshots and run in reader_snapshots and run in jobs and run in configs,
                'selected_copy_pair_incomplete')
        own, other = self_snapshots[run], reader_snapshots[run]
        job = jobs[run]
        require(job['allocation']['run_id'] == run and job['allocation']['continue_to_end'] is False
                and job['allocation']['instruments'] == list(SELF), 'original_prefix_mask')
        original = dict(own['files'])
        for name in ('allocation.json', 'spec.json', 'variant.json', 'model.json', 'summary.json'):
            require(all(own['files'][name][k] == other['files'][name][k] for k in ('bytes', 'sha256')),
                    'cross_profile_identity_changed')
        for name in ('initial_context.json', 'early_context.json'):
            if name in other['files']:
                original[name] = other['files'][name]
        captures = {name: dict(result=own['files']['probes/early/' + name + '/result.json'])
                    for name in SELF if 'probes/early/' + name + '/result.json' in own['files']}
        root = Path(own['receipt']['path'])
        row = dict(run_id=run, stage='early', source_kind='fresh_original_self', root=str(root),
            job_sha256=hashlib.sha256(encoded(job)).hexdigest(), config_sha256=hashlib.sha256(encoded(read(configs[run]))).hexdigest(),
            instruments=list(SELF), captures=captures, original_refs=original, runner_source=runner_source,
            self_snapshot={k: own[k] for k in ('run_id', 'manifest', 'verified', 'receipt')},
            reader_snapshot={k: other[k] for k in ('run_id', 'manifest', 'verified', 'receipt')},
            custody=dict(actual=self_cut, review=self_peer, accepted_flag='accepted_requested_actor_self_probe_input_cut',
                         membership=membership_ref, closed_run_id=run, terminal=True))
        for value in original.values():
            add(value)
        for evidence in (own, other):
            add(evidence['manifest']); add(evidence['verified'])
        add(configs[run])
        directories.update((str(root / 'probes'), str(root / 'probes' / 'early')))
        groups.append(dict(row=row, config=configs[run]))
    for ref in (self_cut, self_peer, membership_ref, runner_source):
        add(ref)
    return dict(groups=groups, files=list(files.values()), read_directories=sorted(directories))


def bind_batch(groups, jobs, assignments, *, reader, api, runner, evidence, labels, materializer, record):
    """Reuse the original native request reconstruction, auditor, parser and slots."""
    slots, selected = [], []
    for group in groups:
        row = group['row']; run = row['run_id']
        require(run not in selected and run in assignments['epochs'] and run in jobs, 'batch_assignment')
        selected.append(run)
        job, config = jobs[run], record.checked_json(group['config'])
        require(row['instruments'] == list(SELF) and job['allocation']['instruments'] == list(SELF)
                and job['allocation']['continue_to_end'] is False, 'three_original_self_slots_required')
        for ref in row['original_refs'].values():
            record(ref)
        summary = record.checked_json(row['original_refs']['summary.json'])
        require(summary['run_id'] == run and summary['sandbox_closure']['state'] == 'closed'
                and summary['sandbox_closure']['overlay_mounted'] is False, 'stable_terminal_summary_required')
        # No reader aggregate/result is opened. Its snapshot supplies only the
        # exact actor context, including when that context is explicitly absent.
        isolated = reader.original_profile(row, record, evidence)
        context_ref = row['original_refs'].get('early_context.json')
        if context_ref is not None:
            context = record.checked_json(context_ref)
            for question in config['questions']:
                if question['id'] not in SELF:
                    continue
                request_ref = row['original_refs'].get('probes/early/' + question['id'] + '/request.json')
                if request_ref is not None:
                    seed = runner.seed_for(job['allocation']['seed'], ['probe', 'early', question['id']])
                    expected = materializer.request_payload(context, config, seed, question, runner)
                    require(record(request_ref, read=True) == runner.encoded(expected) + b'\n',
                            'exact_original_request_bytes_changed')
        values = api.bind_original_self(row, job=job, config=config, runner=runner,
                    evidence_module=isolated, labels=labels, record=record)
        require([value['readout'] for value in values] == list(READOUTS)
                and all(value['run_id'] == run and value['status'] in ('observed', 'missing') for value in values),
                'native_three_slot_group')
        slots.extend(dict(value, epoch=assignments['epochs'][run]) for value in values)
    reuse.measurement_keys(slots, epochs=assignments['epochs'], readout_names=assignments['readout_names'],
                           require_epoch=True)
    return dict(selected_run_ids=selected, slots=slots,
                counts=dict(occupied=len(slots), observed=sum(s['status'] == 'observed' for s in slots),
                            missing=sum(s['status'] == 'missing' for s in slots)),
                model_calls=0, host_calls=0, analysis_performed=False)
