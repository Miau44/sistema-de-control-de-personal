"""Stage 3A.3 HTTP access, document scopes and real approval flows on disposable copies."""
from contextlib import ExitStack
import json
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import core
import organization
import access_control
from attendance_authorizations import authorization
from app import app
from test_local_setup import prepared,local_copy

PASSWORD='Isolated-Capability-Test-2026!'
PASSWORD_HASH=core.hash_password(PASSWORD)


def csrf(client):
    return BeautifulSoup(client.get('/mi-perfil').text,'html.parser').select_one('input[name=csrf]')['value']


@pytest.fixture
def users(local_copy):
    with core.db() as c:
        c.execute('UPDATE users SET password_hash=?',(PASSWORD_HASH,))
        result={r['employee_code']:dict(r) for r in c.execute('SELECT * FROM users')}
        result['WORKER']=dict(c.execute("SELECT * FROM users WHERE role='employee' ORDER BY id LIMIT 1").fetchone())
        m=core.choose_approver(c,result['WORKER'],'manager');result['MANAGER']=dict(m)
        result['OTHER']=dict(c.execute("SELECT * FROM users WHERE role='employee' AND id<>? AND id NOT IN (SELECT user_id FROM employee_placements WHERE supervisor_id=? AND valid_to IS NULL) LIMIT 1",(result['WORKER']['id'],m['id'])).fetchone())
    return result


@pytest.fixture
def login(users):
    with ExitStack() as stack:
        def enter(code):
            client=stack.enter_context(TestClient(app))
            token=BeautifulSoup(client.get('/login').text,'html.parser').select_one('input[name=csrf]')['value']
            assert client.post('/login',data={'csrf':token,'email':users[code]['email'],'password':PASSWORD}).status_code==200
            return client
        yield enter


GLOBAL_GETS=['/equipo','/equipo/5/editar','/calendario','/importar','/conciliacion',
 '/conciliacion/detalle?user_id=5&fecha=2026-10-12','/conciliacion/identidad/8282765',
 '/conciliacion/padron','/conciliacion/referencia-rrhh','/reportes','/reportes/descargar.xlsx?fin=2026-10-25',
 '/historial-solicitudes','/boletas','/auditoria']


@pytest.mark.parametrize('url',GLOBAL_GETS)
def test_administration_cannot_enter_global_hr_screens(login,url):
    assert login('ADM_OPER').get(url).status_code==403


GLOBAL_POSTS=['/equipo/nuevo','/equipo/5/editar','/equipo/5/adscripcion','/equipo/area',
 '/equipo/sucursal','/equipo/sucursal/1/editar','/equipo/area/adscribir','/equipo/area/1/editar',
 '/equipo/responsable','/equipo/horarios','/calendario','/importar','/importar/preview',
 '/importar/confirmar','/importar/biometria','/conciliacion/recalcular','/conciliacion/resolver',
 '/conciliacion/regimen','/conciliacion/asignacion','/conciliacion/calendario',
 '/conciliacion/agrupacion/regla','/conciliacion/agrupacion/politica','/conciliacion/cobertura/1',
 '/conciliacion/importar/preview','/conciliacion/importar/confirmar',
 '/conciliacion/identidades/8282765','/conciliacion/identidad/8282765/padron',
 '/conciliacion/referencia-rrhh/importar','/conciliacion/referencia-rrhh/1/confirmar',
 '/reportes/cerrar','/reportes/reabrir','/boletas/masivo.pdf']


@pytest.mark.parametrize('url',GLOBAL_POSTS)
def test_administration_post_denied_even_with_valid_csrf(login,url):
    c=login('ADM_OPER');assert c.post(url,data={'csrf':csrf(c)}).status_code==403


@pytest.mark.parametrize('url',[u for u in GLOBAL_GETS if u not in ('/importar','/auditoria')]+['/aprobaciones','/solicitar'])
def test_technical_admin_has_no_implicit_labor_access(login,url):
    assert login('ADMIN_TEC').get(url).status_code==403


@pytest.mark.parametrize('code',['OP_RRHH','RESP_RRHH'])
def test_legitimate_hr_keeps_modules_and_navigation(login,code):
    c=login(code)
    for url in ['/equipo','/calendario','/importar','/conciliacion','/reportes','/historial-solicitudes','/boletas','/auditoria']:
        assert c.get(url).status_code==200,url
    nav=BeautifulSoup(c.get('/').text,'html.parser').select_one('nav')
    for link in nav.select('a[href]'):assert c.get(link['href']).status_code==200,link['href']


@pytest.mark.parametrize('code',['WORKER','MANAGER','ADM_OPER','ADMIN_TEC'])
def test_navigation_only_advertises_authorized_pages(login,code):
    c=login(code);soup=BeautifulSoup(c.get('/').text,'html.parser')
    nav=soup.select_one('nav')
    for a in nav.select('a[href]'):assert c.get(a['href']).status_code==200,(code,a['href'])
    if code in ('ADM_OPER','ADMIN_TEC'):
        assert not nav.select('a[href="/equipo"],a[href="/reportes"],a[href="/conciliacion"],a[href="/boletas"]')
    if code=='ADMIN_TEC':
        assert nav.select_one('a[href="/auditoria"]') and not soup.select('a[href="/solicitar"]')


