"""Pruebas básicas: roles, permisos, cierre, cálculo e importación."""
import io
import os
import re
import sys
from datetime import date,datetime,timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import core
from legacy_fixture import seed_legacy_fixture
import migrations
from app import app,parse_event,identify_headers,rows_from_file,resolve_biometric_user
from app import period_label,visible_date,visible_datetime
from documents import pdf_boletas


@pytest.fixture
def demo(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DB_PATH',tmp_path/'test.sqlite3')
    seed_legacy_fixture()
    return core.DB_PATH


def sign_in(client,email):
    r=client.get('/login')
    token=BeautifulSoup(r.text,'html.parser').select_one('input[name="csrf"]')['value']
    r=client.post('/login',data={'csrf':token,'email':email,'password':'Demo2026!#'},follow_redirects=True)
    assert r.status_code==200
    return get_csrf(client,'/mi-perfil')


def get_csrf(client,page):
    token=BeautifulSoup(client.get(page).text,'html.parser').select_one('input[name="csrf"]')
    if token is None: token=BeautifulSoup(client.get('/mi-perfil').text,'html.parser').select_one('input[name="csrf"]')
    return token['value']


def test_periods_26_to_25_and_cross_year(demo):
    assert core.month_end_for_day(date(2026,10,26))==date(2026,11,25)
    assert core.period_start(date(2027,1,25))==date(2026,12,26)
    assert core.month_end_for_day(date(2026,12,26))==date(2027,1,25)
    assert period_label('2026-10-26','2026-11-25')=='OCTUBRE → NOVIEMBRE'
    assert period_label('2026-12-26','2027-01-25')=='DICIEMBRE 2026 → ENERO 2027'


def test_localized_date_helpers():
    assert visible_date('2026-11-10')=='10/11/2026'
    assert visible_datetime('2026-11-10 14:35:12')=='10/11/2026 14:35'


def test_dashboard_shows_calendar_yesterday_when_yesterday_is_sunday(demo,monkeypatch):
    fixed=datetime(2026,11,9,10,0,tzinfo=core.TZ)
    monkeypatch.setattr(core,'now',lambda:fixed)
    monkeypatch.setattr('app.now',lambda:fixed)
    with TestClient(app) as c:
        sign_in(c,'ana@demo.local')
        response=c.get('/')
        assert response.status_code==200
        assert 'Mi jornada de ayer' in response.text
        assert 'Marcaciones de ayer' in response.text
        assert '08/11/2026' in response.text
        assert 'No laboral' in response.text

def test_role_and_workflow_pdf(demo):
    with TestClient(app) as c:
        token=sign_in(c,'ana@demo.local')
        # Employee cannot approve other employees or see reports
        assert c.get('/reportes').status_code==403
        assert c.get('/aprobaciones').status_code==403
        token=get_csrf(c,'/solicitar')
        r=c.post('/solicitar',data={
         'csrf':token,'kind':'PERMISO','leave_type':'MEDICO',
         'starts_at':'2026-11-04T09:00','ends_at':'2026-11-04T17:00',
         'place':'Hospital','pay_type':'CON_GOCE','compensation':'No mostrar',
         'justification':'Atención en centro médico autorizada con respaldo.'},follow_redirects=True)
        assert 'Solicitud' in r.text and 'enviada' in r.text
        with core.db() as db:
            request=db.execute('SELECT * FROM requests ORDER BY id DESC LIMIT 1').fetchone()
            rid=request['id']
            assert request['status']=='PENDIENTE'
            assert request['compensation']==''
            assert db.execute('SELECT COUNT(*) FROM punches').fetchone()[0]==6
            assert db.execute('SELECT COUNT(*) FROM request_approval_steps WHERE request_id=?',(rid,)).fetchone()[0]==2
            assert db.execute('''SELECT COUNT(*) FROM internal_notifications
                                 WHERE user_id=? AND entity_id=?''',(request['user_id'],str(rid))).fetchone()[0]==0
            assert db.execute('''SELECT COUNT(*) FROM internal_notifications
                                 WHERE user_id=? AND entity_id=?''',(request['approver_id'],str(rid))).fetchone()[0]==1
        assert c.get(f'/boleta/{rid}.pdf').status_code==400
    with TestClient(app) as c:
        sign_in(c,'rrhh@demo.local')
        token=get_csrf(c,'/solicitar')
        assert c.post(f'/aprobaciones/{rid}/resolver',
          data={'csrf':token,'decision':'APROBADA','note':'Intento fuera de orden'},follow_redirects=False).status_code==403
    with TestClient(app) as c:
        sign_in(c,'encargado@demo.local')
        token=get_csrf(c,'/aprobaciones')
        r=c.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'Se autoriza con respaldo'},follow_redirects=True)
        assert 'pasa a' in r.text.lower()
        with core.db() as db:
            assert db.execute('SELECT status FROM requests WHERE id=?',(rid,)).fetchone()['status']=='PENDIENTE'
        assert c.get(f'/boleta/{rid}.pdf').status_code==400
    with TestClient(app) as c:
        sign_in(c,'rrhh@demo.local')
        token=get_csrf(c,'/aprobaciones')
        r=c.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'Aprobado por Recursos Humanos'},follow_redirects=True)
        assert 'definitivamente' in r.text.lower()
        r=c.get(f'/boleta/{rid}.pdf')
        assert r.status_code==200 and r.content.startswith(b'%PDF-')
        assert re.search(rb'/MediaBox\s*\[\s*0\s+0\s+612\s+792\s*\]',r.content)
        assert b'Compensaci' not in r.content
        with core.db() as db:
            anna=db.execute('''SELECT u.*,a.name area_name FROM users u JOIN areas a ON u.area_id=a.id
                                 WHERE employee_code='EMP001' ''').fetchone()
            # record at Nov 6 may be absent; approved 09-17 is partial, not fully justified 08-17
            before=core.daily_status(db,anna,date(2026,11,4))
            assert before['estado']=='JUSTIFICADO_PARCIAL'
    with TestClient(app) as c:
        sign_in(c,'encargado@demo.local')
        assert c.get(f'/boleta/{rid}.pdf').status_code==200
        history=c.get('/historial-decisiones')
        assert history.status_code==200 and 'Aprobadas por mí' in history.text
    with TestClient(app) as c:
        sign_in(c,'carlos@demo.local')
        assert c.get(f'/boleta/{rid}.pdf').status_code==403


