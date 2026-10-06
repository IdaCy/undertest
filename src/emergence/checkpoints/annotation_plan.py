"""Metadata-only deterministic reader batches; never reads or commissions views.

pairing_target_codepoints is an explicit prospective readability choice. It is
not a context limit or grant. Over-target singletons require a sized root
envelope, not automatic exclusion. The mapper's actual 1 MiB packet interface
is separately recorded and cannot be silently enlarged by this planner.
"""
import hashlib

FIELDS = {'original_id','view_id','module','phase','checkpoint_id','dependency_component',
          'packet_sha256','packet_bytes','packet_codepoints','available'}
MODULES = {'P1_N1_shared','P2','N2'}

def require(ok, reason):
    if not ok: raise ValueError(reason)

def plan(views, control_codepoints, pairing_target_codepoints):
    require(type(views)is list and len(views)<=1664,'bounded assigned view census required')
    require(type(control_codepoints)is int and control_codepoints>=0,'measured control/wrapper codepoints required')
    require(type(pairing_target_codepoints)is int and pairing_target_codepoints>0,'explicit prospective pairing target required')
    seen_originals=set();seen_views=set();groups={};unready=[]
    for r in views:
        require(type(r)is dict and set(r)==FIELDS,'exact view metadata required')
        for k in ('original_id','view_id','phase','checkpoint_id','dependency_component'):
            require(type(r[k])is str and bool(r[k].strip()) and len(r[k])<=256,'bounded metadata identity')
            r[k].encode('utf8','strict')
        require(r['module']in MODULES and r['phase']in ('MAIN','FRESH_CONFIRMATION'),'registered role required')
        require(r['original_id']not in seen_originals and r['view_id']not in seen_views,'one shared P1/N1 view per original; no duplicate view')
        seen_originals.add(r['original_id']);seen_views.add(r['view_id'])
        require(type(r['available'])is bool,'explicit source availability required')
        if not r['available']:
            require(all(r[k]is None for k in ('packet_sha256','packet_bytes','packet_codepoints')),'unavailable view must not claim packet bytes')
            unready.append({'view_id':r['view_id'],'reason':'authentic_packet_unavailable','assigned_first_reader_slots':2})
            continue
        require(type(r['packet_sha256'])is str and len(r['packet_sha256'])==64 and all(c in '0123456789abcdef'for c in r['packet_sha256']),'exact packet digest required')
        require(type(r['packet_bytes'])is int and r['packet_bytes']>0 and type(r['packet_codepoints'])is int and 0<r['packet_codepoints']<=r['packet_bytes'],'exact measured packet size required')
        if r['packet_bytes']>1048576:
            unready.append({'view_id':r['view_id'],'reason':'exceeds_actual_accepted_mapper_packet_interface','assigned_first_reader_slots':2,'before_dispatch_action':'Retain complete bytes; require a separately reviewed compatible interface or retain unqualified. Do not truncate.'})
            continue
        groups.setdefault((r['phase'],r['checkpoint_id']),[]).append(r)
    batches=[]
    for key in sorted(groups):
        pending=sorted(groups[key],key=lambda r:hashlib.sha256(('analysis1800|'+r['view_id']).encode()).hexdigest())
        while pending:
            first=pending.pop(0);batch=[first]
            for i,candidate in enumerate(pending):
                if candidate['dependency_component']!=first['dependency_component'] and control_codepoints+first['packet_codepoints']+candidate['packet_codepoints']<=pairing_target_codepoints:
                    batch.append(pending.pop(i));break
            view_ids=[r['view_id']for r in batch]
            chars=control_codepoints+sum(r['packet_codepoints']for r in batch)
            batches.append({'batch_id':'B-'+hashlib.sha256('|'.join(view_ids).encode()).hexdigest()[:20],
                            'phase':key[0],'checkpoint_id':key[1],'reader1_order':view_ids,
                            'reader2_order':list(reversed(view_ids)),
                            'packet_sha256s':{r['view_id']:r['packet_sha256']for r in batch},
                            'input_codepoints_including_controls':chars,
                            'singleton_exceeds_pairing_target':len(batch)==1 and chars>pairing_target_codepoints,
                            'root_size_transport_envelope_required':True,'commissioned':False})
    return {'schema':'annotation1800.batches.v1','assigned_original_views':len(views),
            'assigned_initial_reader_slots':2*len(views),'ready_metadata_views':sum(len(b['reader1_order'])for b in batches),
            'proposed_initial_commissions':2*len(batches),'maximum_conditional_third_views':len(views),
            'pairing_target_codepoints':pairing_target_codepoints,'target_is_backend_capacity':False,
            'batches':batches,'unready_views':unready,'model_or_api_calls':0,'issuance_authority':False}
