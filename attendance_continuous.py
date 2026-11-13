"""Incremental invalidation, device coverage and time-dependent daily evaluations."""
import json
from datetime import date, datetime, timedelta


def enabled(cx):
    return bool(cx.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_source_changes'").fetchone())


def queue(cx,uid,day,reason):
    from core import nowstr
    cx.execute('UPDATE attendance_daily_current SET stale=1 WHERE user_id=? AND work_date=?',(uid,day))
    cx.execute('''INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,reason,created_at)
        VALUES(?,?,?,?,?)''',(uid,day,day,reason,nowstr()))


def coverage(cx,day):
    """Latest explicit HR assessment wins; arrival of events never confirms synchronization."""
    if enabled(cx):
        r=cx.execute('''SELECT * FROM attendance_import_receipts WHERE requested_from<=? AND requested_to>=?
            ORDER BY id DESC LIMIT 1''',(day,day)).fetchone()
        if r:
            return {'confirmed':r['coverage_state']=='CONFIRMED','receipt_id':r['id'],
                'expected_devices':json.loads(r['expected_devices_json']),
                'included_devices':json.loads(r['included_devices_json']),
                'device_states':json.loads(r['device_states_json']),'note':r['note'],
                'confirmed_by':r['confirmed_by'],'exported_at':r['exported_at'],'received_at':r['received_at']}
    r=cx.execute('''SELECT 1 FROM import_batches WHERE coverage_start<=? AND coverage_end>=?
        AND coverage_reviewed=1 LIMIT 1''',(day,day)).fetchone()
    return {'confirmed':bool(r),'legacy_assessment':True}


def capture_changes(cx):
    """Resolve old AND new ownership; configuration changes only touch their effective scope."""
    from attendance_engine import work_date
    from core import nowstr
    if not enabled(cx):return
    changes=cx.execute('SELECT * FROM attendance_source_changes WHERE processed_at IS NULL ORDER BY id').fetchall()
    targets={}
    for change in changes:
        table=change['source_table']
        for raw in (change['old_json'],change['new_json']):
            if not raw:continue
            item=json.loads(raw)
            if table=='request_approval_steps':
                from reconciliation import enqueue_request_recalculation
                r=cx.execute('SELECT * FROM requests WHERE id=?',(item['request_id'],)).fetchone()
                if r:enqueue_request_recalculation(cx,r,'Cambio de historial de aprobación')
                continue
            if 'occurred_at' in item:
                if item.get('user_id'):uids={item['user_id']}
                else:
                    uids={r[0] for r in cx.execute('''SELECT user_id FROM biometric_identity_links WHERE person_id=?
                        AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
                        (item['person_id'],item['occurred_at'][:10],item['occurred_at'][:10]))}
                for uid in uids:
                    p=cx.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                    day=work_date(cx,p,datetime.fromisoformat(item['occurred_at'])).isoformat()
                    targets[(uid,day)]=table
                continue
            first=item.get('valid_from') or item.get('date_from') or item.get('work_date') or item.get('date') or '0001-01-01'
            last=item.get('valid_to') or item.get('date_to') or item.get('work_date') or item.get('date') or '9999-12-31'
            # Existing historical evaluations, plus actually available file ranges for a newly linked/eligible person.
            candidates={(r['user_id'],r['work_date']) for r in cx.execute('''SELECT user_id,work_date FROM attendance_daily_current
                WHERE work_date>=COALESCE(date(?,'-1 day'),?) AND work_date<=COALESCE(date(?,'+1 day'),?)''',(first,first,last,last))}
            uids={item['user_id']} if item.get('user_id') else {r['id'] for r in cx.execute("SELECT id FROM users WHERE role<>'admin'")}
            if table=='work_schedule_days':
                assigned=cx.execute('SELECT user_id,area_id FROM schedule_assignments WHERE schedule_id=?',(item['schedule_id'],)).fetchall()
                uids={a['user_id'] for a in assigned if a['user_id']}
                for a in assigned:
                    if a['user_id'] is None:
                        uids.update(r[0] for r in cx.execute('SELECT DISTINCT user_id FROM employee_placements WHERE area_id=?',(a['area_id'],)))
            # Populate gaps only for bounded imported coverage, never infer employment from first punch.
            for uid in uids:
                for r in cx.execute('''SELECT coverage_start,coverage_end FROM import_batches WHERE coverage_start IS NOT NULL
                    AND coverage_end>=? AND coverage_start<=?''',(first,last)):
                    a=date.fromisoformat(max(first,r['coverage_start']));b=date.fromisoformat(min(last,r['coverage_end']))
                    if (b-a).days>366:continue
                    for n in range((b-a).days+1):candidates.add((uid,(a+timedelta(days=n)).isoformat()))
            for uid,day in candidates:
                if uid not in uids:continue
                if item.get('area_id') or item.get('branch_id'):
                    p=cx.execute('''SELECT * FROM employee_placements WHERE user_id=? AND valid_from<=?
                        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',(uid,day,day)).fetchone()
                    if not p or any(item.get(k) and p[k]!=item[k] for k in ('area_id','branch_id')):continue
                targets[(uid,day)]=table
        cx.execute('UPDATE attendance_source_changes SET processed_at=? WHERE id=?',(nowstr(),change['id']))
    for (uid,day),table in targets.items():queue(cx,uid,day,'Cambio de fuente: '+table)


def refresh_expired(cx):
    from core import nowstr
    if not enabled(cx):return
    rows=cx.execute('''SELECT user_id,work_date FROM attendance_daily_current
        WHERE refresh_after IS NOT NULL AND refresh_after<=? AND stale=0''',(nowstr(),)).fetchall()
    for r in rows:queue(cx,r['user_id'],r['work_date'],'Cambio de fase temporal de jornada')


def journey_state(cx,day,ctx,reviewed,events):
    from core import now
    clock=now().replace(tzinfo=None);segments=ctx['segments']
    start=min(a for a,b in segments) if segments else None
    end=max(b for a,b in segments) if segments else None
    refresh=None
    if day>clock.date():state='JORNADA FUTURA';refresh=datetime.combine(day,datetime.min.time())
    elif start and clock<start:state='JORNADA FUTURA';refresh=start
    elif end and clock<end:state='JORNADA ABIERTA';refresh=end
    elif not end and ctx['obligation'] not in ('DESCANSO PROGRAMADO','SIN ASIGNACIÓN DOCUMENTADA'):
        state='TÉRMINO DE JORNADA NO CONFIRMADO'
    elif not reviewed:state='PENDIENTE DE SINCRONIZACIÓN'
    else:state='JORNADA FINALIZADA'
    return {'state':state,'start':start.isoformat(' ') if start else None,
        'end':end.isoformat(' ') if end else None,'refresh_after':refresh.isoformat(' ') if refresh else None,
        'integrity':'COBERTURA CONFIRMADA' if reviewed else 'COBERTURA PENDIENTE',
        'evidence':'SIN REGISTROS' if not events else 'MARCACIÓN INCOMPLETA' if len(events)==1 and ctx['regime_code']!='MOBILE_NO_FIXED_HOURS' else 'ACTIVIDAD REGISTRADA'}


def needs_review(row):
    d=json.loads(row['detail_json'])
    if row['operational_result'] in ('FUERA DE RELACIÓN LABORAL','NO LABORABLE'):return False
    return bool(row['operational_result'] not in ('AUSENCIA JUSTIFICADA','PRESENCIA REGISTRADA') or
        not d.get('employment_confirmed') or not d.get('placement_confirmed') or
        not d.get('coverage_reviewed') or d.get('pending_requests') or d.get('unverified_requests') or d.get('calendar_pending_eligibility') or
        (d.get('effective_punch_count',row['punch_count'])==1 and d.get('regime_code')!='MOBILE_NO_FIXED_HOURS') or
        d.get('grouping',{}).get('missing_movements') or
        d.get('journey',{}).get('state')!='JORNADA FINALIZADA' or
        any(e.get('provenance')=='B_ZLINK_MANUAL' for e in d.get('events',[])) or
        any(not r['authorization']['valid'] and r['status']=='PENDIENTE' for r in d.get('correction_requests',[])))


def process_import(cx,preview,outcome,actor_id,coverage_reviewed=False,requested_from=None,requested_to=None,
                   expected_devices=None,no_event_devices=None,exported_at=None,note=''):
    from core import nowstr
    from reconciliation import drain_recalculations,reconciliation_employees
    a=requested_from or preview['date_from'];b=requested_to or preview['date_to']
    if not a or not b:raise ValueError('Indica el rango solicitado: no se puede deducir desde un archivo sin eventos.')
    first=date.fromisoformat(a);last=date.fromisoformat(b)
    if last<first or (last-first).days>366:raise ValueError('El rango solicitado debe contener entre 1 y 367 días.')
    if (preview['date_from'] and a>preview['date_from']) or (preview['date_to'] and b<preview['date_to']):
        raise ValueError('El rango solicitado debe incluir todos los eventos del archivo.')
    if exported_at:
        from core import TZ,now
        stamp=datetime.fromisoformat(exported_at)
        if stamp.tzinfo:stamp=stamp.astimezone(TZ).replace(tzinfo=None)
        if stamp>now().replace(tzinfo=None):raise ValueError('La exportación no puede tener una fecha futura.')
        exported_at=stamp.isoformat(' ')
    included=sorted({r.get('device_sn','') for r in preview['rows'] if not r.get('parse_error') and r.get('device_sn')})
    known={r[0] for r in cx.execute("SELECT DISTINCT device_sn FROM biometric_events WHERE device_sn<>''")}
    previous=cx.execute('SELECT expected_devices_json FROM attendance_import_receipts ORDER BY id DESC LIMIT 1').fetchone()
    remembered=set(json.loads(previous[0])) if previous else known
    expected=sorted(set(expected_devices) if expected_devices is not None else remembered|set(included))
    empty=set(no_event_devices or [])
    if empty-set(expected):raise ValueError('Los dispositivos sin actividad deben formar parte de los esperados.')
    states={sn:('SYNCED' if sn in included else 'NO_EVENTS_CONFIRMED' if sn in empty else 'MISSING') for sn in expected}
    confirmed=bool(coverage_reviewed and expected and all(v!='MISSING' for v in states.values()))
    before={}
    for n in range((last-first).days+1):
        day=(first+timedelta(days=n)).isoformat();before[day]=coverage(cx,day)['confirmed']
    receipt=cx.execute('''INSERT INTO attendance_import_receipts(batch_id,actor_id,received_at,requested_from,requested_to,
        exported_at,included_devices_json,expected_devices_json,device_states_json,coverage_state,confirmed_by,note,summary_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(outcome['batch_id'],actor_id,nowstr(),a,b,exported_at,json.dumps(included),json.dumps(expected),
        json.dumps(states),'CONFIRMED' if confirmed else 'PENDING',actor_id if confirmed else None,note,'{}')).lastrowid
    capture_changes(cx)
    for p in reconciliation_employees(cx,first,last):
        for n in range((last-first).days+1):
            day=(first+timedelta(days=n)).isoformat()
            current=cx.execute('SELECT 1 FROM attendance_daily_current WHERE user_id=? AND work_date=?',(p['id'],day)).fetchone()
            if not current or before[day]!=confirmed:queue(cx,p['id'],day,'Cobertura importada o jornada nueva del padrón')
    recalculated=drain_recalculations(cx)
    rows=[]
    for uid,day in recalculated.get('affected',[]):
        r=current_evaluation(cx,uid,day)
        if r:rows.append(r)
    summary={**outcome,'receipt_id':receipt,'date_from':a,'date_to':b,'recalculated_days':len(rows),
        'activity_days':sum(r['punch_count']>0 for r in rows),
        'justified_days':sum(r['operational_result']=='AUSENCIA JUSTIFICADA' for r in rows),
        'review_days':sum(needs_review(r) for r in rows),
        'pending_coverage_days':sum(not json.loads(r['detail_json']).get('coverage_reviewed') for r in rows),
        'coverage_pending':not confirmed,'device_states':states,
        'closed_period_changes':sum(r['closed_period_analysis'] for r in rows)}
    cx.execute('UPDATE attendance_import_receipts SET summary_json=? WHERE id=?',(json.dumps(summary,ensure_ascii=False),receipt))
    return summary


def current_evaluation(cx,uid,day):
    return cx.execute('''SELECT d.*,c.stale,c.version_id,v.closed_period_analysis FROM attendance_daily_current c
        JOIN attendance_daily_versions v ON v.id=c.version_id JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id
        WHERE c.user_id=? AND c.work_date=?''',(uid,str(day))).fetchone()
