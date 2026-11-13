"""Frozen assignment plans and immutable evidence of authority exercised at decision time."""
import json
from organization import VERSION, authority, choose, hr_candidates, supervisor


def plan_request(cx,request_id,manager_id):
    r=cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
    person=cx.execute('SELECT * FROM users WHERE id=?',(r['user_id'],)).fetchone()
    day=r['submitted_at'][:10]
    superior=choose(cx,person,day)
    if superior['id']!=manager_id or superior['id']==person['id']:
        raise ValueError('El responsable no coincide con el superior organizacional vigente.')
    try: _,source=supervisor(cx,person['id'],day)
    except ValueError:
        # Legacy manager-only fixture/setup: explicit single available HR, never a technical administrator.
        if person['role']!='manager' or authority(cx,person['id'],day): raise
        source={'version':VERSION,'legacy_single_hr':manager_id,'provisional':True}
    skip=person['role']!='employee'
    eligible=[superior['id']] if skip else hr_candidates(cx,day)
    eligible=[uid for uid in eligible if uid!=person['id'] and (skip or uid!=manager_id)]
    if not eligible: raise ValueError('No existe autoridad final independiente; requiere definición de RR. HH.')
    plan={'version':VERSION,'request_id':request_id,'requester_id':person['id'],'requester_role':person['role'],
          'superior_id':manager_id,'source':source,'manager_skipped':skip,
          'exception':'SUPERIOR_OPERATIVO_DIRECTO' if skip else None,'hr_ids':eligible,
          'provisional':bool(source.get('authority',{}).get('provisional',True) if source.get('authority') else True)}
    cx.execute('INSERT INTO request_authority_plans(request_id,plan_json,created_at) VALUES(?,?,?)',
               (request_id,json.dumps(plan,ensure_ascii=False),r['submitted_at']))
    return plan


def load_plan(cx,request_id):
    row=cx.execute('SELECT plan_json FROM request_authority_plans WHERE request_id=?',(request_id,)).fetchone()
    return json.loads(row[0]) if row else None


def permitted(cx,actor,r,step):
    plan=load_plan(cx,r['id'])
    if not plan or actor['id']==r['user_id'] or not actor['active']: return False
    from core import now
    a=authority(cx,actor['id'],now().date())
    local=bool(cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone())
    if local and (not a or a['functional_role']!=actor['role']): return False
    if step['assignee_role']=='manager':
        return not plan['manager_skipped'] and actor['role']=='manager' and actor['id']==plan['superior_id']==step['assignee_id']
    if step['assignee_role']!='hr' or actor['role']!='hr' or actor['id'] not in plan['hr_ids']: return False
    # Administration is authorized only for an expressly assigned superior relationship, never the generic HR pool.
    if a and a['authority']=='ADMINISTRACION_OPERATIVA':
        return plan['manager_skipped'] and plan['superior_id']==actor['id']
    if a and a['authority'] not in ('OPERADOR_RRHH','RESPONSABLE_RRHH'): return False
    if not a and cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone(): return False
    return step['assignee_id'] in (None,actor['id'])


def record_decision(cx,actor,r,step,decision,stamp):
    if not permitted(cx,actor,r,step): raise ValueError('No existe autoridad para resolver esta etapa.')
    plan=load_plan(cx,r['id'])
    evidence={'version':VERSION,'actor_id':actor['id'],'actor_role':actor['role'],
              'authority_at_decision':authority(cx,actor['id'],stamp[:10]),
              'request_id':r['id'],'requester_id':r['user_id'],'step_id':step['id'],
              'stage':step['assignee_role'],'step_order':step['step_order'],
              'plan':plan,'decision':decision,'decided_at':stamp,
              'assignment_basis':'FROZEN_REQUEST_PLAN','provisional':plan['provisional']}
    cx.execute('''INSERT INTO approval_authority_evidence(step_id,actor_id,decided_at,decision,evidence_json)
        VALUES(?,?,?,?,?)''',(step['id'],actor['id'],stamp,decision,json.dumps(evidence,ensure_ascii=False)))


def evidence_for(cx,step):
    row=cx.execute('SELECT * FROM approval_authority_evidence WHERE step_id=?',(step['id'],)).fetchone()
    if not row: return None
    evidence=json.loads(row['evidence_json'])
    if (row['actor_id']!=step['assignee_id'] or row['decided_at']!=step['decided_at'] or row['decision']!=step['decision']
        or evidence['stage']!=step['assignee_role'] or evidence['step_order']!=step['step_order']): return None
    return evidence
