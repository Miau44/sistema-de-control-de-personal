"""Dated organizational authority and atomic civil-date changes.

Access roles are not labor authority. Local provisional assignments remain provisional.
Historical request plans and decisions never consult today's hierarchy to rewrite yesterday.
"""
import json
from contextlib import contextmanager
from datetime import date, timedelta
import uuid

VERSION = 'organization-v16'


@contextmanager
def atomic(cx):
    name = 'org_' + uuid.uuid4().hex
    cx.execute('SAVEPOINT '+name)
    try:
        yield
        cx.execute('RELEASE '+name)
    except BaseException:
        cx.execute('ROLLBACK TO '+name)
        cx.execute('RELEASE '+name)
        raise


def _stamp():
    from core import nowstr
    return nowstr()


def placement(cx, user_id, day):
    return cx.execute('''SELECT * FROM employee_placements WHERE user_id=? AND valid_from<=?
        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',
        (user_id,str(day)[:10],str(day)[:10])).fetchone()


def authority(cx, user_id, day):
    row=cx.execute('''SELECT * FROM organization_authority_history WHERE user_id=? AND valid_from<=?
        AND (valid_to IS NULL OR valid_to>=?) ORDER BY valid_from DESC,id DESC LIMIT 1''',
        (user_id,str(day)[:10],str(day)[:10])).fetchone()
    return dict(row) if row else None


def supervisor(cx, user_id, day, *, require_active=True):
    user=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if not user: raise ValueError('Persona inexistente.')
    p=placement(cx,user_id,day); a=authority(cx,user_id,day)
    role=a['functional_role'] if a else user['role']
    superior_id=p['supervisor_id'] if role=='employee' and p else (a['superior_id'] if a else None)
    source={'placement':dict(p) if p else None,'authority':a,'version':VERSION}
    if not superior_id or superior_id==user_id:
        raise ValueError('No existe un superior operativo válido; requiere definición de Recursos Humanos.')
    superior=cx.execute('SELECT * FROM users WHERE id=?',(superior_id,)).fetchone()
    if not superior or (require_active and not superior['active']):
        raise ValueError('El superior no existe o está inactivo.')
    if role=='employee':
        responsible=cx.execute('''SELECT * FROM area_responsibles WHERE area_id=? AND user_id=?
            AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?) ORDER BY id DESC LIMIT 1''',
            (p['area_id'],superior_id,str(day)[:10],str(day)[:10])).fetchone()
        if superior['role']!='manager' or not responsible:
            raise ValueError('El trabajador requiere un encargado vigente de su área.')
        source['area_responsible']=dict(responsible)
    elif role=='manager' and (superior['role']!='hr' or superior['id'] not in hr_candidates(cx,day)):
        raise ValueError('Un encargado debe depender de Recursos Humanos.')
    elif role=='admin':
        raise ValueError('La administración técnica no tiene superior laboral implícito.')
    elif role=='hr':
        expected={'OPERADOR_RRHH':'RESPONSABLE_RRHH','RESPONSABLE_RRHH':'ADMINISTRACION_OPERATIVA'}
        required=expected.get(a['authority'] if a else '')
        superior_authority=authority(cx,superior_id,day)
        if not required or not superior_authority or superior_authority['authority']!=required:
            raise ValueError('La superioridad de esta cuenta requiere asignación organizacional expresa.')
    return superior,source


def hr_candidates(cx, day):
    # Accounts without local metadata retain legacy HR access. Administration never enters a generic HR pool.
    legacy=not cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone()
    return [r['id'] for r in cx.execute('SELECT id FROM users WHERE active=1 AND role=\'hr\'')
            if ((a:=authority(cx,r['id'],day)) and a['functional_role']=='hr' and a['authority'] in ('OPERADOR_RRHH','RESPONSABLE_RRHH')) or (not a and legacy)]


def choose(cx, person, day):
    if person['role']=='manager' and not authority(cx,person['id'],day):
        if cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone():
            raise ValueError('Falta la asignación de superior operativo; solicita revisión de Recursos Humanos.')
        candidates=hr_candidates(cx,day)
        if len(candidates)!=1: raise ValueError('Configura un superior de Recursos Humanos para este encargado.')
        return cx.execute('SELECT * FROM users WHERE id=?',(candidates[0],)).fetchone()
    return supervisor(cx,person['id'],day)[0]


def manager_scope(cx, manager_id, user_id, day):
    if manager_id==user_id: return False
    p=placement(cx,user_id,day)
    if not p: return False
    # A manager's area responsibility does not make other managers or HR their dependents.
    u=cx.execute('SELECT role FROM users WHERE id=?',(user_id,)).fetchone()
    a=authority(cx,user_id,day)
    if not u or (a['functional_role'] if a else u['role'])!='employee': return False
    return p['supervisor_id']==manager_id or bool(cx.execute('''SELECT 1 FROM area_responsibles
        WHERE user_id=? AND area_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
        (manager_id,p['area_id'],str(day)[:10],str(day)[:10])).fetchone())


