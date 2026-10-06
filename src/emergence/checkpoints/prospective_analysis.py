"""Pure finite saved-data arithmetic. No I/O, qualification, sampling or inference.

Exact numeric/arm semantics reuse the byte-pinned 1077 implementation. Strict
coefficients are unchanged; this module adds a checked fixed census, marginal
descriptions and explicit partial-failed-gate/nondefined pair handling.
"""
from copy import deepcopy
from fractions import Fraction as F
import hashlib
import json
from emergence.checkpoints import accepted_inference as old

LABELS = old.LABELS
TYPES = tuple('EPGMSCT')
ROW_FIELDS = old.ROW_FIELDS
NONDEFINED = old.NONDEFINED

class InputError(ValueError):
    pass

def need(ok, message):
    if not ok:
        raise InputError(message)

def text(x):
    need(type(x) is str and 0 < len(x) <= 256 and bool(x.strip()), 'bounded nonblank identifier required')
    try:
        x.encode('utf8', 'strict')
    except UnicodeError as exc:
        raise InputError('identifier is not scalar Unicode') from exc
    return x

def number(x):
    try:
        return old.rational(x)
    except old.InputError as exc:
        raise InputError(str(exc)) from exc

def outcome(endpoint, value):
    """An asymmetric failed gate plus an undefined arm stays an undefined pair.

    It preserves the observed per-arm gate zero and the other arm's exact state;
    it neither rejects the whole census nor emits a zero pair/completion range.
    Defined non-gate arms cannot be mixed with a declared failed-work gate.
    """
    try:
        if endpoint == 'P1':
            p, interval, state = old._arm(value, True, endpoint)
            return p, interval, {'scalar': state}
        need(type(value) is dict and set(value) == {'full', 'fresh'}, 'exact P2 arm pair required')
        f, fi, fs = old._arm(value['full'], True, endpoint)
        r, ri, rs = old._arm(value['fresh'], True, endpoint)
    except old.InputError as exc:
        raise InputError(str(exc)) from exc
    if (fs == 'declared_failed_work_gate') != (rs == 'declared_failed_work_gate'):
        other_interval = ri if fs == 'declared_failed_work_gate' else fi
        need(other_interval is None, 'failed-work gate cannot mix with a defined non-gate arm')
    return (None if f is None or r is None else f-r,
            None if fi is None or ri is None else (fi[0]-ri[1], fi[1]-ri[0]),
            {'full': fs, 'fresh': rs})

def prepare(endpoint, cells, census, rows):
    need(type(endpoint) is str and endpoint in ('P1', 'P2'), 'endpoint must be P1 or P2')
    need(type(cells) is dict and 1 <= len(cells) <= 1024, 'bounded fixed cells required')
    weights = {text(k): number(v) for k,v in cells.items()}
    need(all(w >= 0 for w in weights.values()) and sum(weights.values()) == 1, 'fixed nonnegative weights must sum to one')
    need(type(census) is list and len(census) <= 10000, 'bounded frozen census required')
    expected = {}
    sizes = {c: 0 for c in cells}
    for r in census:
        need(type(r) is dict and set(r) == {'original_id','cell_id','bundle_id'}, 'exact frozen census row required')
        for v in r.values(): text(v)
        need(r['original_id'] not in expected and r['cell_id'] in cells, 'duplicate original or unregistered cell')
        expected[r['original_id']] = r
        sizes[r['cell_id']] += 1
    need(all(n == 2 for n in sizes.values()), 'this selected design requires two assigned originals per cell')
    need(type(rows) is list and len(rows) == len(census), 'all-assigned row count required')
    clean, seen = [], set()
    for r in rows:
        need(type(r) is dict and set(r) == ROW_FIELDS, 'exact analysis row fields required')
        oid = text(r['original_id'])
        need(oid in expected and oid not in seen, 'unknown or duplicate analysis original')
        seen.add(oid)
        need(all(r[k] == expected[oid][k] for k in ('cell_id','bundle_id')), 'frozen cell/dependency identity mismatch')
        need(type(r['label']) is str and r['label'] in LABELS, 'unknown measured-class string')
        need(r['selected'] is True and number(r['pi']) == 1, 'all branches are prospectively included; entered is separate')
        point, interval, states = outcome(endpoint, r['outcome'])
        clean.append({**deepcopy(r), 'point':point, 'interval':interval, 'states':states})
    return weights, sizes, clean