def submit(client):
    response=client.post('/solicitar',data={'csrf':csrf(client),'kind':'PERMISO','leave_type':'PERSONAL',
        'starts_at':'2026-11-17T09:00','ends_at':'2026-11-17T12:00','pay_type':'CON_GOCE',
        'justification':'Solicitud aislada para comprobar capacidades y autoridades.'},follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/mis-solicitudes'
    with core.db() as c:return c.execute('SELECT MAX(id) FROM requests').fetchone()[0]


def decide(client,rid,decision='APROBADA'):
    return client.post(f'/aprobaciones/{rid}/resolver',data={'csrf':csrf(client),'decision':decision,
        'note':'Decisión independiente únicamente en copia de prueba.'},follow_redirects=False)


def test_employee_two_stages_documents_and_cross_user_idor(login,users):
    w=login('WORKER');m=login('MANAGER');h=login('OP_RRHH');other=login('OTHER');adm=login('ADM_OPER');tech=login('ADMIN_TEC')
    rid=submit(w)
    assert w.get(f'/boleta/{rid}.pdf').status_code==400
    assert decide(h,rid).status_code==403 and decide(w,rid).status_code==403
    assert decide(adm,rid).status_code==403 and decide(tech,rid).status_code==403
    assert decide(m,rid).status_code==303
    assert decide(h,rid).status_code==303
    assert w.get(f'/boleta/{rid}.pdf').status_code==200 and m.get(f'/boleta/{rid}.pdf').status_code==200
    assert other.get(f'/boleta/{rid}.pdf').status_code==403 and adm.get(f'/boleta/{rid}.pdf').status_code==403
    assert tech.get(f'/boleta/{rid}.pdf').status_code==403
    for client in (w,m,h):assert client.get(f'/historial-solicitudes/{rid}').status_code==200
    assert other.get(f'/historial-solicitudes/{rid}').status_code==403
    assert adm.get(f'/historial-solicitudes/{rid}').status_code==403
    assert f'/historial-solicitudes/{rid}' in m.get('/historial-decisiones').text
    assert m.get(f"/mi-equipo/{users['OTHER']['id']}").status_code==404
    assert w.get(f"/mi-equipo/{users['OTHER']['id']}").status_code==403
    with core.db() as c:
        r=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone();assert authorization(c,r)['valid']
        assert c.execute('SELECT COUNT(*) FROM approval_authority_evidence').fetchone()[0]==2
        # A global HR broadcast must not disclose an unrelated employee's request to Administration.
        assert not c.execute('SELECT 1 FROM internal_notifications WHERE user_id=? AND entity_id=?',(users['ADM_OPER']['id'],str(rid))).fetchone()
    assert h.post('/boletas/masivo.pdf',data={'csrf':csrf(h),'request_ids':str(rid)}).status_code==200
    assert adm.post('/boletas/masivo.pdf',data={'csrf':csrf(adm),'request_ids':str(rid)}).status_code==403


@pytest.mark.parametrize('requester,superior,outsider',[('MANAGER','OP_RRHH','RESP_RRHH'),('OP_RRHH','RESP_RRHH','ADM_OPER'),('RESP_RRHH','ADM_OPER','OP_RRHH')])
def test_configured_personal_flows_without_extra_stages(login,users,requester,superior,outsider):
    person=login(requester);boss=login(superior);outside=login(outsider);rid=submit(person)
    assert decide(person,rid).status_code==403 and decide(outside,rid).status_code==403
    assert boss.get(f'/historial-solicitudes/{rid}').status_code==200
    assert decide(boss,rid).status_code==303
    assert person.get(f'/boleta/{rid}.pdf').status_code==200
    assert boss.get(f'/boleta/{rid}.pdf').status_code==200
    assert f'/historial-solicitudes/{rid}' in boss.get('/historial-decisiones').text
    with core.db() as c:
        r=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone();assert authorization(c,r)['valid']
        assert c.execute('SELECT COUNT(*) FROM approval_authority_evidence e JOIN request_approval_steps s ON s.id=e.step_id WHERE s.request_id=?',(rid,)).fetchone()[0]==1
        assert c.execute('SELECT COUNT(*) FROM request_approval_steps WHERE request_id=? AND decision IS NOT NULL',(rid,)).fetchone()[0]==1
        # Current function or role changes do not rewrite valid historical decisions.
        c.execute("UPDATE users SET role='employee' WHERE id=?",(users[superior]['id'],))
        assert authorization(c,r)['valid']


def test_administration_without_superior_is_explicitly_blocked(login):
    c=login('ADM_OPER')
    page=c.get('/solicitar');assert page.status_code==200
    assert 'superior' in page.text.lower() and not BeautifulSoup(page.text,'html.parser').select('form[action="/solicitar"]')
    response=c.post('/solicitar',data={'csrf':csrf(c),'kind':'PERMISO','justification':'Solicitud sin superior operativo configurado.'})
    assert response.status_code==200 and 'superior' in response.text.lower()
    with core.db() as db:assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==0


def test_inactive_approver_blocks_pending_without_substitution(login,users):
    w=login('WORKER');m=login('MANAGER');h=login('OP_RRHH');rid=submit(w)
    manager_csrf=csrf(m)
    with core.db() as c:
        original=c.execute('SELECT plan_json FROM request_authority_plans WHERE request_id=?',(rid,)).fetchone()[0]
        c.execute('UPDATE users SET active=0 WHERE id=?',(users['MANAGER']['id'],))
    assert m.post(f'/aprobaciones/{rid}/resolver',data={'csrf':manager_csrf,'decision':'APROBADA'}).status_code==401
    assert decide(h,rid).status_code==403
    assert 'sustitución autorizada' in h.get(f'/historial-solicitudes/{rid}').text
    with core.db() as c:assert c.execute('SELECT plan_json FROM request_authority_plans WHERE request_id=?',(rid,)).fetchone()[0]==original
    with core.db() as c:c.execute('UPDATE users SET active=1 WHERE id=?',(users['MANAGER']['id'],))
    assert decide(m,rid).status_code==303
    with core.db() as c:
        c.execute('UPDATE users SET active=0 WHERE id IN (?,?)',(users['OP_RRHH']['id'],users['RESP_RRHH']['id']))
    assert 'sustitución autorizada' in w.get(f'/historial-solicitudes/{rid}').text


def test_rejected_and_unverified_requests_never_issue_documents(login):
    w=login('WORKER');m=login('MANAGER');h=login('OP_RRHH');rid=submit(w)
    assert decide(m,rid,'RECHAZADA').status_code==303
    assert decide(h,rid).status_code==403 and w.get(f'/boleta/{rid}.pdf').status_code==400
    rid2=submit(w)
    with core.db() as c:c.execute("UPDATE requests SET status='APROBADA' WHERE id=?",(rid2,))
    assert w.get(f'/boleta/{rid2}.pdf').status_code==400
    assert h.post('/boletas/masivo.pdf',data={'csrf':csrf(h),'request_ids':str(rid2)}).status_code==404
    assert not BeautifulSoup(w.get(f'/historial-solicitudes/{rid2}').text,'html.parser').select('a[href$=".pdf"]')


def test_endpoint_inventory_has_no_unclassified_routes():
    assert {r.name for r in app.routes if getattr(r,'path','').startswith('/') and getattr(r,'methods',None)}<=access_control.CLASSIFIED


def test_transfer_preserves_own_decision_but_not_current_file_access(login,users):
    w=login('WORKER');m=login('MANAGER');h=login('OP_RRHH');rid=submit(w)
    assert decide(m,rid).status_code==303 and decide(h,rid).status_code==303
    with core.db() as c:
        p=organization.placement(c,users['WORKER']['id'],core.now().date())
        target=c.execute('''SELECT ar.area_id,ar.user_id FROM area_responsibles ar JOIN areas a ON a.id=ar.area_id
            WHERE a.branch_id=? AND ar.area_id<>? AND ar.user_id<>? AND ar.role='PRINCIPAL' AND ar.valid_to IS NULL LIMIT 1''',
            (p['branch_id'],p['area_id'],users['MANAGER']['id'])).fetchone()
        organization.change_placement(c,users['WORKER']['id'],p['company_id'],p['branch_id'],target['area_id'],target['user_id'],core.now().date(),users['OP_RRHH']['id'])
    assert m.get(f'/historial-solicitudes/{rid}').status_code==200 and m.get(f'/boleta/{rid}.pdf').status_code==200
    assert m.get(f"/mi-equipo/{users['WORKER']['id']}").status_code==404
    rid2=submit(w)
    assert m.get(f'/historial-solicitudes/{rid2}').status_code==403
    assert decide(m,rid2).status_code==403


def test_unknown_hr_function_has_no_global_capabilities(local_copy,users):
    with core.db() as c:
        c.execute("UPDATE organization_authority_history SET authority='PENDIENTE' WHERE user_id=?",(users['OP_RRHH']['id'],))
        assert 'hr.global' not in access_control.capabilities(c,users['OP_RRHH'])


@pytest.mark.parametrize('url',['/importar','/importar/preview','/importar/confirmar','/importar/biometria','/reportes/cerrar','/reportes/reabrir','/conciliacion/resolver','/boletas/masivo.pdf'])
def test_technical_admin_read_access_never_grants_labor_post(login,url):
    client=login('ADMIN_TEC')
    assert client.post(url,data={'csrf':csrf(client)}).status_code==403
    assert not BeautifulSoup(client.get('/importar').text,'html.parser').select('form[action="/importar/preview"]')


def test_technical_attendance_never_advertises_forbidden_request(login):
    c=login('ADMIN_TEC')
    assert not BeautifulSoup(c.get('/asistencia').text,'html.parser').select('a[href="/solicitar"]')
