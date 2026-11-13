"""Acceptance checks use fresh preparation and disposable SQLite copies only."""
from contextlib import closing
from collections import Counter
from pathlib import Path
import json
import sqlite3
import pytest
import core
from local_setup import build_database,read_sources


@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    source=Path.home()/'Downloads';files=(source/'Biometrico(20261001-20261031).xlsx',
        source/'reporte (35)'/'01_Reporte_Detallado.xlsx',source/'reporte (35)'/'02_Reporte_Resumido.xlsx')
    if not all(p.is_file() for p in files):pytest.skip('Archivos reales de octubre no disponibles en este equipo.')
    target=tmp_path_factory.mktemp('local-setup')/'prepared.sqlite3'
    result=build_database(target,*files)
    return target,result,files


@pytest.fixture
def local_copy(prepared,tmp_path,monkeypatch):
    source,_,_=prepared;target=tmp_path/'isolated.sqlite3'
    with closing(sqlite3.connect(source)) as s,closing(sqlite3.connect(target)) as d:s.backup(d)
    monkeypatch.setattr(core,'DB_PATH',target)
    return target


def test_fifty_workers_separate_auxiliary_accounts_and_unknown_hours(prepared,local_copy):
    _,stats,_=prepared
    assert (stats['workers'],stats['identities'],stats['events'],stats['groups'],stats['repeated_groups'],stats['extra_grouped_events'])==(50,50,3567,3049,483,518)
    assert stats['provisional_managers']==15 and stats['labor_areas']==18
    with core.db() as c:
        assert c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==54
        assert c.execute("SELECT COUNT(*) FROM local_staff_profiles WHERE account_kind='LABORAL'").fetchone()[0]==50
        assert c.execute('SELECT COUNT(*) FROM biometric_identity_links').fetchone()[0]==50
        assert c.execute("SELECT COUNT(*) FROM users WHERE email NOT LIKE '%.invalid' OR start_time<>'' OR end_time<>''").fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM monthly_closures').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM attendance_employment_periods').fetchone()[0]==0
        assert c.execute('SELECT COUNT(*) FROM hr_roster_proposals').fetchone()[0]==51
        assert c.execute("SELECT COUNT(*) FROM hr_roster_proposals WHERE biometric_person_id IS NULL").fetchone()[0]==1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'


def test_hierarchy_no_self_cycles_cross_branch_or_invalid_periods(local_copy):
    with core.db() as c:
        assert not c.execute('SELECT 1 FROM employee_placements WHERE supervisor_id=user_id OR valid_to<valid_from').fetchone()
        assert not c.execute('''SELECT 1 FROM employee_placements p JOIN employee_placements s ON s.user_id=p.supervisor_id
            WHERE p.branch_id<>s.branch_id AND p.valid_to IS NULL AND s.valid_to IS NULL''').fetchone()
        parents={r['user_id']:r['superior_id'] for r in c.execute('SELECT * FROM local_staff_profiles')}
        for uid in parents:
            seen=set();current=uid
            while current:
                assert current not in seen;seen.add(current);current=parents.get(current)
        managers=[r['employee_code'] for r in c.execute("SELECT employee_code FROM users WHERE role='manager' ORDER BY employee_code")]
        assert len(managers)==15


def test_sources_manual_mobile_and_repeat_import_remain_intact(prepared,local_copy):
    from reconciliation import preview_zlink,import_zlink
    from attendance_continuous import process_import
    _,_,files=prepared
    with core.db() as c:
        raw=files[0].read_bytes();p=preview_zlink(c,raw)
        assert Counter(r['fingerprint'] for r in p['rows'])==Counter(r[0] for r in c.execute('SELECT row_fingerprint FROM biometric_events'))
        actor=c.execute("SELECT id FROM users WHERE employee_code='OP_RRHH'").fetchone()[0]
        versions=c.execute('SELECT COUNT(*) FROM attendance_daily_versions').fetchone()[0]
        result=process_import(c,p,import_zlink(c,p,raw,actor,files[0].name),actor)
        assert result['added']==0 and result['duplicate']==3567 and result['recalculated_days']==0
        assert c.execute('SELECT COUNT(*) FROM attendance_daily_versions').fetchone()[0]==versions
        assert c.execute("SELECT COUNT(*),COUNT(DISTINCT substr(occurred_at,1,10)) FROM biometric_events WHERE person_id='8282765'").fetchone()[:]==(421,27)
        assert c.execute("SELECT COUNT(*) FROM attendance_reconciliation_days WHERE operational_result IN ('POSIBLE INASISTENCIA','FALTA CONFIRMADA POR RR. HH.')").fetchone()[0]==0