def aggregate(clean, coefficients):
    blocked = [{'original_id':r['original_id'],'states':r['states']} for r in clean
               if coefficients[r['original_id']] and r['interval'] is None]
    terms = [{'original_id':r['original_id'],'coefficient':str(coefficients[r['original_id']])} for r in clean]
    if blocked:
        return {'status':'endpoint_not_defined_or_unavailable','point':None,'completion_interval':None,
                'blocked_originals':blocked,'coefficients':terms}
    lo = hi = F(0)
    complete = True
    for r in clean:
        a = coefficients[r['original_id']]
        if a:
            x,y = a*r['interval'][0], a*r['interval'][1]
            lo += min(x,y); hi += max(x,y)
            complete = complete and r['point'] is not None
    return {'status':'finite_measured_corpus','point':str(lo) if complete else None,
            'completion_interval':[str(lo),str(hi)],'blocked_originals':[], 'coefficients':terms}

def calculate(endpoint, cells, census, rows):
    """One fixed checkpoint/phase/focal-type analysis. No caller objects mutate."""
    result = {'schema':'finite-analysis1800.v1','endpoint':endpoint if type(endpoint)is str else None,'status':'invalid_input',
              'strict':None,'marginal':None,'alpha_reserved':'1/40','confidence_interval':None,
              'p_value':None,'MAIN_qualified':False,'source_or_generation_precision_claim':False,
              'label_target':'measured_operational_classes','branch_inclusion_probability':'1'}
    try:
        weights, sizes, clean = prepare(endpoint,cells,census,rows)
    except (InputError,TypeError,KeyError) as exc:
        result['error'] = str(exc)
        return result
    counts = {c:{k:0 for k in LABELS} for c in cells}
    for r in clean: counts[r['cell_id']][r['label']] += 1
    missing = [{'cell_id':c,'label':k} for c,w in weights.items() if w for k in LABELS[:2] if counts[c][k] == 0]
    if missing:
        strict = {'status':'not_estimable','point':None,'completion_interval':None,
                  'unsupported_required_classes':missing,'coefficients':None}
    else:
        coefficients = {}
        for r in clean:
            c,k = r['cell_id'],r['label']
            coefficients[r['original_id']] = (weights[c]/counts[c][k] * (1 if k == 'positive' else -1)) if weights[c] and k in LABELS[:2] else F(0)
        strict = {**aggregate(clean,coefficients),'unsupported_required_classes':[]}
    masses = {k:sum((weights[r['cell_id']]/sizes[r['cell_id']] for r in clean if r['label']==k),F(0)) for k in LABELS}
    composition = {k:{'original_count':sum(r['label']==k for r in clean),'design_mass':str(masses[k]),
                     'cells':{c:{'count':counts[c][k], 'design_mass':str(weights[c]*counts[c][k]/sizes[c]),
                                 'share_within_group':None if not masses[k] else str(weights[c]*counts[c][k]/sizes[c]/masses[k])}
                              for c in cells}} for k in LABELS}
    absent = [k for k in LABELS[:2] if not masses[k]]
    if absent:
        marginal = {'status':'not_estimable','point':None,'completion_interval':None,
                    'unsupported_groups':absent,'coefficients':None}
    else:
        coefficients = {r['original_id']:(weights[r['cell_id']]/sizes[r['cell_id']]/masses[r['label']] * (1 if r['label']=='positive' else -1)) if r['label'] in LABELS[:2] else F(0) for r in clean}
        marginal = {**aggregate(clean,coefficients),'unsupported_groups':[]}
    marginal.update(name='finite_frame_marginal_descriptive_association',group_composition=composition,
                    within_task_standardized=False,primary_replacement=False)
    result.update(status='calculated',strict=strict,marginal=marginal,
                  fixed_cell_weights={c:str(w) for c,w in weights.items()}, all_original_class_counts=counts,
                  all_assigned_count=len(clean),score_states={r['original_id']:r['states'] for r in clean},
                  unknown_ids=[r['original_id']for r in clean if r['label']=='unknown'],
                  other_known_ids=[r['original_id']for r in clean if r['label']=='other_known'])
    return result