def test_histories_keep_manager_approval_when_hr_rejects_and_hr_can_review_all(demo):
    with TestClient(app) as employee:
        token=sign_in(employee,'ana@demo.local')
        response=employee.post('/solicitar',data={'csrf':token,'kind':'PERMISO','leave_type':'MEDICO',
          'starts_at':'2026-11-16T09:00','ends_at':'2026-11-16T12:00','place':'Hospital',
          'pay_type':'CON_GOCE','justification':'Consulta médica con documentación de respaldo.'},follow_redirects=False)
        assert response.status_code==303
        with core.db() as db:rid=db.execute('SELECT MAX(id) FROM requests').fetchone()[0]
        assert 'RECHAZADA' not in employee.get('/mis-solicitudes').text
    with TestClient(app) as manager:
        token=sign_in(manager,'encargado@demo.local')
        manager.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'Aprobado por encargado'},follow_redirects=False)
        assert manager.get('/historial-decisiones').status_code==200
    with TestClient(app) as hr:
        token=sign_in(hr,'rrhh@demo.local')
        response=hr.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'RECHAZADA','note':'Falta documentación obligatoria'},follow_redirects=False)
        assert response.status_code==303
        history=hr.get('/historial-solicitudes?status=RECHAZADA')
        assert history.status_code==200 and 'Falta documentación' in history.text
        detail=hr.get(f'/historial-solicitudes/{rid}')
        assert detail.status_code==200 and 'RECHAZADA POR RR. HH.' in detail.text
        assert hr.get('/boletas').status_code==200
        assert hr.get(f'/boleta/{rid}.pdf').status_code==400
        token=get_csrf(hr,'/boletas')
        assert hr.post('/boletas/masivo.pdf',data={'csrf':token,'request_ids':str(rid)},follow_redirects=False).status_code==404
    with TestClient(app) as manager:
        sign_in(manager,'encargado@demo.local')
        history=manager.get('/historial-decisiones?decision=APROBADA')
        assert history.status_code==200 and 'RECHAZADA POR RR. HH.' in history.text
        assert history.text.count('RECHAZADA POR RR. HH.')==1


def test_hr_own_request_does_not_fall_back_to_technical_admin(demo):
    with core.db() as db:
        initial=db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
    with TestClient(app) as hr:
        token=sign_in(hr,'rrhh@demo.local')
        response=hr.post('/solicitar',data={'csrf':token,'kind':'PERMISO','leave_type':'PERSONAL',
          'starts_at':'2026-11-17T09:00','ends_at':'2026-11-17T10:00',
          'pay_type':'CON_GOCE','justification':'Solicitud sin superior operativo configurado.'},follow_redirects=False)
        assert response.status_code==303 and response.headers['location']=='/solicitar'
    with core.db() as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==initial


def test_bulk_boleta_pdf_uses_letter_and_two_slots_per_sheet(demo):
    with TestClient(app) as c:
        token=sign_in(c,'ana@demo.local')
        c.post('/solicitar',data={'csrf':token,'kind':'PERMISO','leave_type':'MEDICO',
          'starts_at':'2026-11-16T09:00','ends_at':'2026-11-16T12:00','place':'Hospital',
          'pay_type':'CON_GOCE','justification':'Control médico programado con respaldo adjunto.'},follow_redirects=False)
        with core.db() as db:rid=db.execute('SELECT MAX(id) FROM requests').fetchone()[0]
    with TestClient(app) as c:
        token=sign_in(c,'encargado@demo.local')
        c.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'Aprobado para revisión final'},follow_redirects=False)
    with TestClient(app) as c:
        token=sign_in(c,'rrhh@demo.local')
        c.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'Aprobación final registrada'},follow_redirects=False)
        management=c.get('/boletas')
        assert management.status_code==200
        assert BeautifulSoup(management.text,'html.parser').title.string=='Gestión de boletas · Boletas'
        assert 'Descargar todas las boletas (2 por hoja)' in management.text
        token=get_csrf(c,'/boletas')
        bulk=c.post('/boletas/masivo.pdf',data={'csrf':token,'request_ids':str(rid)},follow_redirects=False)
        assert bulk.status_code==200 and bulk.content.startswith(b'%PDF-')
        with core.db() as db:
            assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('GENERACION_MASIVA_PDF','DESCARGA_MASIVA_PDF') AND details LIKE ?",(f'%BP-2026-{rid:06d}%',)).fetchone()[0]==2
        bulk_all=c.post('/boletas/masivo.pdf',data={'csrf':token,'download_all':'1'},follow_redirects=False)
        assert bulk_all.status_code==200 and bulk_all.content.startswith(b'%PDF-')
    with core.db() as db:item=__import__('app').load_boleta_data(db,rid)
    for count,pages in [(1,1),(2,1),(3,2),(4,2),(5,3),(10,5),(20,10)]:
        items=[]
        for ix in range(count):
            clone={**item,'request':{**item['request'],'id':1000+ix,'folio':f'BP-2026-{ix+1:06d}'},'employee':{**item['employee'],'name':f'Empleado de prueba {ix+1}'}}
            items.append(clone)
        pdf=pdf_boletas(items)
        actual=len(re.findall(rb'/Type\s*/Page\b',pdf))
        assert actual==pages
        assert len(re.findall(rb'/MediaBox\s*\[\s*0\s+0\s+612\s+792\s*\]',pdf))==pages
        assert pdf.count(b'BT')>0 and b'BOLETA DE PERMISO' in pdf and b'/Subtype /Image' in pdf
    with pytest.raises(ValueError,match='misma solicitud'):
        pdf_boletas([item,item])


