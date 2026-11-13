"""Conservative daily evaluation with real work intervals and explicit provenance."""
import json
from datetime import date,datetime,time,timedelta
from collections import Counter
from attendance_authorizations import authorization


def merge(intervals):
    out=[]
    for start,end in sorted(intervals):
        if end<=start:continue
        if out and start<=out[-1][1]:out[-1]=(out[-1][0],max(end,out[-1][1]))
        else:out.append((start,end))
    return out


def uncovered(required,authorized):
    remaining=[]
    for start,end in required:
        cursor=start
        for a,b in merge(authorized):
            if b<=cursor or a>=end:continue
            if a>cursor:remaining.append((cursor,min(a,end)))
            cursor=max(cursor,b)
            if cursor>=end:break
        if cursor<end:remaining.append((cursor,end))
    return remaining


def _pair(day,start,end,offset=0):
    a=datetime.combine(day+timedelta(days=offset),time.fromisoformat(start))
    b=datetime.combine(day+timedelta(days=offset),time.fromisoformat(end))
    if b<=a:b+=timedelta(days=1)
    return a,b


def context(cx,employee,day):
    from reconciliation import _pick_period,normalize,BRANCH_STANDARD
    d=day.isoformat();uid=employee['id']
    placement=cx.execute('''SELECT p.*,b.name branch_name,a.name area_name FROM employee_placements p
        LEFT JOIN branches b ON b.id=p.branch_id LEFT JOIN areas a ON a.id=p.area_id
        WHERE p.user_id=? AND p.valid_from<=? AND (p.valid_to IS NULL OR p.valid_to>=?)
        ORDER BY p.valid_from DESC,p.id DESC LIMIT 1''',(uid,d,d)).fetchone()
    employment=_pick_period(cx,'attendance_employment_periods',uid,d)
    regime=_pick_period(cx,'attendance_regime_assignments',uid,d)
    confirmed=bool(employment and employment['confirmed'])
    classified=bool(placement and placement['classification_status']=='CLASIFICADA' and placement['branch_id'])
    known=cx.execute('SELECT 1 FROM attendance_employment_periods WHERE user_id=? AND confirmed=1',(uid,)).fetchone()
    outside=bool(known and not employment)
    code=regime['regime_code'] if regime and regime['confirmed'] else ''
    assignments=[dict(r) for r in cx.execute('SELECT * FROM attendance_work_assignments WHERE user_id=? AND work_date=? AND confirmed=1',(uid,d))]
    obligation='DESCONOCIDA';segments=[]
    if confirmed and classified:
        if code=='MOBILE_NO_FIXED_HOURS':
            if regime['submodality']=='ASSIGNMENTS':obligation='ASIGNACIÓN CONFIRMADA' if assignments else 'SIN ASIGNACIÓN DOCUMENTADA'
            elif regime['submodality']=='DAYS':obligation='TRABAJO PROGRAMADO' if day.weekday() in json.loads(regime['weekdays_json']) else 'DESCANSO PROGRAMADO'
        elif code=='KIMSTAR_ROTATING' or (not code and normalize(placement['branch_name'])=='kimstar'):
            code='KIMSTAR_ROTATING';obligation='TURNO/ASIGNACIÓN CONFIRMADA' if assignments else 'DESCONOCIDA'
        elif code:
            obligation='TRABAJO PROGRAMADO' if day.weekday() in json.loads(regime['weekdays_json']) else 'DESCANSO PROGRAMADO'
        elif normalize(placement['branch_name']) in BRANCH_STANDARD:
            code='BRANCH_STANDARD';obligation='TRABAJO PROGRAMADO' if day.weekday()<6 else 'DESCANSO PROGRAMADO'
        if assignments:obligation='ASIGNACIÓN CONFIRMADA'
        explicit=cx.execute('''SELECT * FROM attendance_journey_segments WHERE confirmed=1 AND weekday=?
            AND (user_id=? OR (user_id IS NULL AND area_id=?)) AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)
            ORDER BY user_id DESC,id''',(day.weekday(),uid,placement['area_id'],d,d)).fetchall()
        personal=[r for r in explicit if r['user_id']==uid]
        if personal:explicit=personal
        if explicit:
            segments=merge([_pair(day,r['start_time'],r['end_time'],r['day_offset']) for r in explicit])
            obligation='TRABAJO PROGRAMADO'
        elif obligation in ('TRABAJO PROGRAMADO','ASIGNACIÓN CONFIRMADA','TURNO/ASIGNACIÓN CONFIRMADA'):
            if regime and regime['confirmed'] and regime['shift_start'] and regime['shift_end']:
                segments=[_pair(day,regime['shift_start'],regime['shift_end'])]
            else:
                schedule=cx.execute('''SELECT wd.* FROM schedule_assignments s JOIN work_schedule_days wd ON wd.schedule_id=s.schedule_id
                    WHERE wd.weekday=? AND (s.user_id=? OR (s.user_id IS NULL AND s.area_id=?))
                    AND s.valid_from<=? AND (s.valid_to IS NULL OR s.valid_to>=?)
                    ORDER BY s.user_id DESC,s.valid_from DESC,s.id DESC LIMIT 1''',(day.weekday(),uid,placement['area_id'],d,d)).fetchone()
                if schedule:
                    if not schedule['is_working']:obligation='DESCANSO PROGRAMADO'
                    elif schedule['start_time'] and schedule['end_time']:segments=[_pair(day,schedule['start_time'],schedule['end_time'])]
    return {'placement':placement,'employment':employment,'regime':regime,'regime_code':code,
        'confirmed':confirmed,'classified':classified,'outside':outside,'obligation':obligation,
        'segments':segments,'assignments':assignments}


