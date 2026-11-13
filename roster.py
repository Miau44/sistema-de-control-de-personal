"""HR-confirmed roster onboarding. All writes run in the caller's transaction."""
import json
import re
from datetime import date, timedelta

from core import audit, hash_password, nowstr
from reconciliation import BRANCH_STANDARD, normalize, reconcile
import organization


def confirm_roster_identity(cx, actor, person_id, form):
    with organization.atomic(cx):
        return _confirm_roster_identity(cx,actor,person_id,form)


def _confirm_roster_identity(cx, actor, person_id, form):
    if actor['role'] != 'hr':
        raise ValueError('Solo Recursos Humanos puede confirmar el padrón.')
    identity = cx.execute('SELECT * FROM biometric_persons WHERE person_id=?', (person_id,)).fetchone()
    if not identity:
        raise ValueError('Identidad biométrica inexistente.')
    reason = str(form.get('reason', '')).strip()
    if len(reason) < 8:
        raise ValueError('Describe el respaldo revisado, con al menos 8 caracteres.')
    classification = str(form.get('classification', 'EMPLOYEE'))
    if classification not in ('EMPLOYEE', 'FORMER', 'NON_EMPLOYEE', 'PENDING'):
        raise ValueError('Clasificación no válida.')
    if classification in ('NON_EMPLOYEE', 'PENDING'):
        if cx.execute('SELECT 1 FROM biometric_identity_links WHERE person_id=?', (person_id,)).fetchone():
            raise ValueError('La identidad ya tiene vínculos históricos; revisa sus vigencias antes de reclasificarla.')
        user_id = None
    else:
        def day(key, required=True):
            value = str(form.get(key, '')).strip()
            if not value and not required:
                return None
            try:
                return date.fromisoformat(value)
            except ValueError:
                raise ValueError(f'Fecha no válida: {key}.')
        employment_from = day('employment_from')
        employment_to = day('employment_to', False)
        link_from = day('valid_from')
        link_to = day('valid_to', False)
        if employment_to and employment_to < employment_from:
            raise ValueError('La salida no puede preceder al ingreso.')
        if classification == 'FORMER' and not employment_to:
            raise ValueError('Para personal desvinculado, confirma la fecha de salida.')
        if link_from < employment_from or (employment_to and (not link_to or link_to > employment_to)):
            raise ValueError('La vigencia biométrica debe quedar dentro de la relación laboral.')
        if link_to and link_to < link_from:
            raise ValueError('La vigencia biométrica es inválida.')
        if str(form.get('confirm_evidence', '')) != '1':
            raise ValueError('Confirma expresamente identidad, relación laboral y adscripción con respaldo.')
        branch_id = int(form.get('branch_id') or 0)
        area_id = int(form.get('area_id') or 0)
        supervisor_id = int(form.get('supervisor_id')) if form.get('supervisor_id') else None
        branch = cx.execute('''SELECT b.* FROM branches b JOIN companies c ON c.id=b.company_id
            WHERE b.id=? AND b.active=1 AND b.classification_status='CLASIFICADA'
            AND c.name='KIMTEK' AND c.active=1''', (branch_id,)).fetchone()
        area = cx.execute('SELECT * FROM areas WHERE id=? AND branch_id=? AND active=1',
                          (area_id, branch_id)).fetchone()
        if not branch or not area:
            raise ValueError('Selecciona una sucursal KIMTEK y un área activa de esa sucursal.')
        mode = str(form.get('employee_mode', 'existing'))
        if mode == 'new':
            name = str(form.get('name', '')).strip()
            code = str(form.get('code', '')).strip()
            document = str(form.get('document_id', '')).strip()
            email = str(form.get('email', '')).strip().lower()
            role = str(form.get('role', 'employee'))
            if len(name) < 3 or len(code) < 2 or '@' not in email or not document:
                raise ValueError('Completa nombre, código, documento y correo del nuevo trabajador.')
            if role not in ('employee', 'manager', 'hr'):
                raise ValueError('Rol no válido para alta laboral.')
            if cx.execute('''SELECT 1 FROM users WHERE lower(employee_code)=lower(?) OR lower(email)=?
                OR (trim(document_id)<>'' AND lower(trim(document_id))=lower(?))''',
                (code, email, document)).fetchone():
                raise ValueError('Ya existe una persona con ese código, correo o documento. Usa empleado existente.')
            similar = [u for u in cx.execute('SELECT id,name FROM users') if normalize(u['name']) == normalize(name)]
            if similar and str(form.get('confirm_homonym', '')) != '1':
                raise ValueError('Existe un nombre coincidente. Verifica el documento; confirma homónimo solo si es otra persona.')
            password_hash = hash_password(str(form.get('password', '')))
            user_id = None
        elif mode == 'existing':
            user_id = int(form.get('user_id') or 0)
            user = cx.execute("SELECT * FROM users WHERE id=? AND role<>'admin'", (user_id,)).fetchone()
            if not user:
                raise ValueError('Selecciona una persona existente del padrón.')
            role = user['role']
        else:
            raise ValueError('Selecciona empleado existente o nuevo.')
        if role == 'employee' and not supervisor_id:
            raise ValueError('El trabajador necesita encargado directo confirmado.')
        if supervisor_id:
            if supervisor_id == user_id:
                raise ValueError('Una persona no puede ser su propio encargado.')
            responsible = cx.execute('''SELECT 1 FROM area_responsibles r JOIN users u ON u.id=r.user_id
                WHERE r.area_id=? AND r.user_id=? AND u.role='manager'
                AND r.valid_from<=? AND (r.valid_to IS NULL OR r.valid_to>=?)''',
                (area_id, supervisor_id, link_from.isoformat(), (link_to or link_from).isoformat())).fetchone()
            if not responsible:
                raise ValueError('El encargado debe ser responsable del área durante la vigencia indicada.')
            seen = {user_id} if user_id else set()
            current = supervisor_id
            while current:
                if current in seen:
                    raise ValueError('La relación de encargados crearía un ciclo.')
                seen.add(current)
                placement = cx.execute('''SELECT supervisor_id FROM employee_placements WHERE user_id=?
                    AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?) ORDER BY id DESC LIMIT 1''',
                    (current, link_from.isoformat(), link_from.isoformat())).fetchone()
                current = placement['supervisor_id'] if placement else None
        regime = str(form.get('regime_code', ''))
        sub = ''
        weekdays = []
        if regime == 'BRANCH_STANDARD':
            if normalize(branch['name']) not in BRANCH_STANDARD:
                raise ValueError('El calendario ordinario solo aplica a las cinco sucursales confirmadas; Kimstar requiere régimen especial.')
            weekdays = [0, 1, 2, 3, 4, 5]
        elif regime == 'KIMSTAR_ROTATING':
            sub = 'ROTATING'
        elif regime == 'MOBILE_NO_FIXED_HOURS':
            sub = str(form.get('submodality', 'PENDING'))
            if sub == 'DAYS':
                weekdays = sorted({int(x) for x in form.getlist('weekday') if str(x).isdigit() and 0 <= int(x) <= 6})
                if not weekdays:
                    raise ValueError('La modalidad A necesita días laborales confirmados.')
            elif sub == 'PENDING':
                # Confirm mobile work without asserting an unconfirmed modality or calendar.
                sub = ''
            elif sub != 'ASSIGNMENTS':
                raise ValueError('Submodalidad móvil no válida.')
        else:
            raise ValueError('Selecciona el régimen confirmado por Recursos Humanos.')
        if user_id is None:
            user_id = cx.execute('''INSERT INTO users(employee_code,name,document_id,position,area_id,email,
                role,password_hash,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (code,name,document,str(form.get('position','')).strip(),area_id,email,role,password_hash,
                 int(classification != 'FORMER'),nowstr())).lastrowid
            audit(cx,actor['id'],'ALTA','usuario',user_id,'Alta de padrón desde identidad '+person_id)
        if role!='employee' and supervisor_id is not None:
            raise ValueError('Los encargados y RR. HH. utilizan superioridad operativa, no otro encargado de área.')
        organization.validate_superior(cx,user_id,supervisor_id,link_from,role,area_id)
        first = link_from.isoformat()
        last = link_to.isoformat() if link_to else None
        overlaps = cx.execute('''SELECT * FROM biometric_identity_links WHERE person_id=?
            AND valid_from<=COALESCE(?,'9999-12-31') AND COALESCE(valid_to,'9999-12-31')>=?''',
            (person_id,last,first)).fetchall()
        if overlaps and not (len(overlaps)==1 and overlaps[0]['user_id']==user_id
                and overlaps[0]['valid_from']==first and overlaps[0]['valid_to']==last):
            raise ValueError('La vigencia se superpone con un vínculo anterior; revisa el historial.')
        placements = cx.execute('''SELECT * FROM employee_placements WHERE user_id=?
            AND valid_from<=COALESCE(?,'9999-12-31') AND COALESCE(valid_to,'9999-12-31')>=?''',
            (user_id,last,first)).fetchall()
        if placements:
            if not (len(placements)==1 and placements[0]['valid_from']<=first
                and (placements[0]['valid_to'] is None or (last is not None and placements[0]['valid_to']>=last))
                and placements[0]['classification_status']=='CLASIFICADA'
                and placements[0]['branch_id']==branch_id and placements[0]['area_id']==area_id
                and placements[0]['supervisor_id']==supervisor_id):
                raise ValueError('Hay una adscripción superpuesta o distinta. Ajusta la vigencia o revisa la adscripción en Equipo.')
        else:
            cx.execute('''INSERT INTO employee_placements(user_id,company_id,branch_id,area_id,supervisor_id,
                valid_from,valid_to,classification_status,changed_by,created_at)
                VALUES(?,?,?,?,?,?,?,'CLASIFICADA',?,?)''',
                (user_id,branch['company_id'],branch_id,area_id,supervisor_id,first,last,actor['id'],nowstr()))
        if mode=='new':
            organization.register_new_person(cx,user_id,link_from,actor['id'])
        for table, start, finish in [('attendance_employment_periods',employment_from.isoformat(),employment_to.isoformat() if employment_to else None),
                                     ('attendance_regime_assignments',first,last)]:
            periods=cx.execute(f'''SELECT * FROM {table} WHERE user_id=?
                AND valid_from<=COALESCE(?,'9999-12-31') AND COALESCE(valid_to,'9999-12-31')>=?''',
                (user_id,finish,start)).fetchall()
            if periods:
                exact=len(periods)==1 and periods[0]['valid_from']==start and periods[0]['valid_to']==finish and periods[0]['confirmed']
                if table=='attendance_regime_assignments':
                    exact=exact and periods[0]['regime_code']==regime and periods[0]['submodality']==sub and json.loads(periods[0]['weekdays_json'])==weekdays
                if not exact:
                    raise ValueError('Existen vigencias laborales o de régimen diferentes. Revísalas antes de confirmar.')
            elif table=='attendance_employment_periods':
                cx.execute('''INSERT INTO attendance_employment_periods(user_id,valid_from,valid_to,confirmed,note,assigned_by,created_at)
                    VALUES(?,?,?,1,?,?,?)''',(user_id,start,finish,reason,actor['id'],nowstr()))
            else:
                cx.execute('''INSERT INTO attendance_regime_assignments(user_id,regime_code,submodality,weekdays_json,
                    valid_from,valid_to,confirmed,note,assigned_by,created_at) VALUES(?,?,?,?,?,?,1,?,?,?)''',
                    (user_id,regime,sub,json.dumps(weekdays),start,finish,reason,actor['id'],nowstr()))
        if not overlaps:
            cx.execute('''INSERT INTO biometric_identity_links(person_id,user_id,valid_from,valid_to,confirmed_by,confirmed_at,note)
                VALUES(?,?,?,?,?,?,?)''',(person_id,user_id,first,last,actor['id'],nowstr(),reason))
        cx.execute('''UPDATE biometric_persons SET linked_user_id=?,link_status='CONFIRMED',reviewed_by=?,
            reviewed_at=?,updated_at=? WHERE person_id=?''',(user_id,actor['id'],nowstr(),nowstr(),person_id))
    cx.execute('''INSERT INTO biometric_roster_reviews(person_id,classification,reason,reviewed_by,reviewed_at,user_id)
        VALUES(?,?,?,?,?,?)''',(person_id,classification,reason,actor['id'],nowstr(),user_id))
    audit(cx,actor['id'],'PADRON_BIOMETRICO','identidad',person_id,
          f'{classification}; usuario={user_id}; respaldo={reason}')
    from attendance_continuous import capture_changes
    from reconciliation import drain_recalculations
    capture_changes(cx)
    result=drain_recalculations(cx)
    audit(cx,actor['id'],'RECONCILIACION','identidad',person_id,
          'Vigencias confirmadas; jornadas afectadas='+str(len(result['affected'])))
    return user_id