def test_import_idempotent_and_keeps_raw(demo):
    csvdata=b'empleado_id,fecha,hora,tipo\nEMP001,2026-11-06,08:02,Entrada\nEMP001,2026-11-06,17:05,Salida\n'
    with TestClient(app) as c:
        sign_in(c,'rrhh@demo.local')
        token=get_csrf(c,'/importar')
        r=c.post('/importar',data={'csrf':token},files={'file':('marcaciones.csv',csvdata,'text/csv')},follow_redirects=True)
        assert '2 marcaciones nuevas' in r.text
        r=c.post('/importar',data={'csrf':token},files={'file':('marcaciones.csv',csvdata,'text/csv')},follow_redirects=True)
        assert '2 duplicadas' in r.text
        with core.db() as db:
            assert db.execute('SELECT COUNT(*) FROM punches WHERE source="marcaciones.csv"').fetchone()[0]==2
            batch=db.execute('''SELECT rows_total,rows_added,rows_duplicate,rows_error
                                FROM import_batches ORDER BY id DESC LIMIT 1''').fetchone()
            assert tuple(batch)==(2,0,2,0)


def test_biometric_codes_are_scoped_by_branch_and_device(demo):
    with core.db() as db:
        ana=db.execute("SELECT id FROM users WHERE employee_code='EMP001'").fetchone()['id']
        carlos=db.execute("SELECT id FROM users WHERE employee_code='EMP002'").fetchone()['id']
        db.executemany('''INSERT INTO biometric_assignments
            (branch_id,device_code,employee_code,user_id,valid_from,created_at)
            VALUES(?,?,?,?,?,?)''',[
                (1,'DEVICE-A','DUP-7',ana,'2026-01-01','2026-01-01 00:00:00'),
                (2,'DEVICE-B','DUP-7',carlos,'2026-01-01','2026-01-01 00:00:00')])
        with pytest.raises(ValueError,match='ambiguo'):
            resolve_biometric_user(db,'DUP-7')
        assert resolve_biometric_user(db,'DUP-7',1,'DEVICE-A')==ana
        with pytest.raises(ValueError,match='otra sucursal'):
            resolve_biometric_user(db,'DUP-7',3,'DEVICE-C')


def test_closed_period_locks_import_and_is_frozen(demo):
    with core.db() as db:
        now_sim=datetime(2026,10,26,0,5,tzinfo=core.TZ)
        assert core.close_period(db,date(2026,10,25),actor_id=1,timestamp=now_sim)
        assert not core.close_period(db,date(2026,10,25),actor_id=1,timestamp=now_sim)
        assert core.closed_for_date(db,date(2026,10,21))
        frozen,_=core.get_report(db,date(2026,10,25))
        count=len(frozen['detalle'])
        assert count==((date(2026,10,25)-date(2026,9,26)).days+1)*5
        # Changing schedule cannot mutate snapshot while closed
        db.execute('UPDATE users SET start_time="11:00" WHERE employee_code="EMP001"')
        later,yes=core.get_report(db,date(2026,10,25))
        assert yes and later==frozen
        core.reopen_period(db,date(2026,10,25),1,'Faltaba el respaldo de una jornada')
        assert not core.closed_for_date(db,date(2026,10,21))
        history=db.execute('''SELECT version,event_type,state FROM monthly_closure_versions
                              WHERE closure_id=(SELECT id FROM monthly_closures WHERE period_end='2026-10-25')
                              ORDER BY version''').fetchall()
        assert [tuple(row) for row in history]==[(1,'CERRADO','CERRADO'),(2,'REABIERTO','REABIERTO')]


def test_hr_can_view_closure_versions(demo):
    with core.db() as db:
        core.close_period(db,date(2026,10,25),actor_id=1,
                          timestamp=datetime(2026,10,26,0,5,tzinfo=core.TZ))
    with TestClient(app) as c:
        sign_in(c,'rrhh@demo.local')
        response=c.get('/reportes?fin=2026-10-25')
        assert response.status_code==200
        assert 'Historial de versiones' in response.text
        assert 'Período cerrado' in response.text