def work_date(cx,employee,stamp):
    day=stamp.date();previous=context(cx,employee,day-timedelta(days=1))
    from attendance_grouping import boundary_allowance
    allowance=boundary_allowance(cx,employee,day-timedelta(days=1),previous)
    if previous['segments'] and previous['segments'][-1][1].date()==day and stamp<=previous['segments'][-1][1]+timedelta(seconds=allowance):
        return day-timedelta(days=1)
    return day


def collect_events(cx,employee,day,ctx=None):
    ctx=ctx or context(cx,employee,day)
    a=datetime.combine(day,time.min);b=max([a+timedelta(days=1)]+[end for _,end in ctx['segments']])
    from attendance_grouping import boundary_allowance
    if ctx['segments']:b=max(b,ctx['segments'][-1][1]+timedelta(seconds=boundary_allowance(cx,employee,day,ctx)))
    uid=employee['id'];start=a.isoformat(' ');finish=b.isoformat(' ')
    values=[]
    for r in cx.execute('''SELECT e.* FROM biometric_events e JOIN biometric_identity_links l ON l.person_id=e.person_id
        WHERE l.user_id=? AND l.valid_from<=substr(e.occurred_at,1,10)
          AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
          AND e.occurred_at>=? AND e.occurred_at<=? ORDER BY e.occurred_at,e.id''',(uid,start,finish)):
        item=dict(r);item['provenance']='B_ZLINK_MANUAL' if 'manual' in r['event_type'].lower() or 'administrador' in r['source'].lower() else 'A_ZLINK_PHYSICAL'
        item['original_event_id']=r['id'];item['direction']=None;values.append(item)
    for r in cx.execute('SELECT * FROM punches WHERE user_id=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,id',(uid,start,finish)):
        item=dict(r);item.update(provenance='LEGACY_IMPORT',direction=r['kind'],event_type=r['kind'],device_name='',device_sn='');values.append(item)
    values=[r for r in values if work_date(cx,employee,datetime.fromisoformat(r['occurred_at']))==day]
    corrections=[]
    for r in cx.execute("SELECT * FROM requests WHERE user_id=? AND kind='CORRECCION' AND correction_at>=? AND correction_at<=? ORDER BY id",(uid,start,finish)):
        if work_date(cx,employee,datetime.fromisoformat(r['correction_at']))!=day:continue
        item=dict(r);item['authorization']=authorization(cx,r);corrections.append(item)
        if not item['authorization']['valid']:continue
        represented=[event for event in values if event['occurred_at']==r['correction_at']]
        if represented:
            for event in represented:event.setdefault('regularization_refs',[]).append({'id':r['id'],'folio':r['folio'],'direction':r['correction_kind']})
        else:
            values.append({'occurred_at':r['correction_at'],'provenance':'C_APPROVED_REGULARIZATION',
                'source':'Regularización aprobada','event_type':r['correction_kind'],'direction':r['correction_kind'],
                'request_id':r['id'],'folio':r['folio'],'device_name':'','device_sn':''})
    values.sort(key=lambda r:r['occurred_at'])
    return values,corrections


