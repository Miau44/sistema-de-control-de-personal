"""Logical evidence groups: fixed anchor window; never mutate imported events."""
import hashlib
from collections import defaultdict
from datetime import datetime


def boundary_allowance(cx,employee,day,ctx):
    if not ctx['confirmed'] or not ctx['classified'] or ctx['regime_code']=='MOBILE_NO_FIXED_HOURS':return 0
    r=cx.execute('''SELECT boundary_seconds FROM attendance_movement_policies WHERE user_id=? AND valid_from<=?
        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',
        (employee['id'],day.isoformat(),day.isoformat())).fetchone()
    return r[0] if r else 0


def group_events(events,day,rule,confirmed=True):
    buckets=defaultdict(list);single=[]
    for e in events:
        if confirmed and e.get('provenance')=='A_ZLINK_PHYSICAL' and e.get('person_id') and e.get('device_sn'):
            buckets[(e['person_id'],e['device_sn'])].append(e)
        else:single.append([e])
    batches=single
    for key,values in sorted(buckets.items()):
        current=[];anchor=None
        for e in sorted(values,key=lambda e:(e['occurred_at'],str(e.get('original_event_id',e.get('id',''))))):
            stamp=datetime.fromisoformat(e['occurred_at'])
            if not current or (stamp-anchor).total_seconds()>rule['window_seconds']:
                if current:batches.append(current)
                current=[];anchor=stamp
            current.append(e)
        if current:batches.append(current)
    result=[]
    for values in batches:
        refs=[{'provenance':e['provenance'],'event_id':e.get('original_event_id',e.get('id')),
               'request_id':e.get('request_id'),'occurred_at':e['occurred_at']} for e in values]
        first,last=values[0]['occurred_at'],values[-1]['occurred_at']
        identity='|'.join(str(v) for v in (day,rule['id'],values[0].get('person_id'),values[0].get('device_sn'),refs[0]))
        explicit=values[0].get('direction') if len(values)==1 else None
        result.append({'id':hashlib.sha256(identity.encode()).hexdigest()[:24],
            'first_at':first,'last_at':last,'original_count':len(values),
            'original_event_ids':[r['event_id'] for r in refs if r['event_id'] is not None],
            'events':refs,'person_id':values[0].get('person_id'),
            'devices':sorted({e.get('device_sn','') for e in values}),
            'sources':sorted({e.get('source','') for e in values}),
            'provenance':values[0]['provenance'],'rule_version':rule['id'],
            'window_seconds':rule['window_seconds'],'rule':'FIXED_FIRST_EVENT_WINDOW',
            'movement':{'IN':'ENTRADA','OUT':'SALIDA'}.get(explicit,'MOVIMIENTO POR IDENTIFICAR'),
            'representative_at':first if explicit in ('IN','OUT') else None,'policy_id':None})
    return sorted(result,key=lambda g:(g['first_at'],g['id']))


def evaluate_groups(cx,employee,day,ctx,events,authorized):
    rule=dict(cx.execute('SELECT * FROM attendance_grouping_rules WHERE valid_from<=? ORDER BY valid_from DESC,id DESC LIMIT 1',
        (day.isoformat(),)).fetchone())
    groups=group_events(events,day.isoformat(),rule)
    policy=cx.execute('''SELECT * FROM attendance_movement_policies WHERE user_id=? AND valid_from<=?
        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',
        (employee['id'],day.isoformat(),day.isoformat())).fetchone()
    missing=[];configured=bool(policy and ctx['confirmed'] and ctx['classified'] and ctx['segments'] and
        ctx['regime_code']!='MOBILE_NO_FIXED_HOURS')
    if configured:
        boundaries=[]
        for n,(a,b) in enumerate(ctx['segments']):
            boundaries.extend([(n,'ENTRADA',a),(n,'SALIDA',b)])
        # A group must match exactly one boundary, and that boundary exactly one group.
        matches=defaultdict(list);candidates={}
        for g in groups:
            if g['provenance']!='A_ZLINK_PHYSICAL':continue
            choices=[i for i,(_,_,stamp) in enumerate(boundaries) if min(
                abs((datetime.fromisoformat(g[k])-stamp).total_seconds()) for k in ('first_at','last_at'))<=policy['boundary_seconds']]
            candidates[g['id']]=choices
            for i in choices:matches[i].append(g)
        for i,(segment,movement,stamp) in enumerate(boundaries):
            # Authorized time at a boundary removes that obligation, without inventing a stamp.
            covered=any(a<=stamp<b if movement=='ENTRADA' else a<stamp<=b for a,b in authorized)
            if covered:continue
            found=matches[i]
            explicit=[g for g in groups if g['movement']==movement and
                abs((datetime.fromisoformat(g['representative_at'])-stamp).total_seconds())<=policy['boundary_seconds']]
            if len(found)==1 and candidates[found[0]['id']]==[i]:
                g=found[0];g['movement']=movement;g['policy_id']=policy['id'];g['segment']=segment
                g['representative_at']=g['first_at'] if movement=='ENTRADA' else g['last_at']
            elif not explicit:
                missing.append({'movement':movement,'at':stamp.isoformat(' '),'segment':segment,
                    'reason':'Sin grupo inequívoco ni autorización que cubra este extremo de jornada'})
    return {'rule':rule,'groups':groups,'effective_count':len(groups),
        'repeated_groups':sum(g['original_count']>1 for g in groups),
        'extra_events':sum(g['original_count']-1 for g in groups),
        'classification_configured':configured,'missing_movements':missing,
        'policy_id':policy['id'] if configured else None,
        'unidentified_groups':sum(g['movement']=='MOVIMIENTO POR IDENTIFICAR' for g in groups)}
