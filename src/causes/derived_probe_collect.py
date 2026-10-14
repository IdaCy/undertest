"""Verify saved derived-view probe finals without rewriting frozen jobs."""
import copy

from causes import campaign_supplement_collect as collector
from causes import derived_probe_inputs as derived


def verify_final(job, terminal, *, root, row, protocol, record):
    """Rebuild original seed, selected view and protocol around native verification.

    Caller supplies accepted input provenance, terminal journal custody and a
    pin-checking record function. No endpoint is contacted or output written.
    """
    config, _ = derived._protocol(protocol)
    derived.prepare_probe(job, row, protocol, config)
    binding = dict(schema='causes.derived_probe_final_binding.v1',
        config=protocol['config'], context=row['view']['file'],
        provenance=[*row['origin'].values(), *row['provenance'], *protocol['sources'].values()])

    def prepare(expected, contexts, original_config, runner):
        context, question = derived.prepare_probe(expected, row, protocol, original_config)
        return context, question, original_config

    result = collector.verify_final(job, terminal, root=root,
        family=protocol['family'], model=protocol['model'], contexts={},
        config=config, runner=derived.runner, record=record,
        prepare_request=prepare, input_binding=binding)
    # Recheck original and derived inputs after the saved response is parsed.
    derived.prepare_probe(job, row, protocol, config)
    result['derived_view'] = copy.deepcopy(row['view'])
    return result