def _revision(cx, entity, entity_id, day, before, after, actor_id):
    cx.execute('''INSERT INTO organization_revisions(entity,entity_id,effective_date,before_json,after_json,actor_id,recorded_at)
        VALUES(?,?,?,?,?,?,?)''',(entity,entity_id,str(day),json.dumps(before,ensure_ascii=False),
        json.dumps(after,ensure_ascii=False),actor_id,_stamp()))


def _dated_change(cx, table, key, values, day, actor_id):
    # Table/column names are internal constants, never request input.
    day=date.fromisoformat(str(day)); where=' AND '.join(k+'=?' for k in key)
    current=cx.execute(f'SELECT * FROM {table} WHERE {where} AND valid_to IS NULL',tuple(key.values())).fetchone()
    if current and day<date.fromisoformat(current['valid_from']):
        raise ValueError('La fecha efectiva precede a la asignación vigente.')
    before=dict(current) if current else None
    if current and current['valid_from']==str(day):
        cx.execute(f'UPDATE {table} SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),current['id']))
        row_id=current['id']
    else:
        if current: cx.execute(f'UPDATE {table} SET valid_to=? WHERE id=?',(str(day-timedelta(days=1)),current['id']))
        data={**key,**values,'valid_from':str(day),'created_at':_stamp()}
        row_id=cx.execute(f'INSERT INTO {table}('+','.join(data)+') VALUES('+','.join('?' for _ in data)+')',tuple(data.values())).lastrowid
    after=dict(cx.execute(f'SELECT * FROM {table} WHERE id=?',(row_id,)).fetchone())
    _revision(cx,table,row_id,day,before,after,actor_id)
    return row_id


def validate_superior(cx, user_id, superior_id, day, role=None, area_id=None):
    user=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    if not user: raise ValueError('Persona inexistente.')
    role=role or user['role']
    if superior_id==user_id: raise ValueError('No se permite autosupervisión.')
    if superior_id is None:
        if role=='employee': raise ValueError('El trabajador requiere encargado.')
        return
    parent=cx.execute('SELECT * FROM users WHERE id=? AND active=1',(superior_id,)).fetchone()
    if not parent or parent['role']=='admin': raise ValueError('Superior inexistente, inactivo o sin autoridad laboral.')
    seen={user_id}; cursor=superior_id
    while cursor:
        if cursor in seen: raise ValueError('La asignación genera un ciclo jerárquico.')
        seen.add(cursor)
        person=cx.execute('SELECT role FROM users WHERE id=?',(cursor,)).fetchone()
        p=placement(cx,cursor,day); a=authority(cx,cursor,day)
        cursor=p['supervisor_id'] if person and person['role']=='employee' and p else (a['superior_id'] if a else None)
    if role=='employee':
        p=placement(cx,user_id,day); area_id=area_id or (p['area_id'] if p else None)
        if parent['role']!='manager' or not cx.execute('''SELECT 1 FROM area_responsibles
            WHERE area_id=? AND user_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
            (area_id,superior_id,str(day),str(day))).fetchone(): raise ValueError('Encargado no vigente en el área.')
    elif role=='manager':
        if parent['role']!='hr' or superior_id not in hr_candidates(cx,day):
            raise ValueError('El encargado debe depender de Recursos Humanos.')
    elif role=='hr':
        a=authority(cx,user_id,day); pa=authority(cx,superior_id,day)
        expected={'OPERADOR_RRHH':'RESPONSABLE_RRHH','RESPONSABLE_RRHH':'ADMINISTRACION_OPERATIVA'}
        if not a or not pa or expected.get(a['authority'])!=pa['authority']:
            raise ValueError('Superior organizacional no autorizado para esta cuenta.')
    else: raise ValueError('La cuenta técnica no ejerce autoridad laboral.')


def set_superior(cx,user_id,superior_id,day,actor_id):
    with atomic(cx):
        from core import blocked_range
        day=date.fromisoformat(str(day))
        today=date.fromisoformat(_stamp()[:10])
        if day>today or blocked_range(cx,day,today):
            raise ValueError('La fecha afecta un período cerrado o requiere programación futura.')
        validate_superior(cx,user_id,superior_id,day)
        person=cx.execute('SELECT role FROM users WHERE id=?',(user_id,)).fetchone()
        if person['role']=='employee':
            p=placement(cx,user_id,day)
            change_placement(cx,user_id,p['company_id'],p['branch_id'],p['area_id'],superior_id,day,actor_id,p['classification_status'])
            return
        a=authority(cx,user_id,day)
        if not a: raise ValueError('Configura primero la función organizacional de la cuenta.')
        _dated_change(cx,'organization_authority_history',{'user_id':user_id},
            {'superior_id':superior_id,'authority':a['authority'],'functional_role':a['functional_role'],'provisional':a['provisional'],
             'source_json':a['source_json'],'changed_by':actor_id},day,actor_id)
        cx.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(superior_id,user_id))


def change_placement(cx,user_id,company_id,branch_id,area_id,supervisor_id,day,actor_id,classification='CLASIFICADA',role=None):
    from core import blocked_range
    with atomic(cx):
        day=date.fromisoformat(str(day))
        if blocked_range(cx,day,date.fromisoformat(_stamp()[:10])): raise ValueError('El cambio alcanza un período cerrado.')
        if day>date.fromisoformat(_stamp()[:10]): raise ValueError('Los cambios futuros requieren programación explícita; no se aplicaron.')
        area=cx.execute('SELECT * FROM areas WHERE id=? AND active=1',(area_id,)).fetchone()
        branch=cx.execute('SELECT * FROM branches WHERE id=? AND company_id=? AND active=1',(branch_id,company_id)).fetchone()
        if not area or not branch or area['branch_id']!=branch_id: raise ValueError('Área y sucursal incompatibles.')
        person=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone(); role=role or person['role']
        if role!='manager' and cx.execute('SELECT 1 FROM area_responsibles WHERE user_id=? AND valid_to IS NULL',(user_id,)).fetchone():
            raise ValueError('Sustituye las responsabilidades de área antes de cambiar este rol.')
        validate_superior(cx,user_id,supervisor_id,day,role,area_id)
        if role!='employee' and supervisor_id is not None: raise ValueError('Usa la superioridad operativa para encargados y RR. HH.')
        if role!='employee':
            # Preserve a valid operational chain. Promotion needs an unambiguous HR superior.
            a=authority(cx,user_id,day)
            superior=a['superior_id'] if a else None
            if role=='manager' and (not superior or cx.execute('SELECT role FROM users WHERE id=?',(superior,)).fetchone()['role']!='hr'):
                choices=[r['id'] for r in cx.execute("SELECT id FROM users WHERE employee_code='OP_RRHH' AND active=1 AND role='hr'")]
                if len(choices)==1: superior=choices[0]
                elif a: superior=None
            if superior: validate_superior(cx,user_id,superior,day,role,area_id)
            if a and superior!=a['superior_id']:
                _dated_change(cx,'organization_authority_history',{'user_id':user_id},
                    {'superior_id':superior,'authority':a['authority'],'functional_role':role,'provisional':a['provisional'],
                     'source_json':a['source_json'],'changed_by':actor_id},day,actor_id)
                cx.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(superior,user_id))
        row_id=_dated_change(cx,'employee_placements',{'user_id':user_id},
            {'company_id':company_id,'branch_id':branch_id,'area_id':area_id,'supervisor_id':supervisor_id,
             'classification_status':classification,'changed_by':actor_id},day,actor_id)
        if role=='employee' and authority(cx,user_id,day):
            a=authority(cx,user_id,day)
            _dated_change(cx,'organization_authority_history',{'user_id':user_id},
                {'superior_id':supervisor_id,'authority':a['authority'],'functional_role':role,'provisional':a['provisional'],
                 'source_json':a['source_json'],'changed_by':actor_id},day,actor_id)
            cx.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(supervisor_id,user_id))
        cx.execute('UPDATE users SET area_id=? WHERE id=?',(area_id,user_id))
        return row_id


def record_role(cx,user_id,role,day,actor_id):
    """Reject a demotion/deactivation that strands an existing assignment; never repair it silently."""
    a=authority(cx,user_id,day)
    if a and a['functional_role']!=role:
        from core import blocked_range
        day=date.fromisoformat(str(day));today=date.fromisoformat(_stamp()[:10])
        if day>today or blocked_range(cx,day,today):
            raise ValueError('El cambio de rol requiere una fecha abierta y no futura.')
        if role!='manager' and cx.execute('SELECT 1 FROM area_responsibles WHERE user_id=? AND valid_to IS NULL',(user_id,)).fetchone():
            raise ValueError('Sustituye primero las responsabilidades de área antes de cambiar este rol.')
        p=placement(cx,user_id,day)
        superior=p['supervisor_id'] if role=='employee' and p else a['superior_id']
        if role=='manager' and a['functional_role']=='employee':
            choices=cx.execute("SELECT id FROM users WHERE employee_code='OP_RRHH' AND role='hr' AND active=1").fetchall()
            superior=choices[0]['id'] if len(choices)==1 else None
        validate_superior(cx,user_id,superior,day,role,p['area_id'] if p else None)
        _dated_change(cx,'organization_authority_history',{'user_id':user_id},
            {'superior_id':superior,'authority':a['authority'],'functional_role':role,'provisional':a['provisional'],
             'source_json':a['source_json'],'changed_by':actor_id},day,actor_id)
        cx.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(superior,user_id))


def register_profile(cx,user_id,day,actor_id):
    """Used for explicit preparation/registration only, never to confirm administrative facts."""
    u=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    local=cx.execute('SELECT * FROM local_staff_profiles WHERE user_id=?',(user_id,)).fetchone()
    if not local: return
    if not cx.execute('SELECT 1 FROM organization_authority_history WHERE user_id=?',(user_id,)).fetchone():
        _dated_change(cx,'organization_authority_history',{'user_id':user_id},
            {'superior_id':local['superior_id'],'authority':local['operational_authority'],'functional_role':u['role'],
             'provisional':local['provisional'],'source_json':local['source_json'],'changed_by':actor_id},day,actor_id)


def register_new_person(cx,user_id,day,actor_id):
    if not cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone(): return
    u=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    p=placement(cx,user_id,day)
    superior=p['supervisor_id'] if u['role']=='employee' and p else None
    if u['role']=='manager':
        rows=cx.execute("SELECT id FROM users WHERE employee_code='OP_RRHH' AND role='hr' AND active=1").fetchall()
        superior=rows[0]['id'] if len(rows)==1 else None
    cx.execute('''INSERT INTO local_staff_profiles(user_id,account_kind,operational_authority,superior_id,source_json,provisional)
        VALUES(?,'LABORAL','',?,?,1)''',(user_id,superior,json.dumps({'source':'ALTA_MANUAL','actor_id':actor_id,
        'authority_confirmation':'PENDIENTE','employment_dates':'PENDIENTES'})))
    register_profile(cx,user_id,day,actor_id)


def assign_responsibles(cx,area_id,principal_id,substitute_id,day,actor_id):
    from core import blocked_range
    with atomic(cx):
        day=date.fromisoformat(str(day))
        today=date.fromisoformat(_stamp()[:10])
        if day>today or blocked_range(cx,day,today): raise ValueError('Fecha futura o cambio que alcanza un período cerrado.')
        if principal_id==substitute_id: raise ValueError('Principal y suplente deben ser diferentes.')
        area=cx.execute('SELECT * FROM areas WHERE id=? AND active=1',(area_id,)).fetchone()
        if not area or not area['branch_id']: raise ValueError('Área sin sucursal vigente.')
        old_ids={r[0] for r in cx.execute('SELECT user_id FROM area_responsibles WHERE area_id=? AND valid_to IS NULL',(area_id,))}
        for kind,uid in [('PRINCIPAL',principal_id),('SUPLENTE',substitute_id)]:
            key={'area_id':area_id,'role':kind}
            current=cx.execute('SELECT * FROM area_responsibles WHERE area_id=? AND role=? AND valid_to IS NULL',(area_id,kind)).fetchone()
            if uid:
                u=cx.execute("SELECT * FROM users WHERE id=? AND role='manager' AND active=1",(uid,)).fetchone()
                p=placement(cx,uid,day)
                if not u or (p and p['branch_id'] is not None and p['branch_id']!=area['branch_id']):
                    raise ValueError('El responsable debe ser un encargado activo de la sucursal.')
                _dated_change(cx,'area_responsibles',key,{'user_id':uid,'assigned_by':actor_id},day,actor_id)
            elif current:
                if str(day)<current['valid_from']: raise ValueError('Fecha anterior a la asignación vigente.')
                if current['valid_from']==str(day):
                    # Same-day removal has no effective interval; its prior content survives in revisions.
                    _revision(cx,'area_responsibles',current['id'],day,dict(current),None,actor_id)
                    cx.execute('DELETE FROM area_responsibles WHERE id=?',(current['id'],))
                else:
                    cx.execute('UPDATE area_responsibles SET valid_to=? WHERE id=?',(str(day-timedelta(days=1)),current['id']))
                    _revision(cx,'area_responsibles',current['id'],day,dict(current),{'valid_to':str(day-timedelta(days=1))},actor_id)
        rows=cx.execute('''SELECT p.* FROM employee_placements p JOIN users u ON u.id=p.user_id
            WHERE p.area_id=? AND p.valid_to IS NULL AND u.role='employee' ''',(area_id,)).fetchall()
        for p in rows:
            # Preserve explicitly assigned independent superiors; replace those attached to removed responsible(s).
            if p['supervisor_id'] not in old_ids and p['supervisor_id'] is not None: continue
            if p['supervisor_id'] and cx.execute('''SELECT 1 FROM area_responsibles WHERE area_id=? AND user_id=?
                AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',(area_id,p['supervisor_id'],str(day),str(day))).fetchone(): continue
            change_placement(cx,p['user_id'],p['company_id'],p['branch_id'],area_id,principal_id,day,actor_id,p['classification_status'])