def test_normal_roles_request_decisions_pdf_and_no_impersonation(local_copy):
    from app import app,approved_permit
    from fastapi.testclient import TestClient
    from bs4 import BeautifulSoup
    from local_credentials import set_password
    import pymupdf
    secret='IsolatedFixturePassword42!'
    with core.db() as c:
        worker=c.execute("SELECT u.* FROM users u JOIN local_staff_profiles p ON p.user_id=u.id WHERE u.role='employee' AND p.account_kind='LABORAL' ORDER BY u.id LIMIT 1").fetchone()
        mid=c.execute('SELECT supervisor_id FROM employee_placements WHERE user_id=?',(worker['id'],)).fetchone()[0]
        manager=c.execute('SELECT * FROM users WHERE id=?',(mid,)).fetchone()
        hr=c.execute("SELECT * FROM users WHERE employee_code='OP_RRHH'").fetchone()
        admin=c.execute("SELECT * FROM users WHERE employee_code='ADMIN_TEC'").fetchone()
        other=c.execute("SELECT * FROM users WHERE role='manager' AND id<>? ORDER BY id LIMIT 1",(mid,)).fetchone()
        for person in (worker,manager,hr,admin,other):set_password(c,person['employee_code'],secret)
    def csrf(client,path='/solicitar'):
        r=client.get(path);assert r.status_code==200
        return BeautifulSoup(r.text,'html.parser').select_one('[name=csrf]')['value']
    def login(person):
        client=TestClient(app);token=csrf(client,'/login')
        r=client.post('/login',data={'csrf':token,'email':person['email'],'password':secret})
        assert r.status_code==200 and r.url.path!='/login'
        assert 'executive-demo-banner' not in r.text
        return client
    sessions=[login(p) for p in (worker,manager,hr,admin,other)];w,m,h,a,o=sessions
    try:
        for route in ['/demo-jefatura','/demo-jefatura/incidencias']:
            assert h.get(route).status_code==404
        assert h.post('/demo-jefatura/acceso/1',data={}).status_code==404
        for client in sessions:
            for page in ['/','/asistencia','/mis-solicitudes','/mi-perfil']:
                assert client.get(page).status_code==200
        payload={'csrf':csrf(w),'kind':'PERMISO','leave_type':'PERSONAL','starts_at':'2026-11-17T09:00',
            'ends_at':'2026-11-17T10:00','pay_type':'CON_GOCE','justification':'Solicitud aislada para verificar autorización.'}
        w.post('/solicitar',data=payload)
        with core.db() as c:rid=c.execute('SELECT id FROM requests ORDER BY id DESC LIMIT 1').fetchone()[0]
        assert w.post(f'/aprobaciones/{rid}/resolver',data={'csrf':csrf(w),'decision':'APROBADA'}).status_code==403
        assert o.post(f'/aprobaciones/{rid}/resolver',data={'csrf':csrf(o),'decision':'APROBADA'}).status_code==403
        assert m.post(f'/aprobaciones/{rid}/resolver',data={'csrf':csrf(m),'decision':'APROBADA'}).status_code==200
        assert h.post(f'/aprobaciones/{rid}/resolver',data={'csrf':csrf(h),'decision':'APROBADA'}).status_code==200
        with core.db() as c:
            r=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
            assert approved_permit(c,r)
            assert c.execute("SELECT COUNT(*) FROM request_approval_steps WHERE request_id=? AND decision='APROBADA'",(rid,)).fetchone()[0]==2
        pdf=w.get(f'/boleta/{rid}.pdf');assert pdf.status_code==200
        with pymupdf.open(stream=pdf.content,filetype='pdf') as doc:
            assert tuple(doc[0].rect)==(0,0,612,792)
            assert 'DEMO' not in doc[0].get_text() and 'SIN VALIDEZ' not in doc[0].get_text()
        assert o.get(f'/boleta/{rid}.pdf').status_code==403
        assert a.get('/equipo').status_code==403
        w.post('/logout',data={'csrf':csrf(w)});assert w.get('/asistencia').status_code==401
        w.close();sessions[0]=login(worker);w=sessions[0]
        payload['csrf']=csrf(w);w.post('/solicitar',data=payload)
        with core.db() as c:rejected=c.execute('SELECT MAX(id) FROM requests').fetchone()[0]
        assert m.post(f'/aprobaciones/{rejected}/resolver',data={'csrf':csrf(m),'decision':'RECHAZADA','note':'Rechazo aislado con respaldo de prueba.'}).status_code==200
        assert w.get(f'/boleta/{rejected}.pdf').status_code==400
        with core.db() as c:
            from attendance_reporting import report_from_reconciliation
            from datetime import date
            report=report_from_reconciliation(c,date(2026,11,25))
            assert report['empleados']==50
            assert not any(x['codigo'] in ('OP_RRHH','RESP_RRHH','ADM_OPER','ADMIN_TEC') for x in report['resumen'])
            future=core.report(c,date(2027,1,25))
            assert future['empleados']==50 and not future['consolidation']['ready']
    finally:
        for session in sessions:session.close()


def test_repeatable_manager_selection_and_destination_guard(prepared,tmp_path,monkeypatch):
    from local_setup import main
    source,stats,files=prepared
    second=tmp_path/'second.sqlite3';result=build_database(second,*files)
    assert result['manager_codes']==stats['manager_codes']
    with pytest.raises(ValueError):build_database(second,*files)
    monkeypatch.setattr('sys.argv',['local_setup','--biometric',str(files[0]),'--detailed',str(files[1]),'--summary',str(files[2]),'--target',str(source),'--apply'])
    with pytest.raises(SystemExit):main()
