"""Reglas de asistencia, base de datos y auditoria; horario de Bolivia."""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

from migrations import MIGRATIONS, apply_migrations

TZ = ZoneInfo('America/La_Paz')
BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('ASISTENCIA_DB', str(BASE / 'instance' / 'kimtek.sqlite3')))
LEAVE_TYPES = {
    'PERSONAL': 'Permiso personal o particular',
    'MEDICO': 'Cita médica',
    'DESCANSO_MEDICO': 'Descanso médico',
    'COMISION': 'Comisión de servicio',
    'CAPACITACION': 'Capacitación',
    'FAMILIAR': 'Asunto familiar / emergencia',
    'VACACIONES': 'Vacaciones',
    'OTRO': 'Otro motivo justificado',
}
STATUS_LABELS = {
    'PUNTUAL': 'Puntual', 'ATRASO': 'Atraso',
    'SALIDA_ANTICIPADA': 'Salida anticipada',
    'ATRASO_Y_SALIDA': 'Atraso y salida anticipada',
    'INCOMPLETA': 'Marcación incompleta', 'FALTA': 'Falta sin justificar',
    'JUSTIFICADO': 'Ausencia justificada',
    'JUSTIFICADO_PARCIAL': 'Permiso parcial / incidencia',
    'ASUETO': 'Asueto', 'LIBRE': 'Día libre',
    'EN_CURSO': 'Jornada en curso', 'NO_LABORAL': 'No laboral',
    'FUTURO': 'Próximo día',
}

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS areas (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 employee_code TEXT UNIQUE NOT NULL,
 name TEXT NOT NULL,
 document_id TEXT NOT NULL DEFAULT '',
 position TEXT NOT NULL DEFAULT '',
 area_id INTEGER NOT NULL REFERENCES areas(id),
 email TEXT UNIQUE NOT NULL,
 role TEXT NOT NULL DEFAULT 'employee' CHECK(role IN ('employee','manager','hr','admin')),
 password_hash TEXT NOT NULL,
 start_time TEXT NOT NULL DEFAULT '08:00',
 end_time TEXT NOT NULL DEFAULT '17:00',
 grace_minutes INTEGER NOT NULL DEFAULT 5,
 active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS punches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id),
 occurred_at TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('IN','OUT','UNSPEC')),
 source TEXT NOT NULL,
 source_key TEXT NOT NULL UNIQUE,
 imported_by INTEGER REFERENCES users(id),
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_punches_user_day ON punches(user_id, occurred_at);
CREATE TABLE IF NOT EXISTS requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 folio TEXT UNIQUE,
 user_id INTEGER NOT NULL REFERENCES users(id),
 approver_id INTEGER NOT NULL REFERENCES users(id),
 kind TEXT NOT NULL CHECK(kind IN ('PERMISO','CORRECCION')),
 leave_type TEXT NOT NULL DEFAULT '',
 starts_at TEXT NOT NULL DEFAULT '',
 ends_at TEXT NOT NULL DEFAULT '',
 place TEXT NOT NULL DEFAULT '',
 contact TEXT NOT NULL DEFAULT '',
 pay_type TEXT NOT NULL DEFAULT 'CON_GOCE',
 compensation TEXT NOT NULL DEFAULT '',
 justification TEXT NOT NULL,
 correction_kind TEXT NOT NULL DEFAULT '',
 correction_at TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'PENDIENTE' CHECK(status IN ('PENDIENTE','APROBADA','RECHAZADA')),
 submitted_at TEXT NOT NULL,
 decided_by INTEGER REFERENCES users(id),
 decided_at TEXT,
 decision_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_requests_approver ON requests(approver_id,status);
