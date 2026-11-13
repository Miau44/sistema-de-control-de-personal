"""Presentation regressions; no changes to attendance or approval rules."""
from ui_presenters import attendance_overview
from test_function_capabilities import prepared,local_copy,users,login


def test_pending_information_grouping_preserves_every_journey():
    rows=[{'operational_result':'INFORMACIÓN INSUFICIENTE','reason':'Falta configuración',
           'user_id':i%50,'work_date':'2026-10-12','justification_status':'PENDIENTE DE REVISIÓN'} for i in range(424)]
    result=attendance_overview(rows)
    assert len(rows)==424 and result['pending_information']==424
    assert len(result['groups'])==1 and result['groups'][0]['people']==50
    assert not result['attention'] and len(result['groups'][0]['sample'])==20


def test_control_has_single_primary_import_and_keeps_advanced_actions(login):
    from bs4 import BeautifulSoup
    client=login('OP_RRHH');response=client.get('/conciliacion')
    soup=BeautifulSoup(response.text,'html.parser')
    assert soup.h1.text=='Control de asistencia'
    assert len(soup.select('form[action="/importar/preview"]'))==1
    assert soup.select_one('#herramientas form[action="/conciliacion/recalcular"]')
    assert soup.select_one('#herramientas form[action="/conciliacion/regimen"]')
    assert len(soup.select('.summary-five .metric'))==5
    assert '424' in soup.select_one('.summary-five').text
    assert 'información pendiente' in soup.text.lower()
    all_rows=BeautifulSoup(client.get('/conciliacion?vista=todas').text,'html.parser')
    assert len(all_rows.select('.attendance-list tbody tr'))==50


def test_same_route_and_permissions_with_new_navigation(login):
    from bs4 import BeautifulSoup
    for code in ('WORKER','MANAGER','ADM_OPER','ADMIN_TEC'):
        c=login(code);html=BeautifulSoup(c.get('/').text,'html.parser')
        assert html.select_one('form[action="/logout"]') and html.select_one('a[href="/mi-perfil"]')
        assert c.get('/control-asistencia').status_code==404
    assert login('ADM_OPER').get('/conciliacion').status_code==403


def test_bulk_document_forms_are_not_nested(login):
    from bs4 import BeautifulSoup
    soup=BeautifulSoup(login('OP_RRHH').get('/boletas').text,'html.parser')
    assert not soup.select('form form')


def test_boleta_pagination_keeps_selection_forms_independent(login,users):
    from bs4 import BeautifulSoup
    import core
    from test_organizational_authority import submit,decide
    with core.db() as c:
        worker=users['WORKER'];manager=core.choose_approver(c,worker,'manager')
        for _ in range(26):
            r=submit(c,worker);r=decide(c,r,manager);decide(c,r,users['OP_RRHH'])
    client=login('OP_RRHH')
    first=BeautifulSoup(client.get('/boletas').text,'html.parser');second=BeautifulSoup(client.get('/boletas?page=2').text,'html.parser')
    assert len(first.select('.row-check'))==25 and len(second.select('.row-check'))==1
    assert not first.select('form form') and not second.select('form form')


def test_audit_can_reach_older_records_without_expanding_access(login):
    import core
    with core.db() as c:
        for i in range(210):core.audit(c,None,'UI_CHECK','fixture',i,'Pagination proof')
    client=login('ADMIN_TEC')
    assert 'Pagination proof' in client.get('/auditoria?q=Pagination%20proof&pagina=5').text
    assert login('ADM_OPER').get('/auditoria?q=Pagination%20proof&pagina=5').status_code==403