def evaluate(cx,employee,day):
    from reconciliation import _coverage_reviewed,_calendar_items
    ctx=context(cx,employee,day);d=day.isoformat();uid=employee['id'];segments=ctx['segments']
    events,corrections=collect_events(cx,employee,day,ctx)
    raw=[e for e in events if e['provenance']!='C_APPROVED_REGULARIZATION']
    window_start=min(a for a,_ in segments) if segments else datetime.combine(day,time.min)
    window_end=max(b for _,b in segments) if segments else datetime.combine(day+timedelta(days=1),time.min)
    requests=[]
    for r in cx.execute("SELECT * FROM requests WHERE user_id=? AND kind='PERMISO' AND starts_at<? AND ends_at>=? ORDER BY id",(uid,window_end.isoformat(' '),window_start.isoformat(' '))):
        item=dict(r);item['authorization']=authorization(cx,r);requests.append(item)
    approved=[r for r in requests if r['authorization']['valid']]
    pending=[r for r in requests if r['status']=='PENDIENTE']
    rejected=[r for r in requests if r['status']=='RECHAZADA']
    invalid=[r for r in requests+corrections if r['status']=='APROBADA' and not r['authorization']['valid']]
    good_corrections=[r for r in corrections if r['authorization']['valid']]
    pending_corrections=[r for r in corrections if r['status']=='PENDIENTE']
    rejected_corrections=[r for r in corrections if r['status']=='RECHAZADA']
    cal,legacy=_calendar_items(cx,uid,ctx['placement']['branch_id'] if ctx['placement'] else None,d)
    calendar_pending=[dict(e) for e in cal if e['eligibility'].strip()]
    cal=[dict(e) for e in cal if e['approved_by'] is not None and not e['eligibility'].strip()]
    resolutions=[dict(r) for r in cx.execute('SELECT * FROM attendance_resolutions WHERE user_id=? AND work_date=? AND active=1 ORDER BY id DESC',(uid,d))]
    authorized=[]
    for r in approved:
        a=datetime.fromisoformat(r['starts_at']);b=datetime.fromisoformat(r['ends_at'])
        if b.time()==time(23,59,59):b+=timedelta(seconds=1)
        authorized.append((a,b))
    if cal or legacy or (resolutions and resolutions[0]['resolution_type']=='JUSTIFICACION'):
        authorized.extend(segments or [(window_start,window_end)])
    from attendance_grouping import evaluate_groups
    grouping=evaluate_groups(cx,employee,day,ctx,events,authorized)
    effective=grouping['effective_count']
    missing=uncovered(segments,authorized) if segments else []
    full=bool(segments and not missing)
    if cal or legacy or (resolutions and resolutions[0]['resolution_type']=='JUSTIFICACION'):
        full=True
    if not segments and ctx['obligation'] not in ('DESCONOCIDA','SIN ASIGNACIÓN DOCUMENTADA'):
        full=not uncovered([(window_start,window_end)],authorized)
    overlap=bool(authorized and (not segments or any(a<y and b>x for a,b in authorized for x,y in segments)))
    unknown_coverage=bool(approved and not segments and not full)
    note=[];justification='SIN JUSTIFICACIÓN APROBADA'
    if calendar_pending:note.append('Elegibilidad textual no verificada: evento pendiente de revisión, no justifica automáticamente.')
    if approved:
        types={r['leave_type'] for r in approved}
        justification='VACACIONES APROBADAS' if types=={'VACACIONES'} else 'LICENCIA APROBADA' if types<={'DESCANSO_MEDICO','LICENCIA'} else 'PERMISO APROBADO'
    elif cal:
        justification={'FERIADO':'FERIADO','CUMPLEANOS':'ASUETO POR CUMPLEAÑOS','DESCANSO_ESPECIAL':'DESCANSO'}.get(cal[0]['event_type'],'ASUETO ESPECIAL')
    elif legacy or (resolutions and resolutions[0]['resolution_type']=='JUSTIFICACION'):justification='ASUETO ESPECIAL'
    elif good_corrections:justification='REGULARIZACIÓN APROBADA'
    elif pending or pending_corrections:justification='SOLICITUD PENDIENTE'
    elif rejected or rejected_corrections:justification='SOLICITUD RECHAZADA'
    if invalid:note.append('Solicitud marcada aprobada sin historial definitivo verificable; no justifica.')
    if approved and not overlap:
        justification='SIN JUSTIFICACIÓN APROBADA'
        note.append('La autorización relacionada no cubre ningún tramo de la jornada exigible.')
    if rejected or rejected_corrections:note.append('Existe una solicitud rechazada; no se aplica como autorización.')
    # Only explicit IN/OUT evidence can establish an interval of physical presence.
    presence=[];entry=None
    for e in events:
        stamp=datetime.fromisoformat(e['occurred_at']);direction=e.get('direction')
        if direction=='IN':entry=stamp
        elif direction=='OUT' and entry and stamp>entry:presence.append((entry,stamp));entry=None
    uncovered_total=uncovered(segments,authorized+presence) if segments else []
    reviewed=all(_coverage_reviewed(cx,(day+timedelta(days=i)).isoformat())
        for i in range((max([day]+[b.date() for _,b in segments])-day).days+1))
    from attendance_continuous import coverage
    assessments=[coverage(cx,(day+timedelta(days=i)).isoformat())
        for i in range((window_end.date()-day).days+1)]
    if segments and any(a.get('received_at') and datetime.fromisoformat(a.get('exported_at') or a['received_at'])<window_end for a in assessments):
        reviewed=False
    technical=bool(cx.execute('SELECT 1 FROM import_batches WHERE coverage_start<=? AND coverage_end>=? AND rows_error>0',(d,d)).fetchone())
    result='INFORMACIÓN INSUFICIENTE'
    if ctx['outside']:
        result='FUERA DE RELACIÓN LABORAL';justification='NO REQUIERE JUSTIFICACIÓN'
    elif events:
        result='PRESENCIA REGISTRADA'
        if effective==1:note.append('Un solo grupo efectivo: no acredita automáticamente la jornada completa.')
        if grouping['missing_movements']:note.append('Movimientos sin acreditar según política confirmada: '+', '.join(m['movement'] for m in grouping['missing_movements'])+'.')
        if grouping['unidentified_groups']:note.append('MOVIMIENTO POR IDENTIFICAR: no se deduce entrada o salida solo por agrupar huellas.')
        if ctx['obligation']=='DESCANSO PROGRAMADO':note.append('Actividad registrada en día no programado; no se calculan horas extraordinarias.')
        if full:note.append('Actividad durante un intervalo autorizado; conservar ambas fuentes para revisión.')
        elif overlap and segments and uncovered_total:result='COBERTURA PARCIAL'
        elif unknown_coverage:result='INFORMACIÓN INSUFICIENTE'
    elif not ctx['confirmed'] or not ctx['classified']:
        note.append('Faltan vigencia laboral o adscripción confirmadas; no se infieren desde el Excel.')
    elif ctx['obligation']=='DESCANSO PROGRAMADO':result='NO LABORABLE';justification='DESCANSO'
    elif full:result='AUSENCIA JUSTIFICADA'
    elif ctx['obligation']=='DESCONOCIDA':
        result='HORARIO NO CONFIGURADO';note.append('Calendario o turno especial pendiente de configuración.')
    elif ctx['obligation']=='SIN ASIGNACIÓN DOCUMENTADA':
        note.append('Trabajo móvil sin obligación documentada para este día; no genera falta.')
    elif unknown_coverage:
        note.append('No existe horario confirmado para resolver la cobertura de este permiso.')
    elif overlap:result='COBERTURA PARCIAL'
    elif pending or pending_corrections or calendar_pending:note.append('Solicitud o elegibilidad todavía pendiente de verificación.')
    elif technical:result='INCIDENCIA TÉCNICA'
    elif not reviewed:note.append('Cobertura biométrica no confirmada para toda la jornada.')
    else:result='POSIBLE INASISTENCIA'
    if result=='POSIBLE INASISTENCIA':
        from core import now
        if now().replace(tzinfo=None)<window_end:
            result='INFORMACIÓN INSUFICIENTE'
            note.append('Jornada todavía abierta o futura; esperar su término y verificar sincronización.')
    from attendance_continuous import journey_state,coverage
    journey=journey_state(cx,day,ctx,reviewed,grouping['groups'])
    if grouping['missing_movements']:journey['evidence']='MARCACIÓN INCOMPLETA'
    if result=='POSIBLE INASISTENCIA' and journey['state']!='JORNADA FINALIZADA':
        result='INFORMACIÓN INSUFICIENTE'
        note.append(journey['state']+'; no hay evidencia suficiente para proponer inasistencia.')
    if resolutions and resolutions[0]['resolution_type']=='FALTA_CONFIRMADA':
        result='FALTA CONFIRMADA POR RR. HH.'
        if approved or events:note.append('La falta confirmada coincide con nueva evidencia; requiere revisión de RR. HH.')
        if journey['state']!='JORNADA FINALIZADA':note.append('La resolución previa requiere revisión: calendario, término o sincronización no verificados con las fuentes actuales.')
    if technical:note.append('El lote contiene filas observadas; revisar integridad.')
    if any(e['provenance']=='B_ZLINK_MANUAL' for e in events):note.append('Fichaje manual Zlink: origen conservado; no equivale a autorización interna.')
    if not segments and ctx['obligation']=='DESCONOCIDA':note.append('No se inventan horas ni días laborales.')
    if len(approved)>1:note.append('Se combinaron autorizaciones; revisar solapamientos y motivos incompatibles.')
    status='SIN MARCACIONES' if not events else 'REGISTRO ÚNICO' if effective==1 else 'REGISTROS MÚLTIPLES'
    if good_corrections:status='REGULARIZACIÓN REGISTRADA'
    references=[]
    if full and approved:
        policy=cx.execute('''SELECT * FROM attendance_presentation_policies WHERE user_id=? AND valid_from<=?
            AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',(uid,d,d)).fetchone()
        if policy:references=[{'provenance':'D_ADMINISTRATIVE_REFERENCE','time':policy[k],
            'policy_id':policy['id'],'request_ids':[r['id'] for r in approved],
            'calendar_ids':[r['id'] for r in cal],
            'resolution_id':resolutions[0]['id'] if resolutions else None} for k in ('start_time','end_time')]
    sources=Counter(e['source'] for e in events)
    detail={'grouping':grouping,'effective_punch_count':effective,'journey':journey,'device_coverage':coverage(cx,d),
        'regime_code':ctx['regime_code'],'employment_period_id':ctx['employment']['id'] if ctx['employment'] else None,
        'branch_id':ctx['placement']['branch_id'] if ctx['placement'] else None,
        'branch_name':ctx['placement']['branch_name'] if ctx['placement'] else '',
        'area_name':ctx['placement']['area_name'] if ctx['placement'] else '',
        'placement_confirmed':ctx['classified'],'employment_confirmed':ctx['confirmed'],'coverage_reviewed':reviewed,
        'events':events,'original_punch_count':len(raw),'approved_requests':approved,'pending_requests':pending,
        'rejected_requests':rejected,'unverified_requests':invalid,'correction_requests':corrections,
        'calendar_events':cal,'calendar_pending_eligibility':calendar_pending,'legacy_calendar_events':[dict(r) for r in legacy],
        'work_assignments':ctx['assignments'],'resolution':resolutions[0] if resolutions else None,
        'work_intervals':[(a.isoformat(' '),b.isoformat(' ')) for a,b in segments],
        'authorized_intervals':[(a.isoformat(' '),b.isoformat(' ')) for a,b in merge(authorized)],
        'uncovered_intervals':[(a.isoformat(' '),b.isoformat(' ')) for a,b in missing],
        'administrative_references':references,'coverage_complete':full}
    return {'user_id':uid,'work_date':d,'punch_status':status,'justification_status':justification,
        'operational_result':result,'work_obligation':'FUERA DE RELACIÓN LABORAL' if ctx['outside'] else ctx['obligation'],
        'punch_count':len(events),'source_summary':', '.join(f'{k}: {v}' for k,v in sorted(sources.items())),
        'reason':' '.join(note),'detail_json':json.dumps(detail,ensure_ascii=False,sort_keys=True,default=str)}