CREATE TABLE IF NOT EXISTS calendar_exceptions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 date TEXT NOT NULL, user_id INTEGER REFERENCES users(id),
 kind TEXT NOT NULL CHECK(kind IN ('ASUETO','LIBRE')),
 description TEXT NOT NULL,
 created_by INTEGER NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monthly_closures (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 period_start TEXT NOT NULL,
 period_end TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('CERRADO','REABIERTO')),
 closed_at TEXT NOT NULL,
 closed_by INTEGER REFERENCES users(id),
 snapshot_json TEXT NOT NULL,
 reopened_at TEXT,
 reopened_by INTEGER REFERENCES users(id),
 reopen_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 actor_id INTEGER REFERENCES users(id),
 action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
 details TEXT NOT NULL, at TEXT NOT NULL
);
'''


def now() -> datetime:
    return datetime.now(TZ)


def nowstr() -> str:
    return now().strftime('%Y-%m-%d %H:%M:%S')


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    cx = sqlite3.connect(DB_PATH, timeout=15)
    cx.row_factory = sqlite3.Row
    cx.execute('PRAGMA foreign_keys=ON')
    cx.execute('PRAGMA busy_timeout=15000')
    try:
        from attendance_continuous import enabled,refresh_expired,capture_changes
        if enabled(cx):
            refresh_expired(cx)
            capture_changes(cx)
            if cx.execute('SELECT 1 FROM attendance_recalc_queue WHERE processed_at IS NULL LIMIT 1').fetchone():
                from reconciliation import drain_recalculations
                drain_recalculations(cx)
        yield cx
        if cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attendance_recalc_queue'").fetchone():
            if enabled(cx):capture_changes(cx)
            if cx.execute('SELECT 1 FROM attendance_recalc_queue WHERE processed_at IS NULL LIMIT 1').fetchone():
                from reconciliation import drain_recalculations
                drain_recalculations(cx)
        cx.commit()
    except Exception:
        cx.rollback()
        raise
    finally:
        cx.close()


def initialize():
    if DB_PATH.exists() and DB_PATH.stat().st_size:
        with closing(sqlite3.connect(DB_PATH)) as check:
            has_tables = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            has_migrations = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            current = check.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0] if has_migrations else 0
        latest=len(MIGRATIONS)
        if has_tables and current < latest:
            label=None if current==0 else f'v{current}-to-v{latest}'
            backup_before_migration(DB_PATH,label)
    with db() as cx:
        cx.executescript(SCHEMA)
        apply_migrations(cx)


def backup_before_migration(path: Path, label: str | None = None) -> Path:
    """Create and verify an online SQLite backup before the first schema migration."""
    backup_dir = path.parent / 'backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime('%Y%m%d-%H%M%S')
    suffix=f'-pre-{label}' if label else '-pre-migration'
    destination = backup_dir / f'{path.stem}{suffix}-{stamp}{path.suffix}'
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination)) as target:
        source_tables = [row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        backup_tables = [row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        source_counts = {
            table: source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in source_tables
        }
        backup_counts = {
            table: target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in backup_tables
        }
        integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
    if source_tables != backup_tables or source_counts != backup_counts or integrity != 'ok':
        destination.unlink(missing_ok=True)
        raise sqlite3.DatabaseError('La copia previa a migración no superó la verificación.')
    return destination


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError('La contraseña debe tener al menos 10 caracteres.')
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2**14, r=8, p=1)
    return 'scrypt$' + salt.hex() + '$' + key.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt, encoded = stored.split('$', 2)
        if algorithm != 'scrypt':
            return False
        key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(key, bytes.fromhex(encoded))
    except (ValueError, TypeError):
        return False


def audit(cx, actor_id, action, entity, entity_id, details=''):
    cx.execute('INSERT INTO audit_log(actor_id,action,entity,entity_id,details,at) VALUES(?,?,?,?,?,?)',
               (actor_id, action, entity, str(entity_id), details, nowstr()))


def notify(cx, user_id, kind, message, entity='', entity_id='', dedupe_key=None):
    # Existing notices must not leak global HR work to an Administration account sharing role='hr'.
    from access_control import capabilities,can_view_request
    recipient=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if recipient and recipient['role']=='hr' and 'hr.global' not in capabilities(cx,recipient):
        if entity=='solicitud':
            related=cx.execute('SELECT * FROM requests WHERE id=?',(entity_id,)).fetchone()
            if not related or not can_view_request(cx,recipient,related): return
        else: return
    if dedupe_key is None:
        dedupe_key=f'{kind}:{entity}:{entity_id}:{message}'
    cx.execute('''INSERT OR IGNORE INTO internal_notifications
                  (user_id,kind,message,entity,entity_id,created_at,dedupe_key)
                  VALUES(?,?,?,?,?,?,?)''',
               (user_id, kind, message, entity, str(entity_id), nowstr(), dedupe_key))


def month_end_for_day(d: date) -> date:
    if d.day <= 25:
        return d.replace(day=25)
    m = 1 if d.month == 12 else d.month + 1
    y = d.year + (1 if d.month == 12 else 0)
    return date(y, m, 25)


def period_start(end: date) -> date:
    if end.day != 25:
        raise ValueError('El cierre siempre corresponde al dia 25.')
    if end.month == 1:
        return date(end.year - 1, 12, 26)
    return date(end.year, end.month - 1, 26)


def date_range(start: date, end: date):
    for offset in range((end-start).days + 1):
        yield start + timedelta(days=offset)


def closed_for_date(cx, d: date) -> bool:
    end = month_end_for_day(d).isoformat()
    row = cx.execute('SELECT state FROM monthly_closures WHERE period_end=?', (end,)).fetchone()
    return row is not None and row['state']=='CERRADO'


def blocked_range(cx, first: date, last: date) -> bool:
    # Solicitudes de hasta 31 dias; evita recorrer dias innecesarios.
    d = first
    while d <= last:
        if closed_for_date(cx, d):
            return True
        d = month_end_for_day(d) + timedelta(days=1)
    return False


def _all_effective_times(cx, user_id, d):
    start=f'{d.isoformat()} 00:00'; end=f'{d.isoformat()} 23:59:59'
    raw=cx.execute('''SELECT occurred_at,kind FROM punches WHERE user_id=?
                      AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at''', (user_id,start,end)).fetchall()
    amendments=cx.execute('''SELECT correction_at AS occurred_at, correction_kind AS kind
                             FROM requests WHERE user_id=? AND kind='CORRECCION' AND status='APROBADA'
                             AND correction_at>=? AND correction_at<=? ORDER BY correction_at''',
                             (user_id,start,end)).fetchall()
    records=[(r['occurred_at'],r['kind'],'BIO') for r in raw]
    records.extend((r['occurred_at'],r['kind'],'CORRECCION') for r in amendments)
    records.sort(key=lambda r:r[0])
    return records, len(raw),len(amendments)


def _approved_leaves(cx, user_id, day):
    return cx.execute('''SELECT * FROM requests WHERE user_id=? AND kind='PERMISO' AND status='APROBADA'
        AND starts_at<=? AND ends_at>=?''',(user_id, f'{day} 23:59:59', f'{day} 00:00:00')).fetchall()


def _covers(leaves, start, end):
    return any(r['starts_at'] <= start and r['ends_at'] >= end for r in leaves)


def schedule_for_day(cx, employee, d: date):
    assignment = cx.execute('''SELECT d.is_working,d.start_time,d.end_time,d.grace_minutes
        FROM schedule_assignments s
        JOIN work_schedule_days d ON d.schedule_id=s.schedule_id AND d.weekday=?
        WHERE s.user_id=? AND s.valid_from<=? AND (s.valid_to IS NULL OR s.valid_to>=?)
        ORDER BY s.valid_from DESC,s.id DESC LIMIT 1''',
        (d.weekday(),employee['id'],d.isoformat(),d.isoformat())).fetchone()
    if assignment is None:
        assignment = cx.execute('''SELECT d.is_working,d.start_time,d.end_time,d.grace_minutes
            FROM schedule_assignments s
            JOIN work_schedule_days d ON d.schedule_id=s.schedule_id AND d.weekday=?
            WHERE s.area_id=? AND s.valid_from<=? AND (s.valid_to IS NULL OR s.valid_to>=?)
            ORDER BY s.valid_from DESC,s.id DESC LIMIT 1''',
            (d.weekday(),employee['area_id'],d.isoformat(),d.isoformat())).fetchone()
    if assignment:
        return {
            'working': bool(assignment['is_working']),
            'start_time': assignment['start_time'] or '',
            'end_time': assignment['end_time'] or '',
            'grace_minutes': assignment['grace_minutes'],
        }
    return {
        'working': d.weekday() < 5,
        'start_time': employee['start_time'],
        'end_time': employee['end_time'],
        'grace_minutes': employee['grace_minutes'],
    }


def daily_status(cx, employee, d: date, as_of: datetime | None = None):
    from attendance_readmodel import daily_result
    return daily_result(cx,employee,d,as_of)


def legacy_daily_status(cx, employee, d: date, as_of: datetime | None = None):
    as_of=as_of or now()
    schedule = schedule_for_day(cx,employee,d)
    start = f"{d.isoformat()} {schedule['start_time']}:00"
    finish = f"{d.isoformat()} {schedule['end_time']}:00"
    raw, orig_count, amendments = _all_effective_times(cx, employee['id'],d)
    leaves = _approved_leaves(cx, employee['id'], d)
    ex = cx.execute('''SELECT kind, description FROM calendar_exceptions
      WHERE date=? AND (user_id=? OR user_id IS NULL) ORDER BY user_id DESC LIMIT 1''',
      (d.isoformat(),employee['id'])).fetchone()
    entry = None; exit_ = None
    raw_in=[x for x in raw if x[1]=='IN']
    raw_out=[x for x in raw if x[1]=='OUT']
    unknown=[x for x in raw if x[1]=='UNSPEC']
    if raw_in:
        entry=min(raw_in,key=lambda x:x[0])[0]
    elif unknown:
        entry=unknown[0][0]
    if raw_out:
        exit_=max(raw_out,key=lambda x:x[0])[0]
    elif unknown:
        candidates=[x[0] for x in unknown if x[0] != entry]
        exit_=max(candidates) if candidates else None
    if entry and exit_ and exit_ <= entry:
        exit_=None
    item={'fecha':d.isoformat(),'empleado':employee['name'],'codigo':employee['employee_code'],
          'area':employee['area_name'],'entrada':entry[11:16] if entry else '',
          'salida':exit_[11:16] if exit_ else '', 'originales':orig_count,
          'correcciones':amendments,'permiso_aprobado':bool(leaves),'observacion':'','estado':''}
    if ex:
        item['estado']=ex['kind'];item['observacion']=ex['description']
    elif not schedule['working']:
        item['estado']='NO_LABORAL'
    elif _covers(leaves, start, finish):
        item['estado']='JUSTIFICADO'
        item['observacion']='Permiso que cubre toda la jornada'
    elif d > as_of.date():
        item['estado']='FUTURO'
    elif not raw:
        if d == as_of.date() and as_of.strftime('%H:%M:%S') < employee['end_time']+':00':
            item['estado']='EN_CURSO'
        elif leaves:
            item['estado']='JUSTIFICADO_PARCIAL'
            item['observacion']='Permiso parcial; revisar el resto de la jornada'
        else:
            item['estado']='FALTA'
    elif not entry or not exit_:
        if d == as_of.date() and as_of.strftime('%H:%M:%S') < employee['end_time']+':00':
            item['estado']='EN_CURSO'
        else:
            item['estado']='INCOMPLETA'
            item['observacion']='Falta una marcacion. No se infiere ni inventa una hora'
    else:
        grace = timedelta(minutes=schedule['grace_minutes'])
        late = datetime.fromisoformat(entry) > datetime.fromisoformat(start)+grace
        early = datetime.fromisoformat(exit_) < datetime.fromisoformat(finish)
        if late and _covers(leaves,start,entry):
            late=False
        if early and _covers(leaves,exit_,finish):
            early=False
        item['estado']=('ATRASO_Y_SALIDA' if late and early else 'ATRASO' if late else
                        'SALIDA_ANTICIPADA' if early else 'PUNTUAL')
    item['estado_texto']=STATUS_LABELS[item['estado']]
    return item


def report(cx, ending: date, as_of: datetime | None = None):
    start=period_start(ending)
    from attendance_continuous import enabled
    continuous=enabled(cx) and (cx.execute('''SELECT 1 FROM attendance_import_receipts
        WHERE requested_from<=? AND requested_to>=? LIMIT 1''',(ending.isoformat(),start.isoformat())).fetchone()
        or cx.execute('SELECT 1 FROM local_preparation LIMIT 1').fetchone())
    if cx.execute('SELECT 1 FROM biometric_events WHERE occurred_at>=? AND occurred_at<=? LIMIT 1',
                  (start.isoformat()+' 00:00:00',ending.isoformat()+' 23:59:59')).fetchone() or continuous:
        from attendance_reporting import report_from_reconciliation
        return report_from_reconciliation(cx,ending)
    people=cx.execute('''SELECT u.*, a.name AS area_name FROM users u
                         JOIN areas a ON a.id=u.area_id WHERE u.active=1
                         ORDER BY a.name,u.name''').fetchall()
    if cx.execute('SELECT 1 FROM biometric_events WHERE occurred_at>=? AND occurred_at<=? LIMIT 1',
                  (start.isoformat()+' 00:00:00',ending.isoformat()+' 23:59:59')).fetchone():
        from reconciliation import reconciliation_employees
        people=[]
        for employee in reconciliation_employees(cx,start,ending):
            person=dict(employee)
            area=cx.execute('SELECT name FROM areas WHERE id=?',(person['area_id'],)).fetchone()
            person['area_name']=area['name'] if area else ''
            people.append(person)
    rows=[]; summary=[]
    for u in people:
        counts={k:0 for k in STATUS_LABELS}
        for d in date_range(start, ending):
            r=daily_status(cx,u,d,as_of)
            r['user_id']=u['id']
            rows.append(r);counts[r['estado']]+=1
        summary.append({'_user_id':u['id'],'codigo':u['employee_code'],'empleado':u['name'],'area':u['area_name'],
          'puntual':counts['PUNTUAL'],'atrasos':counts['ATRASO']+counts['ATRASO_Y_SALIDA'],
          'salidas_anticipadas':counts['SALIDA_ANTICIPADA']+counts['ATRASO_Y_SALIDA'],
          'faltas':counts['FALTA'],'incompletas':counts['INCOMPLETA'],
          'justificados':counts['JUSTIFICADO'],
          'parciales':counts['JUSTIFICADO_PARCIAL'],
          'en_curso':counts['EN_CURSO']})
    from reconciliation import report_consolidation_status
    consolidation=report_consolidation_status(cx,start,ending)
    run=None
    if consolidation['required'] and consolidation['ready'] and consolidation['run_id']:
        run=cx.execute('SELECT id FROM attendance_reconciliation_runs WHERE id=?',
                        (consolidation['run_id'],)).fetchone()
    if run:
        outcomes={(r['user_id'],r['work_date']):dict(r) for r in cx.execute('''
            SELECT user_id,work_date,operational_result,justification_status,work_obligation,
                   punch_count,source_summary,reason
            FROM attendance_reconciliation_days WHERE run_id=?''',(run['id'],))}
        reconciled_users={user_id for user_id,_ in outcomes}
        rows=[item for item in rows if item['user_id'] in reconciled_users]
        summary=[item for item in summary if item['_user_id'] in reconciled_users]
        counts_by_user={}
        for item in rows:
            outcome=outcomes.get((item.get('user_id'),item['fecha']))
            if not outcome:
                continue
            item.update({
                'reconciliation_result':outcome['operational_result'],
                'reconciliation_justification':outcome['justification_status'],
                'reconciliation_obligation':outcome['work_obligation'],
                'reconciliation_punch_count':outcome['punch_count'],
                'reconciliation_sources':outcome['source_summary'],
                'reconciliation_reason':outcome['reason'],
                'entrada':'','salida':'','estado_texto':outcome['operational_result'],
                'observacion':outcome['reason'],
            })
            counts=counts_by_user.setdefault(item['user_id'],{})
            state=outcome['operational_result']
            counts[state]=counts.get(state,0)+1
        for record in summary:
            counts=counts_by_user.get(record['_user_id'],{})
            record['resultados_conciliacion']=counts
            record['conciliacion_presencia']=counts.get('PRESENCIA REGISTRADA',0)
            record['conciliacion_ausencia_justificada']=counts.get('AUSENCIA JUSTIFICADA',0)
            record['conciliacion_cobertura_parcial']=counts.get('COBERTURA PARCIAL',0)
            record['conciliacion_posible_inasistencia']=counts.get('POSIBLE INASISTENCIA',0)
            record['conciliacion_falta_confirmada']=counts.get('FALTA CONFIRMADA POR RR. HH.',0)
            record['conciliacion_no_laborable']=counts.get('NO LABORABLE',0)
            from reconciliation import normalize as normalize_attendance
            record['conciliacion_sin_resolver']=sum(v for state,v in counts.items() if normalize_attendance(state) not in
                ('presencia registrada','ausencia justificada','cobertura parcial',
                 'posible inasistencia','falta confirmada por rr hh','no laborable',
                 'fuera de relacion laboral'))
            # Do not present legacy punctuality/late calculations as if they described ZKBio events.
            record.update({'puntual':0,'atrasos':0,'salidas_anticipadas':0,
                           'faltas':counts.get('FALTA CONFIRMADA POR RR. HH.',0),
                           'incompletas':counts.get('COBERTURA PARCIAL',0),
                           'justificados':counts.get('AUSENCIA JUSTIFICADA',0),
                           'parciales':counts.get('COBERTURA PARCIAL',0),'en_curso':0})
    return {'desde':start.isoformat(),'hasta':ending.isoformat(),'generado':nowstr(),
            'detalle':rows,'resumen':summary,'empleados':len(summary),
            'consolidation':consolidation,'reconciliation_run_id':run['id'] if run else None,
            'report_mode':'CONCILIACION' if run else ('PROVISIONAL' if consolidation['required'] else 'LEGACY')}


def close_period(cx, end: date, actor_id: int | None, timestamp: datetime | None = None):
    timestamp=timestamp or now()
    closed_at=timestamp.strftime('%Y-%m-%d %H:%M:%S')
    if end.day != 25:
        raise ValueError('La fecha de cierre debe ser dia 25.')
    if timestamp.date() < end or (timestamp.date()==end and (timestamp.hour,timestamp.minute)<(23,59)):
        raise ValueError('No se puede cerrar un periodo antes del final del dia 25.')
    current=cx.execute('SELECT * FROM monthly_closures WHERE period_end=?', (end.isoformat(),)).fetchone()
    if current and current['state']=='CERRADO':
        return False
    from reconciliation import report_consolidation_status
    readiness=report_consolidation_status(cx,period_start(end),end)
    if readiness['required'] and not readiness['ready']:
        raise ValueError('Cierre bloqueado: la conciliación ZKBio no está lista. '+' '.join(readiness['blockers']))
    payload=report(cx,end,as_of=timestamp)
    snap=json.dumps(payload,ensure_ascii=False)
    if current:
        cx.execute('''UPDATE monthly_closures SET state='CERRADO',closed_at=?,closed_by=?,snapshot_json=?,
                   reconciliation_run_id=?,
                   reopened_at=NULL,reopened_by=NULL,reopen_reason='' WHERE period_end=?''',
                   (closed_at,actor_id,snap,payload.get('reconciliation_run_id'),end.isoformat()))
    else:
        cx.execute('''INSERT INTO monthly_closures(period_start,period_end,state,closed_at,closed_by,snapshot_json,
                   reconciliation_run_id) VALUES(?,?,'CERRADO',?,?,?,?)''',
                   (period_start(end).isoformat(),end.isoformat(),closed_at,actor_id,snap,payload.get('reconciliation_run_id')))
    # Se archiva un Excel completo al mismo tiempo que se congela el período.
    # Importación diferida para evitar acoplar el motor de asistencia al generador de documentos.
    from documents import excel_report
    archive_dir=DB_PATH.parent / 'reportes'
    archive_dir.mkdir(parents=True,exist_ok=True)
    archive_file=archive_dir / f'Asistencia_Cerrada_{end.isoformat()}.xlsx'
    temporary=archive_file.with_name(archive_file.name+'.tmp')
    temporary.write_bytes(excel_report(cx,payload,True))
    temporary.replace(archive_file)
    closure=cx.execute('SELECT id FROM monthly_closures WHERE period_end=?',
                       (end.isoformat(),)).fetchone()
    version=cx.execute('''SELECT COALESCE(MAX(version),0)+1
                          FROM monthly_closure_versions WHERE closure_id=?''',
                       (closure['id'],)).fetchone()[0]
    cx.execute('''INSERT INTO monthly_closure_versions
                  (closure_id,version,event_type,state,occurred_at,actor_id,snapshot_json,excel_path,reconciliation_run_id)
                  VALUES(?,?,'CERRADO','CERRADO',?,?,?,?,?)''',
               (closure['id'],version,closed_at,actor_id,snap,str(archive_file),payload.get('reconciliation_run_id')))
    audit(cx,actor_id,'CIERRE','periodo',end.isoformat(),f'Periodo cerrado. {len(payload["detalle"])} registros diarios congelados. Excel archivado.')
    for recipient in cx.execute("SELECT id FROM users WHERE active=1 AND role='hr'"):
        notify(cx,recipient['id'],'PERIODO_CERRADO',
               f'El período {period_start(end).strftime("%d/%m/%Y")} al {end.strftime("%d/%m/%Y")} fue cerrado.',
               'periodo',end.isoformat())
    return True


def reopen_period(cx, end: date, actor_id: int, reason: str):
    if len(reason.strip())<12:
        raise ValueError('Explique la reapertura con al menos 12 caracteres.')
    row=cx.execute('SELECT * FROM monthly_closures WHERE period_end=?',(end.isoformat(),)).fetchone()
    if not row or row['state']!='CERRADO':
        raise ValueError('El periodo no tiene un cierre activo.')
    reopened_at=nowstr()
    cx.execute('''UPDATE monthly_closures SET state='REABIERTO',reopened_at=?,reopened_by=?,reopen_reason=?
                  WHERE period_end=?''',(reopened_at,actor_id,reason.strip(),end.isoformat()))
    version=cx.execute('''SELECT COALESCE(MAX(version),0)+1
                          FROM monthly_closure_versions WHERE closure_id=?''',(row['id'],)).fetchone()[0]
    cx.execute('''INSERT INTO monthly_closure_versions
                  (closure_id,version,event_type,state,occurred_at,actor_id,snapshot_json,reason,reconciliation_run_id)
                  VALUES(?,?,'REABIERTO','REABIERTO',?,?,?,?,?)''',
               (row['id'],version,reopened_at,actor_id,row['snapshot_json'],reason.strip(),row['reconciliation_run_id']))
    audit(cx,actor_id,'REAPERTURA','periodo',end.isoformat(),reason.strip())
    for recipient in cx.execute("SELECT id FROM users WHERE active=1 AND role='hr'"):
        notify(cx,recipient['id'],'PERIODO_REABIERTO',
               f'El período terminado el {end.strftime("%d/%m/%Y")} fue reabierto.',
               'periodo',end.isoformat())


def get_report(cx,end):
    record=cx.execute('SELECT * FROM monthly_closures WHERE period_end=?',(end.isoformat(),)).fetchone()
    if record and record['state']=='CERRADO':
        return json.loads(record['snapshot_json']),True
    return report(cx,end),False


def may_approve(cx, actor, request_row):
    from approval_authority import permitted
    step=cx.execute("SELECT * FROM request_approval_steps WHERE request_id=? AND decision IS NULL AND is_skipped=0 ORDER BY step_order LIMIT 1",(request_row['id'],)).fetchone()
    return bool(step and permitted(cx,actor,request_row,step))


def choose_approver(cx, employee, route):
    from organization import choose
    return choose(cx,employee,now().date())


def auto_close_if_due():
    timestamp=now()
    end=date(timestamp.year,timestamp.month,25)
    if timestamp.day < 25 or (timestamp.day==25 and (timestamp.hour,timestamp.minute)<(23,59)):
        end=period_start(end)-timedelta(days=1)
    with db() as cx:
        return close_period(cx,end,None,timestamp=timestamp)