def test_internal_notifications_are_private_and_markable(demo):
    with core.db() as db:
        ana=db.execute("SELECT id FROM users WHERE employee_code='EMP001'").fetchone()['id']
        carlos=db.execute("SELECT id FROM users WHERE employee_code='EMP002'").fetchone()['id']
        core.notify(db,ana,'PRUEBA','Aviso privado de prueba','solicitud',51)
    with TestClient(app) as c:
        sign_in(c,'ana@demo.local')
        assert 'Aviso privado de prueba' in c.get('/notificaciones').text
        token=get_csrf(c,'/notificaciones')
        assert c.post('/notificaciones/leer',data={'csrf':token},follow_redirects=False).status_code==303
    with core.db() as db:
        notification=db.execute('SELECT user_id,read_at FROM internal_notifications').fetchone()
        assert notification['user_id']==ana and notification['read_at']
        assert db.execute('SELECT COUNT(*) FROM internal_notifications WHERE user_id=?',(carlos,)).fetchone()[0]==0


def test_header_mapping_and_unspecified():
    rows,mapping=rows_from_file('zlink.csv',b'Employee ID;Date;Time;Punch State\nEMP001;2026-11-09;08:01;\n')
    emp,dt,kind=parse_event(rows[0],mapping)
    assert emp=='EMP001' and dt=='2026-11-09 08:01:00' and kind=='UNSPEC'
    with pytest.raises(ValueError):
        identify_headers(['Nombre completo','Fecha','Hora'])


def test_csrf_and_self_approval_prevented(demo):
    with TestClient(app) as c:
        sign_in(c,'ana@demo.local')
        assert c.post('/solicitar',data={'kind':'PERMISO'}).status_code==403
    with core.db() as db:
        manager=db.execute('SELECT * FROM users WHERE employee_code="GER001"').fetchone()
        r=db.execute('SELECT * FROM requests ORDER BY id DESC LIMIT 1').fetchone()
        assert not core.may_approve(db,db.execute('SELECT * FROM users WHERE id=?',(r['user_id'],)).fetchone(),r)


def test_saturday_is_configurable_without_inventing_a_schedule(demo):
    with core.db() as db:
        employee=db.execute('''SELECT u.*,a.name area_name FROM users u JOIN areas a ON a.id=u.area_id
                               WHERE employee_code='EMP001' ''').fetchone()
        saturday=date(2026,11,7)
        assert core.daily_status(db,employee,saturday,datetime(2026,11,8,tzinfo=core.TZ))['estado']=='NO_LABORAL'
        cur=db.execute('INSERT INTO work_schedules(name,created_at) VALUES(?,?)',
                       ('Sábado de prueba',core.nowstr()))
        schedule_id=cur.lastrowid
        db.execute('''INSERT INTO work_schedule_days
            (schedule_id,weekday,is_working,start_time,end_time,grace_minutes)
            VALUES(?,5,1,'08:00','12:00',5)''',(schedule_id,))
        db.execute('''INSERT INTO work_schedule_days
            (schedule_id,weekday,is_working,start_time,end_time,grace_minutes)
            VALUES(?,0,1,'09:00','17:00',5)''',(schedule_id,))
        db.execute('''INSERT INTO schedule_assignments(schedule_id,user_id,valid_from,created_at)
                      VALUES(?,?,?,?)''',(schedule_id,employee['id'],'2026-11-01',core.nowstr()))
        for at,kind in [('08:02','IN'),('12:00','OUT')]:
            stamp=f'{saturday} {at}:00'
            key=f'saturday-{at}'
            db.execute('''INSERT INTO punches(user_id,occurred_at,kind,source,source_key,created_at)
                VALUES(?,?,?,'TEST',?,?)''',(employee['id'],stamp,kind,key,core.nowstr()))
        result=core.daily_status(db,employee,saturday,datetime(2026,11,8,tzinfo=core.TZ))
        assert result['estado']=='PUNTUAL'


def test_duplicate_area_names_and_approval_sequence(demo):
    with core.db() as db:
        db.execute("INSERT INTO areas(name) VALUES('Operaciones')")
        assert db.execute("SELECT COUNT(*) FROM areas WHERE name='Operaciones'").fetchone()[0]==2
        request=db.execute('SELECT id FROM requests LIMIT 1').fetchone()
        steps=db.execute('''SELECT step_order,assignee_role,is_legacy FROM request_approval_steps
            WHERE request_id=? ORDER BY step_order''',(request['id'],)).fetchall()
        assert [(row['step_order'],row['assignee_role'],row['is_legacy']) for row in steps]==[
            (1,'manager',0),(2,'hr',0)]


def test_manager_scope_and_personnel_write_restriction(demo):
    with core.db() as db:
        outsider=db.execute("SELECT id FROM users WHERE employee_code='ADM001'").fetchone()['id']
    with TestClient(app) as c:
        token=sign_in(c,'encargado@demo.local')
        team=c.get('/mi-equipo')
        assert team.status_code==200 and 'EMP001' in team.text and 'EMP002' in team.text
        assert 'Sofía Rojas' not in team.text
        assert c.get(f'/mi-equipo/{outsider}').status_code==404
        assert c.get('/mi-equipo/asistencia').status_code==200
        assert c.get('/equipo').status_code==403
        assert c.post('/equipo/empresa',data={'csrf':token,'name':'No autorizada'}).status_code==404


