"""Validate effective approval history, never infer authority from an Excel annotation."""


def authorization(cx, request):
    from approval_authority import load_plan, evidence_for
    r=dict(request)
    plan=load_plan(cx,r['id'])
    steps=[dict(s) for s in cx.execute('''SELECT s.* FROM request_approval_steps s
        WHERE s.request_id=? ORDER BY step_order''',(r['id'],))]
    result={'valid':False,'status':r['status'],'stage':'SOLICITADA','errors':[],
            'request_id':r['id'],'folio':r['folio'],'requester_id':r['user_id'],'steps':steps}
    if r['status']=='RECHAZADA':result['stage']='RECHAZADA';return result
    if r['status']!='APROBADA':
        pending=next((s for s in steps if not s['is_skipped'] and s['decision'] is None),None)
        result['stage']='PENDIENTE_RRHH' if pending and pending['assignee_role']=='hr' else 'PENDIENTE_ENCARGADO' if pending else 'SOLICITADA'
        return result
    errors=result['errors']
    if not plan: errors.append('Falta evidencia verificable de autoridad historica.')
    elif plan['requester_id']!=r['user_id'] or plan['superior_id']!=r['approver_id']:
        errors.append('La solicitud no coincide con el plan de autoridad original.')
    manager=next((s for s in steps if s['assignee_role']=='manager'),None)
    hr=next((s for s in steps if s['assignee_role']=='hr'),None)
    if not manager or not hr:errors.append('Falta historial completo de encargado y RR. HH.')
    for s in steps:
        if not s['is_skipped']:
            if s['decision']!='APROBADA' or not s['decided_at'] or not s['assignee_id']:
                errors.append('Etapa obligatoria sin aprobación y responsable.')
            if s['assignee_id']==r['user_id']:errors.append('Autoaprobación no permitida.')
            evidence=evidence_for(cx,s)
            if not evidence or evidence['requester_id']!=r['user_id'] or evidence['plan']!=plan:
                errors.append('Falta evidencia de autoridad al decidir.')
            elif ((s['assignee_role']=='manager' and (evidence['actor_role']!='manager' or s['assignee_id']!=plan['superior_id']))
                  or (s['assignee_role']=='hr' and (evidence['actor_role']!='hr' or s['assignee_id'] not in plan['hr_ids']))):
                errors.append('Autoridad historica no autorizada en la etapa.')
            if s['decided_at'] and s['decided_at']<r['submitted_at']:
                errors.append('La decisión precede a la solicitud.')
    if hr and (hr['is_skipped'] or hr['decision']!='APROBADA'):errors.append('Falta autorización definitiva de RR. HH.')
    if manager and manager['is_skipped']:
        if (not plan or not plan['manager_skipped'] or not plan.get('exception')
            or not manager['skip_reason'] or manager['decision'] is not None
            or not hr or hr['assignee_id']!=plan['superior_id']):
            errors.append('Excepcion de superior operativo no acreditada.')
    if manager and hr and not manager['is_skipped']:
        if manager['assignee_id']!=r['approver_id']:errors.append('El encargado no coincide con el responsable asignado.')
        if manager['step_order']>=hr['step_order'] or not manager['decided_at'] or not hr['decided_at'] or manager['decided_at']>hr['decided_at']:
            errors.append('Orden de aprobación inválido.')
    if hr and (r['decided_by']!=hr['assignee_id'] or r['decided_at']!=hr['decided_at']):
        errors.append('La decisión final no coincide con RR. HH.')
    result['valid']=not errors
    result['stage']='APROBADA_DEFINITIVAMENTE' if not errors else 'APROBACION_NO_VERIFICADA'
    return result