def scenarios(endpoint,cells,census,rows,specs,view_eligibility):
    need(type(specs) is list and len(specs)<=32,'bounded named label scenarios required')
    ids={r['original_id']for r in census};names=set();out=[]
    need(type(view_eligibility)is dict and set(view_eligibility)==ids and all(type(v)is bool for v in view_eligibility.values()),'exact fixed view eligibility mask required')
    for s in specs:
        need(type(s)is dict and set(s)=={'scenario_id','labels'},'exact scenario fields required')
        name=text(s['scenario_id']);need(name not in names,'duplicate scenario');names.add(name)
        need(type(s['labels'])is dict and all(k in ids and type(v)is str and v in LABELS for k,v in s['labels'].items()),'scenario labels must bind known originals')
        need(all(view_eligibility[k] or v=='unknown'for k,v in s['labels'].items()),'label scenarios cannot qualify an ineligible view')
        changed=[{**deepcopy(r),'label':s['labels'].get(r['original_id'],r['label'])}for r in rows]
        out.append({'scenario_id':name,'label_overrides':deepcopy(s['labels']),'result':calculate(endpoint,cells,census,changed)})
    return {'status':'label_sensitivity_only','scenarios':out,'confidence_set':False,'original_labels_changed':False}

def fixed_endpoint_mean(weights, endpoint_results, mode):
    need(mode in ('strict','marginal'),'named estimand mode required')
    need(type(weights)is dict and type(endpoint_results)is dict and set(weights)==set(endpoint_results),'exact endpoint union required')
    w={text(k):number(v)for k,v in weights.items()};need(all(x>=0 for x in w.values())and sum(w.values())==1,'fixed endpoint weights')
    bad=[k for k in w if w[k] and (endpoint_results[k].get('status')!='calculated' or endpoint_results[k][mode]['status']!='finite_measured_corpus')]
    if bad:return {'status':'not_estimable','unsupported_endpoints':bad,'point':None,'completion_interval':None,'mode':mode,'confidence_interval':None}
    lo=hi=F(0);complete=True
    for k,v in w.items():
        if not v:continue
        r=endpoint_results[k][mode];lo+=v*F(r['completion_interval'][0]);hi+=v*F(r['completion_interval'][1]);complete=complete and r['point']is not None
    return {'status':'finite_measured_corpus','point':str(lo)if complete else None,'completion_interval':[str(lo),str(hi)],'unsupported_endpoints':[],'mode':mode,'confidence_interval':None}

def project_labels(derived, original_id, view_id, module):
    """Consistency-only projection of an externally authenticated mapper output.

    N2 reuses its complete-original/pre-handoff measurement view, never its P2
    calculator role. The independent pre-update boundary proof remains required.
    """
    need(module in ('P1_N1_shared','P2','N2'),'registered module required')
    need(type(derived)is dict and derived.get('schema')=='traindev.awareness.derived_pair.v2'and derived.get('mapper_version')==2,'accepted mapper version required')
    need(derived.get('original_id')==original_id and derived.get('view_id')==view_id,'exact original/view join')
    expected='p1_first_native_prechoice'if module=='P1_N1_shared'else 'p2_whole_original_prebranch'
    need(derived.get('scope')==expected and derived.get('types')==list(TYPES),'exact accepted measurement scope/types required')
    need(type(derived.get('calculator_view_eligible'))is bool,'explicit view eligibility required')
    need(type(derived.get('paired'))is dict and set(derived['paired'])==set(TYPES),'seven paired types required')
    labels={t:derived['paired'][t]['label']for t in TYPES}
    need(all(type(x)is str and x in LABELS for x in labels.values()),'known class values required')
    eligible=derived['calculator_view_eligible']
    calculator={t:(labels[t]if eligible else 'unknown')for t in TYPES}
    focal='M'if module=='P1_N1_shared'else 'G'if module=='P2'else None
    if focal:need(derived.get('calculator_label')==calculator[focal],'accepted endpoint-gated focal label mismatch')
    raw=json.dumps(derived,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')
    return {'original_id':original_id,'view_id':view_id,'module':module,
            'shared_hypotheses':['P1','N1']if module=='P1_N1_shared'else [module],
            'paired_descriptive_labels':deepcopy(labels),'analysis_labels':calculator,
            'primary_focal_type':focal,'primary_label':None if focal is None else calculator[focal],
            'mapper_result_sha256':hashlib.sha256(raw).hexdigest(),'all_original_mapper_records_retained_separately':True,
            'external_provenance_required':True,'MAIN_qualified':False}