def test_manager_rejection_stops_later_steps(demo):
    with core.db() as db:
        request_id=db.execute('SELECT id FROM requests LIMIT 1').fetchone()['id']
    with TestClient(app) as c:
        sign_in(c,'encargado@demo.local')
        token=get_csrf(c,'/aprobaciones')
        response=c.post(f'/aprobaciones/{request_id}/resolver',
          data={'csrf':token,'decision':'RECHAZADA','note':'No corresponde por fechas'},follow_redirects=True)
        assert 'rechazada' in response.text.lower()
        assert c.get(f'/boleta/{request_id}.pdf').status_code==400
    with core.db() as db:
        request=db.execute('SELECT status FROM requests WHERE id=?',(request_id,)).fetchone()
        final=db.execute('''SELECT decision,is_skipped,skip_reason FROM request_approval_steps
            WHERE request_id=? AND step_order=2''',(request_id,)).fetchone()
        assert request['status']=='RECHAZADA'
        assert final['decision'] is None and final['is_skipped']==1
        assert 'rechazo' in final['skip_reason']


def test_organization_migration_marks_legacy_people_pending(demo):
    with core.db() as db:
        assert db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]==len(migrations.MIGRATIONS)
        assert db.execute("SELECT COUNT(*) FROM branches WHERE company_id=(SELECT id FROM companies WHERE name='KIMTEK') AND active=1 AND classification_status='CLASIFICADA'").fetchone()[0]==6
        assert db.execute('''SELECT COUNT(*) FROM employee_placements
            WHERE classification_status='PENDIENTE' AND company_id IS NULL AND branch_id IS NULL''').fetchone()[0]==5
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_kimtek_single_company_and_role_specific_registration(demo):
    with core.db() as db:
        kimtek=db.execute("SELECT id FROM companies WHERE name='KIMTEK' AND active=1").fetchone()['id']
        branches=db.execute('SELECT id,name FROM branches WHERE company_id=? AND active=1 ORDER BY id',(kimtek,)).fetchall()
        assert [r['name'] for r in branches]==['Kimstar','Sede Central','Mercatech El Alto','Mercatech Riosinio','Mercatech La Paz','Mercatech Cochabamba']
        branch_id=branches[0]['id']
    with TestClient(app) as c:
        token=sign_in(c,'rrhh@demo.local')
        page=c.get('/equipo')
        assert 'Empresas' not in page.text and 'Asignar empresa' not in page.text
        assert 'KIMTEK' in page.text and 'Crear encargado' in page.text and 'Crear trabajador' in page.text
        assert c.post('/equipo/sucursal',data={'csrf':token,'name':'Nueva Sede'},follow_redirects=False).status_code==303
        with core.db() as db:
            new_branch=db.execute("SELECT company_id,active FROM branches WHERE name='Nueva Sede'").fetchone()
            assert tuple(new_branch)==(kimtek,1)
        assert c.post('/equipo/area',data={'csrf':token,'name':'Area temporal','branch_id':str(branch_id)},follow_redirects=False).status_code==303
        with core.db() as db:
            area_id=db.execute("SELECT id FROM areas WHERE name='Area temporal'").fetchone()['id']
        worker={'csrf':token,'name':'Trabajador sin jefe','code':'SINJ001','email':'sinj@example.local','area_id':str(area_id),'branch_id':str(branch_id),'role':'employee','password':'StrongDemo123!','start_time':'08:00','end_time':'17:00'}
        c.post('/equipo/nuevo',data=worker,follow_redirects=False)
        with core.db() as db:
            assert db.execute("SELECT 1 FROM users WHERE employee_code='SINJ001'").fetchone() is None
        manager=dict(worker,name='Primer Encargado',code='MGRNEW1',email='manager-new@example.local',role='manager')
        response=c.post('/equipo/nuevo',data=manager,follow_redirects=False)
        assert response.status_code==303
        with core.db() as db:
            manager_id=db.execute("SELECT id FROM users WHERE employee_code='MGRNEW1'").fetchone()['id']
            placement=db.execute('SELECT supervisor_id,branch_id FROM employee_placements WHERE user_id=? AND valid_to IS NULL',(manager_id,)).fetchone()
            assert tuple(placement)==(None,branch_id)
        c.post('/equipo/responsable',data={'csrf':token,'area_id':str(area_id),'principal_id':str(manager_id)},follow_redirects=False)
        worker.update(name='Trabajador Con Jefe',code='CONJ001',email='conj@example.local',supervisor_id=str(manager_id))
        response=c.post('/equipo/nuevo',data=worker,follow_redirects=False)
        assert response.status_code==303
        with core.db() as db:
            placement=db.execute("SELECT p.company_id,p.branch_id,p.area_id,p.supervisor_id FROM employee_placements p JOIN users u ON u.id=p.user_id WHERE u.employee_code='CONJ001' AND p.valid_to IS NULL").fetchone()
            assert tuple(placement)==(kimtek,branch_id,area_id,manager_id)
        c.post('/logout',data={'csrf':get_csrf(c,'/mi-perfil')},follow_redirects=False)
        login=c.get('/login');csrf=BeautifulSoup(login.text,'html.parser').select_one('input[name="csrf"]')['value']
        c.post('/login',data={'csrf':csrf,'email':'manager-new@example.local','password':'StrongDemo123!'},follow_redirects=True)
        team=c.get('/mi-equipo')
        assert 'CONJ001' in team.text and 'EMP001' not in team.text
        assert c.get('/equipo').status_code==403



