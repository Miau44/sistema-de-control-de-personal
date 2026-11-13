"""Local UI review against a disposable DB; never starts a second operational environment."""
import os,sys,sqlite3,tempfile,threading,socket,json,time
from pathlib import Path
from contextlib import closing
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
ROOT=Path(__file__).resolve().parents[1]
stage=sys.argv[1] if len(sys.argv)>1 else 'before'
out=ROOT/'instance'/'ui_stage3b'/stage;out.mkdir(parents=True,exist_ok=True)
temp=tempfile.TemporaryDirectory();target=Path(temp.name)/'review.sqlite3'
with closing(sqlite3.connect(ROOT/'instance/kimtek.sqlite3')) as s,closing(sqlite3.connect(target)) as c:s.backup(c)
os.environ['ASISTENCIA_DB']=str(target);os.environ['AUTO_CLOSE']='0'
import core,uvicorn
from app import app
import app as app_module
app_module.RECONCILIATION_STAGING=Path(temp.name)/'staging'
password='Isolated-Visual-Review-2026!'
with core.db() as c:
    c.execute('UPDATE users SET password_hash=?',(core.hash_password(password),))
    worker=dict(c.execute("SELECT * FROM users WHERE role='employee' ORDER BY id LIMIT 1").fetchone())
    manager=dict(core.choose_approver(c,worker,'manager'))
    accounts={r['employee_code']:dict(r) for r in c.execute("SELECT * FROM users WHERE employee_code IN ('OP_RRHH','ADM_OPER','ADMIN_TEC')")}
