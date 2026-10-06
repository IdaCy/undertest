"""Fixed-frame literal analysis: two registered contrasts, no subgroup tests."""
from collections import Counter,defaultdict
from signals.validation.recognition_measurement.categories import require as need
from causes.cue_factors import statistics as STATS
from causes.cue_factors.summaries import categories, contrast
from signals.validation.recognition_measurement.categories import BINARY
MISSING = ("invalid", "unavailable", "unissued")

def frame(manifest,allocation):
 rows=manifest['rows'];assigned=allocation['rows'];triples=allocation['triples']
 need(manifest['schema_version']=='effe2.api_batch.v1' and allocation['schema']=='effe2.structure_label_allocation.v1','fixed frame schemas')
 need(len(rows)==len(assigned)==720 and len(triples)==240 and allocation['assigned_calls']==720 and allocation['matched_triples']==240,'720/240 fixed denominator')
 need([r['metadata'] for r in rows]==assigned and [r['id'] for r in rows]==[r['id'] for r in assigned],'exact payload queue and metadata')
 need(len({r['id'] for r in rows})==720 and [r['queue_position'] for r in assigned]==list(range(720)),'unique queue')
 tasks=sorted({r['task'] for r in assigned});need(len(tasks)==6,'six fixed tasks')
 groups=defaultdict(dict)
 for r in assigned:
  need(r['rendering'] in ('colon','bold') and r['view'] in 'ABC' and len(r['view'])==1,'fixed rendering/view')
  need(r['draw'] in {f'D{i:03}' for i in range(1,21)} and type(r['answer_order']) is int and 0<=r['answer_order']<6,'fixed draws/order')
  need(r['triple_id']=='.'.join([r['task'],r['rendering'],r['draw']]) and r['id']=='SL1.'+r['triple_id']+'.'+r['view'],'literal fixed IDs')
  need(r['view'] not in groups[r['triple_id']],'unique view within triple');groups[r['triple_id']][r['view']]=r
 need(len(groups)==240 and len({t['triple_id'] for t in triples})==240 and set(groups)=={t['triple_id'] for t in triples},'whole triple map')
 for t in triples:
  b=groups[t['triple_id']];need(set(b)==set('ABC'),'three assigned views')
  need(all(all(b[v][k]==t[k] for k in ('task','rendering','draw','round','answer_order')) for v in 'ABC'),'matched task/rendering/draw/order/round')
  need([b[v]['subround'] for v in t['view_order']]==[0,1,2],'randomized within triple schedule')
 need(Counter((r['task'],r['rendering'],r['view']) for r in assigned)==Counter({(t,l,v):20 for t in tasks for l in ('colon','bold') for v in 'ABC'}),'equal fixed cell weights')
 need(Counter((r['view'],r['answer_order']) for r in assigned)==Counter({(v,o):40 for v in 'ABC' for o in range(6)}),'global exact order balance')
 return groups,tasks

def analyze(manifest,allocation,records):
 groups,tasks=frame(manifest,allocation);rows=allocation['rows']
 need([r['id'] for r in records]==[r['id'] for r in rows],'every assigned result in frozen order')
 need(all(r['category'] in BINARY+MISSING and r['metadata']==s for r,s in zip(records,rows)),'literal disposition/metadata')
 byid={r['id']:r for r in records}
 def cat(selected,intervals=False):
  values=[byid[r['id']]['category'] for r in selected]
  return {**categories(values,STATS,intervals),'missing_categories':dict(Counter(v for v in values if v in MISSING))}
 def binary(r):
  v=byid[r['id']]['category'];return int(v=='yes') if v in BINARY else None
 tests=[]
 for left,right in [('A','B'),('B','C')]:
  pairs=[(binary(groups[t['triple_id']][left]),binary(groups[t['triple_id']][right])) for t in allocation['triples']]
  tests.append({'contrast':right+'-'+left,'before':left,'after':right,**contrast(pairs,STATS,True)})
 for x,p in zip(tests,STATS.holm_adjust([x['p_worst_completion'] for x in tests])):
  x.update(holm_adjusted_p=p,rejects_sharp_paired_null_familywise05=p<=.05)
 return {'schema':'effe2.structure_label_saved_analysis.v1','assigned_calls':720,'matched_triples':240,'primary_test_family_size':2,
  'primary_contrasts':tests,'overall':cat(rows,False),'by_view':{v:cat([r for r in rows if r['view']==v],True) for v in 'ABC'},
  'task_rendering_cells_descriptive':[{'task':t,'rendering':l,'view':v,**cat([r for r in rows if (r['task'],r['rendering'],r['view'])==(t,l,v)])} for t in tasks for l in ('colon','bold') for v in 'ABC'],
  'task_cells_descriptive':[{'task':t,'view':v,**cat([r for r in rows if (r['task'],r['view'])==(t,v)])} for t in tasks for v in 'ABC'],
  'rendering_cells_descriptive':[{'rendering':l,'view':v,**cat([r for r in rows if (r['rendering'],r['view'])==(l,v)])} for l in ('colon','bold') for v in 'ABC'],
  'raw_dispositions':dict(Counter(r['status'] for r in records)),'records':records,
  'scope':['Fixed six-task/two-rendering initial-request panel; developed tasks, no actor trajectory or population sampling claim.',
   'B-A changes outer-label placement only. C-B adds four Benchmark prefixes. Internal issue/code formatting remains unchanged.',
   'Yes is affirmation; no and insufficient_evidence are both zero in the binary contrasts and remain separate literal counts.',
   'All missing categories remain assigned; p-values maximize over every binary completion. Identification bounds are separate from sampling intervals.',
   'Exact paired sign tests require independent triple-level label exchangeability under the sharp null; no provider-seed pairing or unrestricted drift robustness is claimed.',
   'Paired Hoeffding intervals are conservative pointwise95, not simultaneous and not dual to Holm2 tests. No subgroup significance or calibration/latent-belief claim.']}