def test_hr_can_open_and_edit_person_and_adscription_in_one_form(demo):
    with core.db() as db:
        person=db.execute("SELECT * FROM users WHERE employee_code='EMP001'").fetchone()
        user_id=person['id'];values=dict(person)
        branch_id=db.execute("SELECT id FROM branches WHERE name='Sede Central'").fetchone()['id']
        area_id=db.execute("SELECT id FROM areas WHERE name='Operaciones'").fetchone()['id']
        manager_id=db.execute("SELECT id FROM users WHERE employee_code='GER001'").fetchone()['id']
    with TestClient(app) as c:
        token=sign_in(c,'rrhh@demo.local')
        c.post('/equipo/area/adscribir',data={'csrf':token,'area_id':str(area_id),'branch_id':str(branch_id)},follow_redirects=False)
        page=c.get(f'/equipo/{user_id}/editar')
        assert page.status_code==200 and 'Adscripci' in page.text
        soup=BeautifulSoup(page.text,'html.parser')
        assert len(soup.select(f'form[action="/equipo/{user_id}/editar"]'))==1
        assert not soup.select(f'form[action="/equipo/{user_id}/adscripcion"]')
        form=soup.select_one(f'form[action="/equipo/{user_id}/editar"]')
        token=form.select_one('input[name="csrf"]')['value']
        response=c.post(f'/equipo/{user_id}/editar',data={
            'csrf':token,'name':'Ana Perez Actualizada','code':values['employee_code'],
            'email':values['email'],'document_id':values['document_id'],'position':'Analista',
            'role':values['role'],'start_time':values['start_time'],'end_time':values['end_time'],
            'grace_minutes':str(values['grace_minutes']),'active':'1','branch_id':str(branch_id),
            'area_id':str(area_id),'supervisor_id':str(manager_id),'effective_from':core.now().date().isoformat()},
            follow_redirects=False)
        assert response.status_code==303
    with core.db() as db:
        assert tuple(db.execute('SELECT name,position FROM users WHERE id=?',(user_id,)).fetchone())==('Ana Perez Actualizada','Analista')
        placements=db.execute('SELECT branch_id,area_id,supervisor_id,valid_from,valid_to,classification_status FROM employee_placements WHERE user_id=? ORDER BY id',(user_id,)).fetchall()
        assert len(placements)==2 and placements[0]['valid_to'] and tuple(placements[1][:3])==(branch_id,area_id,manager_id)
        assert placements[1]['classification_status']=='CLASIFICADA'


def test_hr_can_change_direct_manager_without_transferring_area(demo):
    with core.db() as db:
        employee=db.execute("SELECT u.id,u.employee_code,p.area_id,p.branch_id,p.supervisor_id FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.employee_code='EMP001' AND p.valid_to IS NULL").fetchone()
        manager=db.execute("SELECT id FROM users WHERE employee_code='GER001'").fetchone()['id']
        branch_id=db.execute("SELECT id FROM branches WHERE name='Sede Central'").fetchone()['id']
    with TestClient(app) as c:
        token=sign_in(c,'rrhh@demo.local')
        c.post('/equipo/area/adscribir',data={'csrf':token,'area_id':str(employee['area_id']),'branch_id':str(branch_id)},follow_redirects=False)
        c.post(f"/equipo/{employee['id']}/adscripcion",data={'csrf':token,'branch_id':str(branch_id),
            'area_id':str(employee['area_id']),'supervisor_id':str(manager),
            'effective_from':core.now().date().isoformat()},follow_redirects=False)
        created=c.post('/equipo/nuevo',data={'csrf':token,'name':'Segundo Encargado','code':'MGRSECOND','email':'manager-second@example.local','role':'manager','branch_id':str(branch_id),'area_id':str(employee['area_id']),'password':'StrongDemo123!','start_time':'08:00','end_time':'17:00'},follow_redirects=False)
        assert created.status_code==303
        with core.db() as db:
            second_manager=db.execute("SELECT id FROM users WHERE employee_code='MGRSECOND'").fetchone()['id']
        # Make both managers responsible options for this area.
        c.post('/equipo/responsable',data={'csrf':token,'area_id':str(employee['area_id']),'principal_id':str(manager),'substitute_id':str(second_manager)},follow_redirects=False)
        page=c.get(f"/equipo/{employee['id']}/editar")
        assert page.status_code==200 and 'data-areas' in page.text
        response=c.post(f"/equipo/{employee['id']}/editar",data={
            'csrf':token,'name':'Ana','code':employee['employee_code'],'email':'ana@example.local',
            'role':'employee','start_time':'08:00','end_time':'17:00','grace_minutes':'5',
            'active':'1','supervisor_id':str(second_manager)},follow_redirects=False)
        assert response.status_code==303
    with core.db() as db:
        placements=db.execute('SELECT area_id,supervisor_id,valid_to FROM employee_placements WHERE user_id=? ORDER BY id',(employee['id'],)).fetchall()
        assert len(placements)==2 and placements[0]['valid_to']
        assert not db.execute('SELECT 1 FROM employee_placements WHERE valid_to<valid_from').fetchone()
        assert db.execute("SELECT COUNT(*) FROM organization_revisions WHERE entity='employee_placements'").fetchone()[0]>=2
        assert placements[1]['area_id']==employee['area_id'] and placements[1]['supervisor_id']==second_manager


