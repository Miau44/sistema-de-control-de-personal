"""Conservative import and reconciliation of ZKBio Zlink attendance exports."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from io import BytesIO

from openpyxl import load_workbook

from core import nowstr

EXPECTED = {
    'person_id': 'id de persona', 'person_name': 'nombre de la persona',
    'department': 'departamento', 'event_type': 'tipo', 'source': 'fuente',
    'weekday': 'dia de la semana', 'occurred_at': 'hora de fichaje',
    'timezone': 'zona horaria', 'verification_mode': 'modo de verificacion',
    'is_mobile': 'fichaje movil', 'device_sn': 'dispositivo sn',
    'device_name': 'nombre del dispositivo', 'uploaded_at': 'hora de carga',
}
KIMSTAR = 'kimstar'
BRANCH_STANDARD = {
    'sede central', 'mercatech el alto', 'mercatech riosinio',
    'mercatech la paz', 'mercatech cochabamba',
}
WEEKDAY_ES = ('Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo')


def normalize(value) -> str:
    text = unicodedata.normalize('NFKD', str(value or '')).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def _cell_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _stamp(value, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        except (TypeError, ValueError):
            raise ValueError(f'{field} no contiene una fecha y hora válida.')
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def _event_identity_key(row: dict) -> str:
    stable={key:row.get(key,'') or '' for key in
        ('person_id','occurred_at','event_type','source','device_sn','timezone','verification_mode','is_mobile')}
    return hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()


def parse_zlink_xlsx(content: bytes) -> dict:
    """Find the real header row (Zlink exports include a title row) and retain raw fields."""
    if not content or len(content) > 20 * 1024 * 1024:
        raise ValueError('El archivo está vacío o supera el límite de 20 MB.')
    book = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        sheet = book.active
        sheet.reset_dimensions()
        iterator = sheet.iter_rows(values_only=True)
        header_row = None
        index = None
        for row_number, row in enumerate(iterator, 1):
            normalized = {normalize(value): pos for pos, value in enumerate(row) if value is not None}
            candidate = {}
            for field, name in EXPECTED.items():
                if name in normalized:
                    candidate[field] = normalized[name]
            required = {'person_id', 'person_name', 'event_type', 'source', 'occurred_at', 'device_sn', 'device_name', 'uploaded_at'}
            if required.issubset(candidate):
                header_row, index = row_number, candidate
                break
            if row_number >= 12:
                break
        if index is None:
            raise ValueError('No encuentro los encabezados de ZKBio Zlink en las primeras 12 filas.')
        rows = []
        for row_number, row in enumerate(iterator, header_row + 1):
            if not any(value is not None and str(value).strip() for value in row):
                continue
            item = {field: (row[pos] if pos < len(row) else None) for field, pos in index.items()}
            item['row_number'] = row_number
            person_id = _cell_text(item.get('person_id'))
            if not person_id:
                item['parse_error'] = 'ID de persona vacío.'
            else:
                item['person_id'] = person_id[:-2] if re.fullmatch(r'\d+\.0', person_id) else person_id
            try:
                item['occurred_at'] = _stamp(item.get('occurred_at'), 'Hora de fichaje')
                item['uploaded_at'] = _stamp(item.get('uploaded_at'), 'Hora de carga') if item.get('uploaded_at') else ''
            except ValueError as exc:
                item['parse_error'] = str(exc)
            for field in EXPECTED:
                if field not in ('person_id', 'occurred_at', 'uploaded_at'):
                    item[field] = _cell_text(item.get(field))
            if not item.get('person_name'):
                item['parse_error'] = 'Nombre de persona vacío.'
            item['fingerprint'] = hashlib.sha256(json.dumps(
                {key: item.get(key, '') for key in EXPECTED}, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            rows.append(item)
    finally:
        book.close()
    # An export with valid headers but no events needs an explicit requested range and HR coverage assessment.
    return {'header_row': header_row, 'rows': rows}


def _verified_identity_user(cx, person_id: str, day: str):
    direct = cx.execute('SELECT id FROM users WHERE employee_code=? AND active=1', (person_id,)).fetchall()
    user_ids = {row['id'] for row in direct}
    mapped = cx.execute('''SELECT DISTINCT user_id FROM biometric_assignments
        WHERE employee_code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
        (person_id, day, day)).fetchall()
    user_ids.update(row['user_id'] for row in mapped)
    return next(iter(user_ids)) if len(user_ids) == 1 else None


def preview_zlink(cx, content: bytes) -> dict:
    parsed = parse_zlink_xlsx(content)
    errors = [row for row in parsed['rows'] if row.get('parse_error')]
    valid = [row for row in parsed['rows'] if not row.get('parse_error')]
    identity_keys = [_event_identity_key(row) for row in valid]
    existing = set()
    if identity_keys:
        for offset in range(0, len(identity_keys), 400):
            section = identity_keys[offset:offset + 400]
            marks = ','.join('?' for _ in section)
            existing.update(row['row_fingerprint'] for row in cx.execute(
                f'SELECT event_identity_key AS row_fingerprint FROM biometric_events WHERE event_identity_key IN ({marks})', section))
    duplicate_in_file = len(identity_keys) - len(set(identity_keys))
    ids = {row['person_id'] for row in valid}
    identity_counts = Counter(row['person_id'] for row in valid)
    linked = pending = 0
    devices = set()
    for person_id in ids:
        if any(row['device_sn'] for row in valid if row['person_id'] == person_id):
            pass
        sample = next(row for row in valid if row['person_id'] == person_id)
        days={row['occurred_at'][:10] for row in valid if row['person_id']==person_id}
        confirmed=all(cx.execute('''SELECT COUNT(DISTINCT user_id) FROM biometric_identity_links
            WHERE person_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
            (person_id,day,day)).fetchone()[0]==1 for day in days)
        linked += int(confirmed)
        pending += int(not confirmed)
    for row in valid:
        if row['device_sn']:
            devices.add(row['device_sn'])
    dates = [row['occurred_at'][:10] for row in valid]
    kinds = Counter(row['event_type'] for row in valid)
    sources = Counter(row['source'] for row in valid)
    return {
        **parsed, 'row_count': len(parsed['rows']), 'valid_count': len(valid),
        'errors': errors[:25], 'error_count':len(errors), 'duplicate_existing': sum(key in existing for key in identity_keys),
        'duplicate_in_file': duplicate_in_file, 'identity_count': len(ids),
        'identity_linked': linked, 'identity_pending': pending, 'device_count': len(devices),
        'date_from': min(dates) if dates else None, 'date_to': max(dates) if dates else None,
        'event_types': kinds, 'sources': sources,
        'sha256': hashlib.sha256(content).hexdigest(),
        'new_count_estimate': max(0, len(set(identity_keys) - existing)),
        'identity_event_counts': identity_counts,
    }


def import_zlink(cx, preview: dict, content: bytes, actor_id: int, filename: str,
                 coverage_reviewed: bool = False) -> dict:
    if hashlib.sha256(content).hexdigest() != preview['sha256']:
        raise ValueError('El archivo cambió después de la vista previa. Vuelve a previsualizarlo.')
    existing_batch=cx.execute('SELECT id,rows_total FROM import_batches WHERE file_sha256=? AND rows_error=0 ORDER BY id LIMIT 1',
                              (preview['sha256'],)).fetchone()
    if existing_batch:
        unlinked=cx.execute('''SELECT COUNT(DISTINCT e.person_id) FROM biometric_events e
            WHERE e.import_batch_id=? AND NOT EXISTS(SELECT 1 FROM biometric_identity_links l
              WHERE l.person_id=e.person_id AND l.valid_from<=substr(e.occurred_at,1,10)
              AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10)))''',
            (existing_batch['id'],)).fetchone()[0]
        return {'batch_id':existing_batch['id'],'added':0,'duplicate':preview['valid_count'],
                'errors':len(preview['errors']),'unlinked':unlinked,'identities_new':0,
                'already_imported':True}
    batch = cx.execute('''INSERT INTO import_batches
        (filename,imported_by,imported_at,rows_total,rows_observed,rows_added,rows_duplicate,
         rows_error,rows_unlinked,file_sha256,coverage_start,coverage_end,coverage_reviewed,details_json)
        VALUES(?,?,?,?,0,0,0,0,0,?,?,?,?,?)''',
        ((filename or 'ZKBio Zlink')[:150], actor_id, nowstr(), preview['row_count'],
         preview['sha256'], preview['date_from'], preview['date_to'], int(coverage_reviewed), '[]'))
    batch_id = batch.lastrowid
    added = duplicate = errors = 0
    error_messages = []
    new_identities = {}
    person_dates=defaultdict(list)
    for row in preview['rows']:
        if not row.get('parse_error'):
            person_dates[row['person_id']].append(row['occurred_at'][:10])
    for row in preview['rows']:
        if row.get('parse_error'):
            errors += 1
            if len(error_messages) < 25:
                error_messages.append(f"Fila {row['row_number']}: {row['parse_error']}")
            continue
        person_id = row['person_id']
        existing_identity = cx.execute('SELECT * FROM biometric_persons WHERE person_id=?', (person_id,)).fetchone()
        note = ''
        if person_id == '8282765':
            note = 'Candidato informado por RR. HH.: Jheyson Jhoel Flores Velasco; posible coincidencia con “Jeyso/Jayson”. Confirmar con padrón antes de vincular y asignar régimen móvil.'
        if existing_identity:
            cx.execute('''UPDATE biometric_persons SET display_name=?,department=?,
                candidate_note=CASE WHEN candidate_note='' AND ?<>'' THEN ? ELSE candidate_note END,
                updated_at=? WHERE person_id=?''',
                (row['person_name'], row.get('department', ''), note, note, nowstr(), person_id))
        else:
            direct_user = None  # RR. HH. confirms every new ZKBio identity with dated evidence.
            status = 'CONFIRMED' if direct_user is not None else 'PENDING'
            cx.execute('''INSERT INTO biometric_persons(person_id,display_name,department,candidate_note,
                linked_user_id,link_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                (person_id, row['person_name'], row.get('department', ''), note,
                 direct_user, status, nowstr(), nowstr()))
            new_identities[person_id] = status
            if direct_user is not None:
                cx.execute('''INSERT INTO biometric_identity_links
                    (person_id,user_id,valid_from,valid_to,confirmed_by,confirmed_at,note)
                    VALUES(?,?,?,?,?,?,?)''',(person_id,direct_user,min(person_dates[person_id]),
                    max(person_dates[person_id]),actor_id,nowstr(),
                    'Vínculo por ID de padrón o asignación biométrica inequívoca.'))
        event_key=_event_identity_key(row)
        if cx.execute('SELECT 1 FROM biometric_events WHERE event_identity_key=?',(event_key,)).fetchone():
            duplicate+=1
            continue
        try:
            cx.execute('''INSERT INTO biometric_events
                (import_batch_id,person_id,person_name,department,event_type,source,weekday,occurred_at,
                 timezone,verification_mode,is_mobile,device_sn,device_name,uploaded_at,row_number,
                 row_fingerprint,imported_by,imported_at,event_identity_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (batch_id, person_id, row['person_name'], row.get('department', ''), row.get('event_type', ''),
                 row.get('source', ''), row.get('weekday', ''), row['occurred_at'], row.get('timezone', ''),
                 row.get('verification_mode', ''), row.get('is_mobile', ''), row.get('device_sn', ''),
                 row.get('device_name', ''), row.get('uploaded_at', ''), row['row_number'],
                 row['fingerprint'], actor_id, nowstr(),event_key))
            added += 1
        except Exception as exc:
            if 'UNIQUE constraint failed: biometric_events.row_fingerprint' in str(exc) or 'idx_biometric_event_identity_key' in str(exc):
                duplicate += 1
            else:
                errors += 1
                if len(error_messages) < 25:
                    error_messages.append(f"Fila {row['row_number']}: {exc}")
    unlinked = cx.execute('''SELECT COUNT(DISTINCT e.person_id) FROM biometric_events e
        WHERE e.import_batch_id=? AND NOT EXISTS(SELECT 1 FROM biometric_identity_links l
          WHERE l.person_id=e.person_id AND l.valid_from<=substr(e.occurred_at,1,10)
          AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10)))''', (batch_id,)).fetchone()[0]
    cx.execute('''UPDATE import_batches SET rows_observed=?,rows_added=?,rows_duplicate=?,rows_error=?,
        rows_unlinked=?,details_json=? WHERE id=?''',
        (added + duplicate + errors, added, duplicate, errors, unlinked,
         json.dumps(error_messages, ensure_ascii=False), batch_id))
    return {'batch_id': batch_id, 'added': added, 'duplicate': duplicate,
            'errors': errors, 'unlinked': unlinked, 'identities_new': len(new_identities)}


def _pick_period(cx, table: str, user_id: int, day: str):
    return cx.execute(f'''SELECT * FROM {table} WHERE user_id=? AND valid_from<=?
        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',
        (user_id, day, day)).fetchone()


def _approved_items(cx, user_id: int, day: str):
    start, end = day + ' 00:00:00', day + ' 23:59:59'
    requests = cx.execute('''SELECT * FROM requests WHERE user_id=? AND kind='PERMISO'
        AND starts_at<=? AND ends_at>=? ORDER BY submitted_at''', (user_id, end, start)).fetchall()
    corrections = cx.execute('''SELECT * FROM requests WHERE user_id=? AND kind='CORRECCION'
        AND correction_at>=? AND correction_at<=? ORDER BY submitted_at''', (user_id, start, end)).fetchall()
    return requests, corrections


def _calendar_items(cx, user_id: int, branch_id, day: str):
    events = cx.execute('''SELECT * FROM attendance_calendar_events WHERE date_from<=? AND date_to>=?
        AND (user_id=? OR (user_id IS NULL AND (branch_id IS NULL OR branch_id=?)))''',
        (day, day, user_id, branch_id)).fetchall()
    legacy = cx.execute('''SELECT * FROM calendar_exceptions WHERE date=?
        AND (user_id=? OR user_id IS NULL) ORDER BY user_id DESC''', (day, user_id)).fetchall()
    return list(events), list(legacy)


def _coverage_reviewed(cx, day: str) -> bool:
    from attendance_continuous import coverage
    return coverage(cx,day)['confirmed']


def _punches_for_day(cx, user_id: int, day: str):
    from attendance_engine import collect_events
    employee=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    return collect_events(cx,employee,date.fromisoformat(day))[0]


def _evaluate_day(cx, employee, day: date):
    from attendance_engine import evaluate
    return evaluate(cx,employee,day)


def reconcile(cx, date_from: date, date_to: date, actor_id: int, user_ids=None, reason="Recalculo de rango", request_id=None) -> dict:
    if date_to < date_from:
        raise ValueError('La fecha final debe ser igual o posterior a la inicial.')
    if (date_to - date_from).days > 366:
        raise ValueError('El rango máximo por recálculo es de 367 días.')
    employees = reconciliation_employees(cx,date_from,date_to)
    if user_ids is not None:
        employees=[e for e in employees if e["id"] in set(user_ids)]
    evaluated = []
    day = date_from
    while day <= date_to:
        for employee in employees:
            evaluated.append(_evaluate_day(cx, employee, day))
        day += timedelta(days=1)
    batches = [dict(row) for row in cx.execute('''SELECT DISTINCT file_sha256,coverage_start,coverage_end,
        coverage_reviewed,rows_error FROM import_batches
        WHERE coverage_start<=? AND coverage_end>=? ORDER BY file_sha256''',
        (date_to.isoformat(), date_from.isoformat()))]
    input_payload = {
        'from': date_from.isoformat(), 'to': date_to.isoformat(), 'batches': batches,
        'days': evaluated,
    }
    input_hash = hashlib.sha256(json.dumps(input_payload, ensure_ascii=False, sort_keys=True,
                                           default=str, separators=(',', ':')).encode()).hexdigest()
    reviewed = all(_coverage_reviewed(cx, (date_from + timedelta(days=offset)).isoformat())
                   for offset in range((date_to-date_from).days + 1))
    # All periods that touch the 26–25 cycle but are not complete remain provisional.
    next_month=(date_from.replace(day=1)+timedelta(days=32)).replace(day=1)
    full_cycle = date_from.day == 26 and date_to == next_month.replace(day=25)
    is_partial = not full_cycle or not reviewed
    actor = cx.execute('SELECT name FROM users WHERE id=?',(actor_id,)).fetchone() if actor_id is not None else None
    actor_label = actor['name'] if actor else 'Sistema / importación asistida'
    cx.execute('''INSERT OR IGNORE INTO attendance_reconciliation_runs
        (date_from,date_to,input_hash,actor_id,actor_label,executed_at,people_evaluated,days_evaluated,
         source_coverage_reviewed,is_partial) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (date_from.isoformat(), date_to.isoformat(), input_hash, actor_id, actor_label, nowstr(),
         len(employees), len(evaluated), int(reviewed), int(is_partial)))
    run = cx.execute('SELECT * FROM attendance_reconciliation_runs WHERE input_hash=?', (input_hash,)).fetchone()
    run_id = run['id']
    cx.executemany('''INSERT INTO attendance_reconciliation_days
        (run_id,user_id,work_date,punch_status,justification_status,operational_result,
         work_obligation,punch_count,source_summary,reason,detail_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,user_id,work_date) DO UPDATE SET
         punch_status=excluded.punch_status,justification_status=excluded.justification_status,
         operational_result=excluded.operational_result,work_obligation=excluded.work_obligation,
         punch_count=excluded.punch_count,source_summary=excluded.source_summary,
         reason=excluded.reason,detail_json=excluded.detail_json''',
        [(run_id, row['user_id'], row['work_date'], row['punch_status'], row['justification_status'],
          row['operational_result'], row['work_obligation'], row['punch_count'],
          row['source_summary'], row['reason'], row['detail_json']) for row in evaluated])
    from core import closed_for_date
    for row in cx.execute('SELECT * FROM attendance_reconciliation_days WHERE run_id=?',(run_id,)).fetchall():
        old=cx.execute('SELECT * FROM attendance_daily_current WHERE user_id=? AND work_date=?',
                       (row['user_id'],row['work_date'])).fetchone()
        previous=cx.execute('''SELECT v.evaluation_id,d.detail_json,d.operational_result FROM attendance_daily_versions v JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id WHERE v.id=?''',
                            (old['version_id'],)).fetchone() if old else None
        if previous and previous['evaluation_id']==row['id']:
            cx.execute('UPDATE attendance_daily_current SET stale=0 WHERE user_id=? AND work_date=?',(row['user_id'],row['work_date']))
            continue
        version=cx.execute('''INSERT INTO attendance_daily_versions(user_id,work_date,evaluation_id,previous_id,request_id,reason,created_at,closed_period_analysis) VALUES(?,?,?,?,?,?,?,?)''',
             (row['user_id'],row['work_date'],row['id'],old['version_id'] if old else None,request_id,reason,nowstr(),
              int(closed_for_date(cx,date.fromisoformat(row['work_date']))))).lastrowid
        cx.execute('''INSERT INTO attendance_daily_current(user_id,work_date,version_id,stale) VALUES(?,?,?,0) ON CONFLICT(user_id,work_date) DO UPDATE SET version_id=excluded.version_id,stale=0''',(row['user_id'],row['work_date'],version))
        from attendance_continuous import enabled
        if enabled(cx):
            cx.execute('UPDATE attendance_daily_current SET refresh_after=? WHERE user_id=? AND work_date=?',
                (json.loads(row['detail_json']).get('journey',{}).get('refresh_after'),row['user_id'],row['work_date']))
    scope_ids={e['id'] for e in employees}
    for queued in cx.execute('SELECT * FROM attendance_recalc_queue WHERE processed_at IS NULL').fetchall():
        if queued['user_id'] in scope_ids and queued['starts_at'] and date_from.isoformat()<=queued['starts_at'][:10] and date_to.isoformat()>=queued['ends_at'][:10]:
            cx.execute('UPDATE attendance_recalc_queue SET processed_at=? WHERE id=?',(nowstr(),queued['id']))
    stats = Counter(row['operational_result'] for row in evaluated)
    stats['people_evaluated'] = len(employees)
    stats['days_evaluated'] = len(evaluated)
    stats['activity_days'] = sum(1 for row in evaluated if row['punch_count'])
    stats['justified_days'] = sum(1 for row in evaluated if row['operational_result'] == 'AUSENCIA JUSTIFICADA')
    stats['pending_requests'] = sum(1 for row in evaluated if row['justification_status'] == 'SOLICITUD PENDIENTE')
    stats['single_records'] = sum(1 for row in evaluated if row['punch_status'] == 'REGISTRO ÚNICO')
    stats['unconfigured_schedule'] = len({row['user_id'] for row in evaluated
        if row['operational_result'] == 'HORARIO NO CONFIGURADO' or
        not json.loads(row['detail_json']).get('regime_code')})
    stats['possible_absences'] = stats['POSIBLE INASISTENCIA']
    stats['confirmed_absences'] = stats['FALTA CONFIRMADA POR RR. HH.']
    stats['technical_incidents'] = stats['INCIDENCIA TÉCNICA']
    stats['information_insufficient'] = stats['INFORMACIÓN INSUFICIENTE']
    start, end = date_from.isoformat() + ' 00:00:00', date_to.isoformat() + ' 23:59:59'
    observed = cx.execute('''SELECT COUNT(*) AS events,COUNT(DISTINCT person_id||'|'||substr(occurred_at,1,10)) AS person_days,
        COUNT(DISTINCT person_id) AS identities,COUNT(DISTINCT NULLIF(device_sn,'')) AS devices
        FROM biometric_events WHERE occurred_at>=? AND occurred_at<=?''',(start,end)).fetchone()
    unlinked = cx.execute('''SELECT COUNT(DISTINCT p.person_id) AS identities,COUNT(e.id) AS events FROM biometric_persons p
        JOIN biometric_events e ON e.person_id=p.person_id WHERE p.link_status<>'CONFIRMED' AND COALESCE((SELECT classification FROM biometric_roster_reviews r WHERE r.person_id=p.person_id ORDER BY r.id DESC LIMIT 1),'PENDING')<>'NON_EMPLOYEE'
        AND e.occurred_at>=? AND e.occurred_at<=?''',(start,end)).fetchone()
    stats['source_events'] = observed['events']
    stats['source_person_days'] = observed['person_days']
    stats['source_identities'] = observed['identities']
    stats['source_devices'] = observed['devices']
    stats['unlinked_identities'] = unlinked['identities']
    stats['unlinked_events'] = unlinked['events']
    stats['manual_source_events'] = sum(
        1 for row in cx.execute('SELECT event_type,source FROM biometric_events WHERE occurred_at>=? AND occurred_at<=?',(start,end))
        if 'administrador' in normalize(row['source']) or 'manual' in normalize(row['event_type']))
    return {'run': dict(run), 'rows': evaluated, 'stats': stats, 'is_partial': is_partial,
            'source_coverage_reviewed': reviewed}


def latest_import_summary(cx):
    batch = cx.execute('''SELECT b.*,u.name AS importer_name FROM import_batches b
        LEFT JOIN users u ON u.id=b.imported_by ORDER BY b.id DESC LIMIT 1''').fetchone()
    if not batch:
        return None
    return dict(batch)


def enqueue_request_recalculation(cx,request_row,reason='Cambio de etapa de aprobación'):
    r=dict(request_row)
    start=r.get('starts_at') or r.get('correction_at');end=r.get('ends_at') or r.get('correction_at')
    cx.execute('''INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,request_id,reason,created_at)
        VALUES(?,?,?,?,?,?)''',(r['user_id'],start,end,r['id'],reason,nowstr()))


def drain_recalculations(cx):
    from core import audit
    from attendance_engine import context
    jobs=cx.execute('SELECT * FROM attendance_recalc_queue WHERE processed_at IS NULL ORDER BY id').fetchall()
    affected={}
    for job in jobs:
        if not job['starts_at'] or not job['ends_at']:
            cx.execute('UPDATE attendance_recalc_queue SET processed_at=? WHERE id=?',(nowstr(),job['id']));continue
        first=date.fromisoformat(job['starts_at'][:10]);last=date.fromisoformat(job['ends_at'][:10])
        person=cx.execute('SELECT * FROM users WHERE id=?',(job['user_id'],)).fetchone()
        prev=context(cx,person,first-timedelta(days=1))
        from attendance_grouping import boundary_allowance
        allowance=boundary_allowance(cx,person,first-timedelta(days=1),prev)
        # Date-only jobs already name a work journey (event ownership was resolved by the importer).
        # Request timestamps, in contrast, may overlap the tail of the preceding night shift.
        if (len(job['starts_at'])>10 and prev['segments'] and prev['segments'][-1][1].date()==first
            and datetime.fromisoformat(job['starts_at'])<=prev['segments'][-1][1]+timedelta(seconds=allowance)):
            first-=timedelta(days=1)
        for offset in range((last-first).days+1):affected[(job['user_id'],first+timedelta(days=offset))]=job
    for (uid,day),job in affected.items():
        actor=cx.execute('SELECT decided_by FROM requests WHERE id=?',(job['request_id'],)).fetchone()
        result=reconcile(cx,day,day,actor['decided_by'] if actor else None,user_ids=[uid],
                         reason=job['reason'],request_id=job['request_id'])
        audit(cx,actor['decided_by'] if actor else None,'RECONCILIACION_SOLICITUD','rango',result['run']['id'],
              f'Solicitud {job["request_id"]}; usuario={uid}; fecha={day}; revisión provisional, sin cambiar cierres')
    for job in jobs:cx.execute('UPDATE attendance_recalc_queue SET processed_at=? WHERE id=?',(nowstr(),job['id']))
    return {'affected':[(uid,day.isoformat()) for uid,day in affected]}


def reconciliation_employees(cx, date_from, date_to):
    return cx.execute('''SELECT u.* FROM users u WHERE u.role IN ('employee','manager','hr') AND
        NOT EXISTS(SELECT 1 FROM local_staff_profiles lp WHERE lp.user_id=u.id AND lp.account_kind='OPERATIVA') AND
        (u.active=1 OR EXISTS(SELECT 1 FROM attendance_employment_periods p WHERE p.user_id=u.id
          AND p.confirmed=1 AND p.valid_from<=? AND (p.valid_to IS NULL OR p.valid_to>=?)))
        ORDER BY u.name,u.id''',(date_to.isoformat(),date_from.isoformat())).fetchall()


def report_consolidation_status(cx, date_from: date, date_to: date) -> dict:
    """Readiness gate for making a monthly report official when ZKBio data exists."""
    start, end = date_from.isoformat(), date_to.isoformat()
    event_count = cx.execute('''SELECT COUNT(*) FROM biometric_events
        WHERE occurred_at>=? AND occurred_at<=?''', (start+' 00:00:00', end+' 23:59:59')).fetchone()[0]
    from attendance_continuous import enabled
    has_receipt=enabled(cx) and cx.execute('''SELECT 1 FROM attendance_import_receipts
        WHERE requested_from<=? AND requested_to>=? LIMIT 1''',(end,start)).fetchone()
    local_scope=cx.execute('SELECT 1 FROM local_preparation LIMIT 1').fetchone()
    result = {'source_events': event_count, 'required': bool(event_count or has_receipt or local_scope), 'ready': True,
              'run_id': None, 'blockers': []}
    if not result['required']:
        return result

    blockers = result['blockers']
    day = date_from
    missing_coverage = 0
    while day <= date_to:
        if not _coverage_reviewed(cx, day.isoformat()):
            missing_coverage += 1
        day += timedelta(days=1)
    if missing_coverage:
        blockers.append(f'Cobertura ZKBio sin confirmar para {missing_coverage} dias del periodo.')

    errors = cx.execute('''SELECT COUNT(*) FROM import_batches WHERE file_sha256<>''
        AND coverage_start<=? AND coverage_end>=? AND rows_error>0''', (end, start)).fetchone()[0]
    if errors:
        blockers.append(f'Hay {errors} lote(s) con filas observadas en el período.')

    unlinked = cx.execute('''SELECT COUNT(DISTINCT e.person_id) FROM biometric_events e
        LEFT JOIN biometric_identity_links l ON l.person_id=e.person_id
          AND l.valid_from<=substr(e.occurred_at,1,10)
          AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
        WHERE e.occurred_at>=? AND e.occurred_at<=? AND l.user_id IS NULL
        AND COALESCE((SELECT classification FROM biometric_roster_reviews r
          WHERE r.person_id=e.person_id ORDER BY r.id DESC LIMIT 1),'PENDING')<>'NON_EMPLOYEE' ''',
        (start+' 00:00:00', end+' 23:59:59')).fetchone()[0]
    if unlinked:
        blockers.append(f'{unlinked} identidad(es) biometrica(s) sin vinculo vigente con el padron.')

    cross_source_duplicates = cx.execute('''SELECT COUNT(*) FROM (
        SELECT user_id,occurred_at FROM (
            SELECT l.user_id,e.occurred_at,'ZKBIO' source_kind FROM biometric_events e
            JOIN biometric_identity_links l ON l.person_id=e.person_id
              AND l.valid_from<=substr(e.occurred_at,1,10)
              AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
            WHERE e.occurred_at>=? AND e.occurred_at<=?
            UNION ALL
            SELECT user_id,occurred_at,'LEGACY' source_kind FROM punches WHERE occurred_at>=? AND occurred_at<=?
        ) GROUP BY user_id,occurred_at HAVING COUNT(DISTINCT source_kind)>1
    )''', (start+' 00:00:00', end+' 23:59:59', start+' 00:00:00', end+' 23:59:59')).fetchone()[0]
    if cross_source_duplicates:
        blockers.append(f'{cross_source_duplicates} marca(s) aparecen en los dos importadores; revisar posible doble conteo.')

    employees=reconciliation_employees(cx,date_from,date_to)
    saved=cx.execute('''SELECT d.*,c.stale,c.version_id FROM attendance_daily_current c
        JOIN attendance_daily_versions v ON v.id=c.version_id JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id
        WHERE c.work_date BETWEEN ? AND ? ORDER BY c.work_date,c.user_id''',(start,end)).fetchall()
    fresh={(person['id'],(date_from+timedelta(days=i)).isoformat())
           for person in employees for i in range((date_to-date_from).days+1)}
    from attendance_continuous import enabled
    dirty=enabled(cx) and cx.execute('SELECT 1 FROM attendance_source_changes WHERE processed_at IS NULL LIMIT 1').fetchone()
    if dirty or len(saved)!=len(fresh) or any((r['user_id'],r['work_date']) not in fresh or r['stale'] for r in saved):
        blockers.append('Hay jornadas sin recalcular o fuentes desactualizadas; actualiza las evaluaciones pendientes.')
    run_ids=sorted({r['run_id'] for r in saved})
    result['run_id']=run_ids[0] if len(run_ids)==1 else None
    result['run_ids']=run_ids
    result['version_ids']=[r['version_id'] for r in saved]
    if date_from.day!=26 or date_to!=(date_from.replace(day=1)+timedelta(days=32)).replace(day=25):
        blockers.append('El rango es provisional y no representa un ciclo oficial completo 26-25.')
    linked=cx.execute('''SELECT DISTINCT l.user_id,substr(e.occurred_at,1,10) day FROM biometric_events e
        JOIN biometric_identity_links l ON l.person_id=e.person_id AND l.valid_from<=substr(e.occurred_at,1,10)
        AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10)) WHERE e.occurred_at>=? AND e.occurred_at<=?''',
        (start+' 00:00:00',end+' 23:59:59')).fetchall()
    if any((r['user_id'],r['day']) not in fresh for r in linked):
        blockers.append('Hay personas con eventos vinculados que no estan incluidas en el padron evaluado.')
    reference_pending=cx.execute('''SELECT COUNT(DISTINCT p.id) FROM hr_roster_proposals p JOIN hr_reference_days d ON d.proposal_id=p.id
        WHERE d.work_date BETWEEN ? AND ? AND (p.user_id IS NULL OR p.state<>'CONFIRMED')''',(start,end)).fetchone()[0]
    if reference_pending:blockers.append(f'{reference_pending} perfiles historicos de RRHH siguen pendientes de vinculacion/verificacion.')
    unresolved=Counter()
    evidence_pending=0
    allowed={'PRESENCIA REGISTRADA','AUSENCIA JUSTIFICADA','NO LABORABLE','FUERA DE RELACIÓN LABORAL','FALTA CONFIRMADA POR RR. HH.'}
    for row in saved:
        if row['operational_result'] not in allowed:unresolved[row['operational_result']]+=1
        detail=json.loads(row['detail_json'])
        if (not detail.get('employment_confirmed') or not detail.get('placement_confirmed')
            or (row['operational_result'] not in ('NO LABORABLE','FUERA DE RELACIÓN LABORAL') and
                detail.get('journey',{}).get('state')!='JORNADA FINALIZADA')
            or row['work_obligation']=='DESCONOCIDA' or detail.get('pending_requests')
            or detail.get('unverified_requests') or detail.get('calendar_pending_eligibility')
            or any(not r['authorization']['valid'] and r['status']=='PENDIENTE' for r in detail.get('correction_requests',[]))
            or (detail.get('effective_punch_count',row['punch_count'])==1 and detail.get('regime_code')!='MOBILE_NO_FIXED_HOURS')
            or detail.get('grouping',{}).get('missing_movements')
            or (detail.get('approved_requests') and row['punch_count'])
            or any('administrador' in normalize(e.get('source')) for e in detail.get('events',[]))):evidence_pending+=1
    if evidence_pending:blockers.append(f'{evidence_pending} jornadas tienen evidencia que RRHH debe revisar.')
    for state,n in unresolved.items():blockers.append(f'{n} jornadas pendientes: {state}.')

    result['ready'] = not blockers
    return result


def pending_identities(cx, limit=100):
    return [dict(row) for row in cx.execute('''SELECT p.person_id,p.display_name,p.department,p.candidate_note,
        p.link_status,COUNT(e.id) AS event_count,COUNT(DISTINCT substr(e.occurred_at,1,10)) AS active_days,
        MIN(e.occurred_at) AS first_event,MAX(e.occurred_at) AS last_event,
        COUNT(DISTINCT e.device_sn) AS device_count
        FROM biometric_persons p LEFT JOIN biometric_events e ON e.person_id=p.person_id
        WHERE p.link_status<>'CONFIRMED' AND COALESCE((SELECT classification FROM biometric_roster_reviews r
            WHERE r.person_id=p.person_id ORDER BY r.id DESC LIMIT 1),'PENDING')<>'NON_EMPLOYEE'
        GROUP BY p.person_id
        ORDER BY event_count DESC,p.person_id LIMIT ?''', (limit,))]

