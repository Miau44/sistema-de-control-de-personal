"""Read-only presentation and HTTP directory regressions on disposable databases."""
import pytest
from bs4 import BeautifulSoup
import core
from personnel_presenter import describe
from test_function_capabilities import prepared,local_copy,users,login


def soup(response):
    assert response.status_code==200
    return BeautifulSoup(response.text,'html.parser')


def test_directory_search_finds_third_page_and_keeps_configuration_complete(login):
    client=login('OP_RRHH')
    with core.db() as c:
        person=c.execute('SELECT * FROM users ORDER BY name LIMIT 1 OFFSET 40').fetchone()
    first=soup(client.get('/equipo'))
    assert len(first.select('.personnel-table tbody tr'))==20
    assert person['employee_code'] not in first.select_one('.personnel-table').text
    found=soup(client.get('/equipo',params={'q':person['employee_code']}))
    assert person['employee_code'] in found.select_one('.personnel-table').text
    assert '1 resultados' in found.select_one('#personnel-search').text
    assert len(found.select('#schedule-target-id option[data-kind=user]'))==53
    assert not found.select_one('#personnel-search input[name=pagina]')
    assert found.select_one('.personnel-table').has_attr('data-server-paged')


def test_directory_filters_paging_and_name_query_intersect(login):
    client=login('OP_RRHH')
    with core.db() as c:
        person=c.execute('''SELECT u.*,p.branch_id FROM users u JOIN employee_placements p ON p.user_id=u.id
            WHERE p.valid_to IS NULL ORDER BY u.name LIMIT 1 OFFSET 40''').fetchone()
    params={'q':person['name'],'branch_id':person['branch_id'],'area_id':person['area_id']}
    filtered=soup(client.get('/equipo',params=params))
    assert person['employee_code'] in filtered.select_one('.personnel-table').text
    params['area_id']='99999'
    assert '0 resultados' in soup(client.get('/equipo',params=params)).text
    broad=soup(client.get('/equipo',params={'q':'','pagina':2}))
    following=broad.select_one('nav.pagination a:last-child')['href']
    assert 'q=' in following and 'branch_id=' in following and 'area_id=' in following and 'pagina=3' in following
    assert 'Página 3 de 3' in soup(client.get('/equipo?pagina=3')).text


def test_directory_labor_pending_counts_exclude_auxiliary(login):
    client=login('OP_RRHH');page=soup(client.get('/equipo'))
    note=page.select_one('.inside-note').text
    for fragment in ('50 cuentas laborales','15 encargados provisionales','4 cuentas operativas','50 trabajadores requieren'):
        assert fragment in note
    auxiliary=soup(client.get('/equipo?q=ADMIN_TEC')).select_one('.personnel-table tbody tr')
    assert auxiliary.select_one('.personnel-state').text=='No aplica'
    assert auxiliary.select_one('.role-label').text=='Administrador técnico'
    worker=soup(client.get('/equipo?q=11070630')).select_one('.personnel-table tbody tr')
    assert worker.select_one('.personnel-state').text=='Asignación provisional'


def test_presentation_does_not_invent_confirmations():
    rows=[{'id':n,'role':'employee','classification_status':'CLASIFICADA'} for n in range(1,5)]
    profiles={1:{'person_id':'A','provisional':0},2:{'person_id':'B','provisional':1},
              3:{'person_id':'C','source_json':'{"administrative_confirmation":false}'},
              4:{'person_id':'D','source_json':'{"administrative_confirmation":true}'}}
    result,counts=describe(rows,profiles,{3,4})
    assert [r['display_state'] for r in result]==['Pendiente de validación','Asignación provisional','Pendiente de validación','Confirmado']
    assert counts['pending']==3


@pytest.mark.parametrize('actor',['WORKER','MANAGER','ADM_OPER','ADMIN_TEC'])
def test_global_directory_search_keeps_backend_guard(login,actor):
    assert login(actor).get('/equipo?q=ADMIN_TEC&pagina=3').status_code==403


@pytest.mark.parametrize('actor,superior',[('WORKER','MANAGER'),('MANAGER','OP_RRHH'),('OP_RRHH','RESP_RRHH'),('RESP_RRHH','ADM_OPER')])
def test_request_help_uses_actual_authority(login,users,actor,superior):
    page=soup(login(actor).get('/solicitar'));help=page.select_one('.side-info').text
    assert users[superior]['name'] in help
    assert ('continúa a Recursos Humanos' in help)==(actor=='WORKER')
    assert 'reapertura autorizada' in help
    assert 'Boleta de Permiso' in page.select_one('#submission-outcome').text
    assert 'La marcación original no será modificada.' in str(page)
    assert not page.select_one('[name=starts_at]').get('value')


def test_control_keeps_filters_and_explains_actionable_vs_pending(login):
    page=soup(login('OP_RRHH').get('/conciliacion'))
    assert page.select_one('.summary-five').sourceline<page.select_one('.query-controls').sourceline
    for name in ('desde','hasta','empleado','vista'):
        assert page.select_one(f'.query-controls [name={name}]')
    assert not page.select_one('.query-controls').has_attr('open')
    assert len(page.select('[data-quick-range]'))==5
    assert 'Incidencias accionables' in page.text and 'Cero incidencias no certifica asistencia perfecta' in page.text
    assert page.select_one('.file-picker input[type=file][required]')
    assert page.select_one('[data-open-tools]')


def test_boleta_readability_keeps_selection_and_documents(login,users):
    from test_organizational_authority import submit,decide
    with core.db() as c:
        request=submit(c,users['WORKER']);request=decide(c,request,users['MANAGER']);decide(c,request,users['OP_RRHH'])
    client=login('OP_RRHH');page=soup(client.get('/boletas'))
    assert page.select_one('.pdf-link').text=='Ver PDF'
    assert page.select_one('.boleta-table a[title="Ver expediente"]')
    assert page.select_one('#select-filtered').has_attr('disabled')
    assert not soup(client.get('/boletas?kind=PERSONAL')).select_one('#select-filtered').has_attr('disabled')
    for selector in ('#select-page','#clear-selection','#selected-count','#sheet-count','.row-check'):
        assert page.select_one(selector)
    assert 'Math.ceil(n/2)' in str(page)