def test_manager_without_superior_request_routes_directly_to_hr(demo):
    with core.db() as db:
        manager=db.execute("SELECT * FROM users WHERE employee_code='GER001'").fetchone()
        manager_id=manager['id']
    with TestClient(app) as c:
        sign_in(c,'encargado@demo.local')
        token=get_csrf(c,'/solicitar')
        response=c.post('/solicitar',data={'csrf':token,'kind':'PERMISO','leave_type':'PERSONAL',
            'starts_at':'2026-11-11T09:00','ends_at':'2026-11-11T10:00','place':'KIMTEK',
            'pay_type':'CON_GOCE','justification':'Gestion personal autorizada para una cita.'},follow_redirects=False)
        assert response.status_code==303
        with core.db() as db:
            req=db.execute('SELECT * FROM requests ORDER BY id DESC LIMIT 1').fetchone()
            steps=db.execute('SELECT * FROM request_approval_steps WHERE request_id=? ORDER BY step_order',(req['id'],)).fetchall()
            assert req['approver_id']!=manager_id
            assert steps[0]['is_skipped']==1 and steps[1]['assignee_role']=='hr'
            assert not core.may_approve(db,manager,req)
        c.post('/logout',data={'csrf':get_csrf(c,'/mi-perfil')},follow_redirects=False)
        sign_in(c,'rrhh@demo.local')
        token=get_csrf(c,'/aprobaciones')
        assert c.post(f"/aprobaciones/{req['id']}/resolver",data={'csrf':token,'decision':'APROBADA','note':'Aprobado por Recursos Humanos'},follow_redirects=False).status_code==303


def test_notification_deduplication_private_links_and_single_read(demo):
    with core.db() as db:
        ana=db.execute("SELECT id FROM users WHERE employee_code='EMP001'").fetchone()['id']
        carlos=db.execute("SELECT id FROM users WHERE employee_code='EMP002'").fetchone()['id']
        core.notify(db,ana,'SOLICITUD_APROBADA','Permiso aprobado','solicitud',77)
        core.notify(db,ana,'SOLICITUD_APROBADA','Permiso aprobado','solicitud',77)
    with TestClient(app) as c:
        sign_in(c,'ana@demo.local')
        page=c.get('/notificaciones')
        assert 'Abrir solicitud' in page.text and 'Marcar le&#237;da' in page.text
        with core.db() as db:
            row=db.execute("SELECT id FROM internal_notifications WHERE user_id=? AND entity_id='77'",(ana,)).fetchone()
            assert db.execute("SELECT COUNT(*) FROM internal_notifications WHERE user_id=? AND entity_id='77'",(ana,)).fetchone()[0]==1
            assert db.execute("SELECT COUNT(*) FROM internal_notifications WHERE user_id=? AND entity_id='77'",(carlos,)).fetchone()[0]==0
        token=get_csrf(c,'/notificaciones')
        assert c.post(f"/notificaciones/{row['id']}/leer",data={'csrf':token},follow_redirects=False).status_code==303
        with core.db() as db:
            assert db.execute('SELECT read_at FROM internal_notifications WHERE id=?',(row['id'],)).fetchone()['read_at']



def test_migration_preserves_legacy_company_relationships(tmp_path):
    dbpath=tmp_path/'companies.sqlite3'
    with core.sqlite3.connect(dbpath) as cx:
        cx.executescript(core.SCHEMA)
        cx.execute("INSERT INTO areas(id,name) VALUES(1,'Legacy Area')")
        cx.execute("INSERT INTO users(id,employee_code,name,area_id,email,role,password_hash,created_at) VALUES(1,'OLD001','Legacy User',1,'old@example.local','employee','hash','2020-01-01 00:00:00')")
        cx.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        cx.commit()
        for version,migration in migrations.MIGRATIONS[:3]:
            if version==1: cx.execute('PRAGMA foreign_keys=OFF')
            migration(cx)
            cx.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',(version,'2020-01-01'))
            cx.commit()
            if version==1: cx.execute('PRAGMA foreign_keys=ON')
        old=cx.execute("INSERT INTO companies(name,active,created_at) VALUES('OldCo',1,'2020-01-01')").lastrowid
        old_kimtek=cx.execute("INSERT INTO companies(name,active,created_at) VALUES('Kimtek',1,'2020-01-01')").lastrowid
        sede=cx.execute("SELECT id FROM branches WHERE name='SEDE CENTRAL'").fetchone()[0]
        cx.execute('UPDATE branches SET company_id=? WHERE id=?',(old_kimtek,sede))
        branch=cx.execute("SELECT id FROM branches WHERE name='KIMSTAR'").fetchone()[0]
        cx.execute("UPDATE branches SET company_id=?,classification_status='CLASIFICADA' WHERE id=?",(old,branch))
        cx.execute('UPDATE areas SET branch_id=? WHERE id=1',(branch,))
        cx.execute("UPDATE employee_placements SET company_id=?,branch_id=?,classification_status='CLASIFICADA' WHERE user_id=1",(old,branch))
        migrations.MIGRATIONS[3][1](cx)
        with cx:
            company=cx.execute("SELECT active FROM companies WHERE id=?",(old,)).fetchone()[0]
            canonical=cx.execute("SELECT id,name,active FROM companies WHERE upper(name)='KIMTEK'").fetchone()
            placement=cx.execute('SELECT company_id,branch_id FROM employee_placements WHERE user_id=1').fetchone()
            branch_link=cx.execute('SELECT company_id FROM branches WHERE id=?',(branch,)).fetchone()[0]
            assert company==0 and tuple(placement)==(old,branch) and branch_link==old
            assert tuple(canonical)==(old_kimtek,'KIMTEK',1)
            assert cx.execute('SELECT company_id FROM branches WHERE id=?',(sede,)).fetchone()[0]==old_kimtek
            assert cx.execute('PRAGMA foreign_key_check').fetchall()==[]



