"""Private test fixture, independent from local operational data and credentials."""
from datetime import timedelta
import hashlib
from core import initialize,db,hash_password,nowstr,now,month_end_for_day,period_start,audit

def seed_legacy_fixture():
    """Isolated legacy fixture for tests; never invoked by application startup or local preparation."""
    initialize()
    with db() as cx:
        if cx.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']:
            raise RuntimeError('No se puede cargar demo: ya existen usuarios.')
        for a in ('Administracion', 'Operaciones', 'Recursos Humanos'):
            cx.execute('INSERT INTO areas(name) VALUES (?)', (a,))
        amap = {r['name']: r['id'] for r in cx.execute('SELECT * FROM areas')}
        people = [
            ('RRHH001','María Gómez','5683941','Responsable de personal','Recursos Humanos','rrhh@demo.local','hr'),
            ('GER001','Luis Fernández','6792143','Encargado de área','Operaciones','encargado@demo.local','manager'),
            ('EMP001','Ana Pérez','5213874','Técnica administrativa','Operaciones','ana@demo.local','employee'),
            ('EMP002','Carlos Quispe','7385216','Auxiliar administrativo','Operaciones','carlos@demo.local','employee'),
            ('ADM001','Sofía Rojas','6849231','Administradora del sistema','Administracion','admin@demo.local','admin'),
        ]
        for code, name, ci, position, area, email, role in people:
            cx.execute('''INSERT INTO users(employee_code,name,document_id,position,area_id,email,role,password_hash,created_at)
              VALUES(?,?,?,?,?,?,?,?,?)''',
              (code,name,ci,position,amap[area],email,role,hash_password('Demo2026!#'),nowstr()))
        ids={r['employee_code']:r['id'] for r in cx.execute('SELECT id, employee_code FROM users')}
        for person in cx.execute('SELECT id,area_id,created_at FROM users').fetchall():
            cx.execute('''INSERT INTO employee_placements
                (user_id,area_id,valid_from,classification_status,created_at)
                VALUES(?,?,?,'PENDIENTE',?)''',
                (person['id'],person['area_id'],'2020-01-01',person['created_at']))
        manager = ids['GER001']
        cx.execute('''INSERT INTO area_responsibles(area_id,user_id,role,valid_from,assigned_by,created_at)
                       VALUES(?,?,'PRINCIPAL',?, ?, ?)''',
                   (amap['Operaciones'],manager,'2020-01-01',ids['ADM001'],nowstr()))
        cx.execute('''UPDATE employee_placements SET supervisor_id=?,classification_status='PENDIENTE'
                      WHERE user_id IN (?,?) AND valid_to IS NULL''',
                   (manager,ids['EMP001'],ids['EMP002']))
        # Ejemplos ficticios del periodo actual
        this_end = month_end_for_day(now().date())
        p_start = period_start(this_end)
        sample_day = max(p_start, min(now().date()-timedelta(days=2),this_end))
        if sample_day.weekday() >= 5:
            sample_day-=timedelta(days=sample_day.weekday()-4)
        for code, values in {'EMP001':[('08:02','IN'),('17:04','OUT')],
                             'EMP002':[('08:24','IN'),('17:07','OUT')],
                             'GER001':[('08:01','IN'),('17:02','OUT')]}.items():
            for hhmm, kind in values:
                dt=f'{sample_day.isoformat()} {hhmm}:00'
                sk=hashlib.sha256(f'{code}|{dt}|{kind}|demo'.encode()).hexdigest()
                cx.execute('''INSERT INTO punches(user_id,occurred_at,kind,source,source_key,imported_by,created_at)
                              VALUES(?,?,?,?,?,?,?)''',(ids[code],dt,kind,'DEMO',sk,ids['ADM001'],nowstr()))
        future=max(now().date()+timedelta(days=1),p_start)
        while future.weekday()>=5:
            future+=timedelta(days=1)
        starts=f'{future} 10:00:00'; ends=f'{future} 12:00:00'
        cur=cx.execute('''INSERT INTO requests(user_id,approver_id,kind,leave_type,starts_at,ends_at,
          place,contact,pay_type,justification,status,submitted_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
          (ids['EMP001'],ids['GER001'],'PERMISO','MEDICO',starts,ends,'Centro médico','',
           'CON_GOCE','Consulta médica programada (ejemplo ficticio)',
           'PENDIENTE',nowstr()))
        rid=cur.lastrowid
        cx.execute('UPDATE requests SET folio=? WHERE id=?',(f'BP-{now().year}-{rid:06d}',rid))
        from app import create_approval_steps
        create_approval_steps(cx,rid,'PERMISO',manager)
        audit(cx,ids['ADM001'],'SEED','demo','1','Datos y usuarios de prueba; no corresponden a una entidad real.')