accounts.update(worker=worker,manager=manager)
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]});thread.start()
while not server.started:time.sleep(.1)
from playwright.sync_api import sync_playwright
results=[];errors=[]
try:
 with sync_playwright() as p:
    browser=p.chromium.launch()
    routes={'OP_RRHH':['/conciliacion','/importar','/equipo','/aprobaciones','/reportes','/boletas'],
            'worker':['/','/asistencia','/solicitar','/mis-solicitudes'],
            'manager':['/mi-equipo','/aprobaciones','/mis-solicitudes'],
            'ADM_OPER':['/','/aprobaciones'],'ADMIN_TEC':['/','/auditoria']}
    contexts={}
    for code,account in accounts.items():
        context=browser.new_context(locale="es-BO",timezone_id="America/La_Paz");contexts[code]=context;page=context.new_page()
        page.goto(f'http://127.0.0.1:{port}/login');page.locator('[name=email]').fill(account['email']);page.locator('[name=password]').fill(password)
        page.locator('button[type=submit],button.btn-primary').first.click();page.wait_for_url(f'http://127.0.0.1:{port}/')
    # Real services, temporary requests only: one complete and one pending scenario.
    w=contexts['worker'].pages[0];w.goto(f'http://127.0.0.1:{port}/mi-perfil');token=w.locator('[name=csrf]').first.input_value()
    for _ in range(3):
        w.request.post(f'http://127.0.0.1:{port}/solicitar',form={'csrf':token,'kind':'PERMISO','leave_type':'MEDICO','starts_at':'2026-11-18T09:00','ends_at':'2026-11-18T12:00','justification':'Escenario temporal exclusivo para inspeccion de interfaz.'})
    with core.db() as c:rid=c.execute('SELECT MAX(id)-2 FROM requests').fetchone()[0]
    for code in ('manager','OP_RRHH'):
        page=contexts[code].pages[0];page.goto(f'http://127.0.0.1:{port}/mi-perfil');csrf=page.locator('[name=csrf]').first.input_value()
        page.request.post(f'http://127.0.0.1:{port}/aprobaciones/{rid}/resolver',form={'csrf':csrf,'decision':'APROBADA','note':'Revision aislada'})
    page=contexts['manager'].pages[0];page.goto(f'http://127.0.0.1:{port}/mi-perfil')
    page.request.post(f'http://127.0.0.1:{port}/aprobaciones/{rid+1}/resolver',form={'csrf':page.locator('[name=csrf]').first.input_value(),'decision':'APROBADA','note':'Revision aislada'})
    for code,paths in routes.items():
        page=contexts[code].pages[0];page.on('pageerror',lambda e:errors.append(str(e)))
        for width,height in [(1366,900),(768,1024),(390,844)]:
            page.set_viewport_size({'width':width,'height':height})
            for route in paths:
                response=page.goto(f'http://127.0.0.1:{port}'+route);assert response.status==200,(code,route,response.status);page.wait_for_timeout(100)
                name=f'{code}_{route.strip("/").replace("/","_") or "inicio"}_{width}.png'
                page.screenshot(path=str(out/name),full_page=True)
                results.append({'role':code,'route':route,'width':width,'status':response.status,'height':page.evaluate('document.documentElement.scrollHeight'),
                    'overflow':page.evaluate('document.documentElement.scrollWidth>innerWidth+2'),'controls':page.locator('main button:visible,main input:visible,main select:visible').count()})
    page=contexts['OP_RRHH'].pages[0];page.goto(f'http://127.0.0.1:{port}/importar');page.locator('[name=file]').set_input_files(str(Path.home()/'Downloads/Biometrico(20261001-20261031).xlsx'))
    page.locator('form[action="/importar/preview"] button').click();page.screenshot(path=str(out/'import_preview.png'),full_page=True)
    page.goto(f'http://127.0.0.1:{port}/conciliacion/detalle?user_id={worker["id"]}&fecha=2026-10-12');page.screenshot(path=str(out/'daily_detail.png'),full_page=True)
    if stage=='after':
        # Exercise the daily path on the same isolated copy, with the existing October file.
        page=contexts['OP_RRHH'].pages[0];page.goto(f'http://127.0.0.1:{port}/conciliacion')
        page.locator('form[action="/importar/preview"] [name=file]').set_input_files(str(Path.home()/'Downloads/Biometrico(20261001-20261031).xlsx'))
        page.locator('form[action="/importar/preview"] button').click()
        page.locator('form[action="/importar/confirmar"] button').click()
        assert page.url.startswith(f'http://127.0.0.1:{port}/conciliacion?')
        with core.db() as c:
            assert c.execute('SELECT COUNT(*) FROM biometric_events').fetchone()[0]==3567
            summary=json.loads(c.execute('SELECT summary_json FROM attendance_import_receipts ORDER BY id DESC LIMIT 1').fetchone()[0])
            assert summary['added']==0 and summary['duplicate']==3567 and summary['recalculated_days']==0,summary
        (out/'repeat_import.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
        page.screenshot(path=str(out/'reimport_result.png'),full_page=True)
        w=contexts['worker'].pages[0];w.goto(f'http://127.0.0.1:{port}/solicitar')
        w.locator('[data-vacation]').check();assert w.locator('[name=leave_type]').input_value()=='VACACIONES'
        assert w.locator('[name=starts_at]').input_value()==''
        w.screenshot(path=str(out/'vacation.png'),full_page=True)
        w.locator('[name=kind][value=CORRECCION]').check();assert w.locator('#leave-fields').is_hidden()
        w.screenshot(path=str(out/'regularization.png'),full_page=True)
        w.set_viewport_size({'width':390,'height':844});w.locator('.mobile-nav-toggle').click()
        assert w.locator('#primary-navigation').is_visible()
        w.screenshot(path=str(out/'mobile_navigation.png'),full_page=True)
    if stage=='after_b1':
        from urllib.parse import urlparse,parse_qs
        checks={}
        w=contexts['worker'].pages[0];w.set_viewport_size({'width':390,'height':844})
        w.goto(f'http://127.0.0.1:{port}/solicitar')
        assert w.locator('#submission-outcome').inner_text().endswith('Boleta de Permiso.')
        assert w.locator('.sidebar').bounding_box()['height']<110
        assert w.locator('#mobile-session').is_hidden() and w.locator('#mobile-identity').is_hidden()
        assert w.locator('.topbar').is_hidden()
        w.screenshot(path=str(out/'mobile_menu_closed.png'),full_page=True)
        checks['collapsed_header_height']=w.locator('.sidebar').bounding_box()['height']
        for width,height in [(1366,900),(768,1024),(390,844)]:
            w.set_viewport_size({'width':width,'height':height})
            w.locator('[name=kind]').first.check()
            w.locator('[name=starts_at]').fill('2026-11-19T09:15')
            assert w.locator('[name=starts_at]').input_value()=='2026-11-19T09:15'
            assert '19/11/2026 09:15' in w.locator('[name=starts_at] + .localized-date').inner_text()
            w.screenshot(path=str(out/f'permission_{width}.png'),full_page=True)
            w.locator('[data-vacation]').check()
            assert 'vacaciones' in w.locator('#submission-outcome').inner_text()
            assert w.locator('[name=starts_at]').input_value()=='2026-11-19T09:15'
            w.screenshot(path=str(out/f'vacation_{width}.png'),full_page=True)
            w.locator('[name=kind][value=CORRECCION]').check()
            message=w.locator('#submission-outcome').inner_text()
            assert 'Boleta' not in message and 'PDF' not in message and 'La marcación original no será modificada.' in message
            assert w.locator('#leave-fields').is_hidden()
            assert not w.evaluate('document.documentElement.scrollWidth>innerWidth+2')
            w.screenshot(path=str(out/f'regularization_{width}.png'),full_page=True)
        checks['live_help_and_iso_values']=True
        for code in ['worker','manager','OP_RRHH','ADM_OPER','ADMIN_TEC']:
            page=contexts[code].pages[0];page.set_viewport_size({'width':390,'height':844})
            page.goto(f'http://127.0.0.1:{port}/solicitar' if code=='worker' else f'http://127.0.0.1:{port}/')
            menu=page.locator('.mobile-nav-toggle');menu.click()
            assert menu.get_attribute('aria-expanded')=='true'
            assert page.locator('#mobile-session a[href="/mi-perfil"]').is_visible()
            assert page.locator('#mobile-session button').is_visible() and page.locator('#mobile-identity').is_visible()
            page.screenshot(path=str(out/f'{code}_mobile_menu_open.png'),full_page=True)
            page.keyboard.press('Escape')
            assert menu.get_attribute('aria-expanded')=='false' and menu.evaluate('(e)=>e===document.activeElement')
            menu.focus();page.keyboard.press('Enter');assert page.locator('#primary-navigation').is_visible()
            page.keyboard.press('Escape')
        page=contexts['OP_RRHH'].pages[0];page.set_viewport_size({'width':1366,'height':900})
        with core.db() as c:
            person=dict(c.execute('SELECT * FROM users ORDER BY name LIMIT 1 OFFSET 40').fetchone())
        page.goto(f'http://127.0.0.1:{port}/equipo?pagina=3')
        page.locator('#personnel-search [name=q]').fill(person['employee_code'])
        page.locator('#personnel-search button').click()
        assert 'pagina' not in parse_qs(urlparse(page.url).query)
        assert page.locator('.personnel-table tbody tr').count()==1
        assert person['name'] in page.locator('.personnel-table').inner_text()
        page.screenshot(path=str(out/'global_personnel_search.png'),full_page=True)
        checks['search_third_page_resets_first']=True
        page.goto(f'http://127.0.0.1:{port}/boletas')
        assert page.locator('#select-filtered').is_disabled()
        assert page.locator('#filtered-requirement').is_visible()
        count=page.locator('.row-check').count();assert count>0
        page.locator('#select-page').check()
        assert page.locator('#selected-count').inner_text()==str(count)
        assert page.locator('#sheet-count').inner_text()==str((count+1)//2)
        page.locator('#clear-selection').click();assert page.locator('#selected-count').inner_text()=='0'
        page.goto(f'http://127.0.0.1:{port}/boletas?kind=MEDICO')
        page.locator('#select-filtered').click();assert page.locator('#all-filtered').input_value()=='1'
        checks['pdf_selection_and_letter_sheet_counter']=True
        for selector in ['.boleta-table td','.personnel-state']:
            if page.locator(selector).count():assert page.locator(selector).first.evaluate('(e)=>getComputedStyle(e).overflowWrap')=='normal'
        page.set_viewport_size({'width':390,'height':844});page.goto(f'http://127.0.0.1:{port}/conciliacion')
        assert not page.locator('.query-controls').evaluate('(e)=>e.open')
        assert page.locator('.summary-five').bounding_box()['y']<page.locator('.query-controls').bounding_box()['y']
        page.locator('.query-controls > summary').click()
        page.locator('[data-date-preset]').select_option('yesterday')
        from datetime import date,timedelta
        today=page.locator('[data-date-filter]').get_attribute('data-today')
        expected=(date.fromisoformat(today)-timedelta(days=1)).isoformat()
        assert page.locator('[name=desde]').first.input_value()==expected
        assert page.locator('[name=hasta]').first.input_value()==expected
        assert '/'.join(reversed(expected.split('-'))) in page.locator('[name=desde] + .localized-date').first.inner_text()
        page.locator('.query-controls > summary').click()
        page.locator('.file-picker input').set_input_files(str(Path.home()/'Downloads/Biometrico(20261001-20261031).xlsx'))
        assert 'Biometrico' in page.locator('.selected-file').inner_text()
        page.locator('.file-picker input').focus();assert page.locator('.file-picker input').evaluate('(e)=>e===document.activeElement')
        page.screenshot(path=str(out/'control_mobile_selected_file.png'),full_page=True)
        checks['date_presets_spanish_file_picker_keyboard']=True
        page.locator('[data-quick-range=today]').click()
        assert parse_qs(urlparse(page.url).query)['desde']==[today]
        assert parse_qs(urlparse(page.url).query)['hasta']==[today]
        checks['quick_range_submission']=True
        assert not errors,errors
        assert not [r for r in results if r['overflow']], [r for r in results if r['overflow']]
        (out/'acceptance.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    browser.close()
finally:
 server.should_exit=True;thread.join(timeout=15)
 (out/'results.json').write_text(json.dumps({'pages':results,'javascript_errors':errors},indent=2),encoding='utf-8')
 temp.cleanup()
print(stage,len(results),'screens; JS errors',errors)