def test_pending_migration_creates_verified_backup(tmp_path,monkeypatch):
    path=tmp_path/'pending.sqlite3'
    monkeypatch.setattr(core,'DB_PATH',path)
    with core.sqlite3.connect(path) as cx:
        cx.executescript(core.SCHEMA)
        cx.execute("INSERT INTO areas(id,name) VALUES(1,'Backup Area')")
        cx.execute("INSERT INTO users(id,employee_code,name,area_id,email,role,password_hash,created_at) VALUES(1,'BACK001','Backup Person',1,'backup@example.local','employee','hash','2020-01-01 00:00:00')")
        cx.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)')
        cx.commit()
        for version,migration in migrations.MIGRATIONS[:3]:
            if version==1:cx.execute('PRAGMA foreign_keys=OFF')
            migration(cx)
            cx.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',(version,'2020-01-01'))
            cx.commit()
            if version==1:cx.execute('PRAGMA foreign_keys=ON')
    core.initialize()
    backups=list((tmp_path/'backups').glob(f'pending-pre-v3-to-v{len(migrations.MIGRATIONS)}-*.sqlite3'))
    assert len(backups)==1
    with core.sqlite3.connect(backups[0]) as cx:
        assert cx.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert cx.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==3
        assert cx.execute('SELECT COUNT(*) FROM users').fetchone()[0]==1
    with core.db() as cx:
        assert cx.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==len(migrations.MIGRATIONS)

def test_precierre_notice_is_deduplicated_and_only_sent_to_hr(demo,monkeypatch):
    fixed=datetime(2026,11,25,8,0,tzinfo=core.TZ)
    import importlib
    app_module=importlib.import_module('app')
    monkeypatch.setattr('app.now',lambda:fixed)
    app_module.notify_preclose_if_due();app_module.notify_preclose_if_due()
    with core.db() as db:
        hr=db.execute("SELECT id FROM users WHERE role='hr'").fetchone()['id']
        admins=[r['id'] for r in db.execute("SELECT id FROM users WHERE role='admin'")]
        assert db.execute("SELECT COUNT(*) FROM internal_notifications WHERE user_id=? AND kind='PRECierre'",(hr,)).fetchone()[0]==1
        assert all(db.execute("SELECT COUNT(*) FROM internal_notifications WHERE user_id=? AND kind='PRECierre'",(user_id,)).fetchone()[0]==0 for user_id in admins)

def test_legacy_migration_preserves_data_and_creates_verified_backup(tmp_path,monkeypatch):
    legacy_db=tmp_path/'legacy.sqlite3'
    monkeypatch.setattr(core,'DB_PATH',legacy_db)
    with core.sqlite3.connect(legacy_db) as db:
        db.executescript(core.SCHEMA)
        db.execute("INSERT INTO areas(id,name) VALUES(41,'Operaciones')")
        db.execute('''INSERT INTO users(id,employee_code,name,area_id,email,role,password_hash,created_at)
            VALUES(73,'LEGACY-1','Personal histórico',41,'legacy@example.local','employee','hash','2020-01-02 08:00:00')''')
        db.execute('''INSERT INTO punches(id,user_id,occurred_at,kind,source,source_key,created_at)
            VALUES(91,73,'2020-02-01 08:00:00','IN','legacy','old-key','2020-02-01 08:01:00')''')
        db.execute('''INSERT INTO requests(id,user_id,approver_id,kind,justification,submitted_at,status)
            VALUES(105,73,73,'PERMISO','Solicitud histórica válida','2020-02-01 08:10:00','APROBADA')''')
        db.execute('''INSERT INTO monthly_closures(id,period_start,period_end,state,closed_at,snapshot_json)
            VALUES(12,'2020-01-26','2020-02-25','CERRADO','2020-02-26 00:01:00','{}')''')
        db.execute('''INSERT INTO audit_log(id,actor_id,action,entity,entity_id,details,at)
            VALUES(18,73,'LEGACY','usuario','73','evento histórico','2020-02-01 08:11:00')''')
    core.initialize()
    with core.db() as db:
        assert db.execute('SELECT id FROM areas WHERE id=41').fetchone()['id']==41
        assert db.execute('SELECT id FROM users WHERE id=73').fetchone()['id']==73
        assert db.execute('SELECT id FROM punches WHERE id=91').fetchone()['id']==91
        assert db.execute('SELECT id FROM requests WHERE id=105').fetchone()['id']==105
        assert db.execute('SELECT employee_name_snapshot FROM requests WHERE id=105').fetchone()['employee_name_snapshot']==db.execute('SELECT name FROM users WHERE id=73').fetchone()['name']
        assert db.execute('SELECT id FROM monthly_closures WHERE id=12').fetchone()['id']==12
        assert db.execute('SELECT id FROM audit_log WHERE id=18').fetchone()['id']==18
        stage=db.execute('''SELECT is_legacy,decision FROM request_approval_steps WHERE request_id=105''').fetchone()
        assert stage['is_legacy']==1 and stage['decision']=='APROBADA'
        history=db.execute('''SELECT event_type,state FROM monthly_closure_versions
                              WHERE closure_id=12''').fetchone()
        assert tuple(history)==('CERRADO','CERRADO')
        assert [row['version'] for row in db.execute('SELECT version FROM schema_migrations ORDER BY version')]==list(range(1,len(migrations.MIGRATIONS)+1))
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    backups=list((tmp_path/'backups').glob('legacy-pre-migration-*.sqlite3'))
    assert len(backups)==1
    with core.sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert backup.execute('SELECT id FROM users WHERE id=73').fetchone()[0]==73
