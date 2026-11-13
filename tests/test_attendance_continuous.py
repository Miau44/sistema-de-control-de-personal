"""Continuous import acceptance checks. All writes use pytest temporary databases."""
import io
import json
from datetime import date,datetime
from pathlib import Path
import pytest
from openpyxl import Workbook
import core
from reconciliation import preview_zlink,import_zlink,reconcile,_evaluate_day
from attendance_continuous import process_import,current_evaluation,coverage
from test_attendance_accuracy import configured,request


def workbook(events):
    from reconciliation import EXPECTED
    wb=Workbook();ws=wb.active;ws.append(list(EXPECTED.values()))
    for event in events:
        row=dict(person_id='TEST-EMP',person_name='Test Employee',event_type='Normal',source='Device',
            device_sn='SN1',device_name='Test',uploaded_at='2026-11-10 18:00:00')
        row.update(event)
        ws.append([row.get(k,'') for k in EXPECTED])
    out=io.BytesIO();wb.save(out);return out.getvalue()


def link(c,people):
    person,_,hr=people
    c.execute("INSERT INTO biometric_persons(person_id,display_name,department,link_status,created_at,updated_at) VALUES('TEST-EMP','Test Employee','Test','PENDING',?,?)",(core.nowstr(),core.nowstr()))
    c.execute("INSERT INTO biometric_identity_links(person_id,user_id,valid_from,confirmed_by,confirmed_at,note) VALUES('TEST-EMP',?,'2020-01-01',?,?,'TEST verified')",(person['id'],hr['id'],core.nowstr()))


def upload(c,people,events,**kwargs):
    content=workbook(events);preview=preview_zlink(c,content)
    outcome=import_zlink(c,preview,content,people[2]['id'],'test.xlsx')
    return process_import(c,preview,outcome,people[2]['id'],**kwargs)


def test_repeat_overlap_and_late_events_are_incremental(configured):
    person,_,_=configured
    with core.db() as c:
        link(c,configured)
        first=upload(c,configured,[{'occurred_at':'2026-10-12 08:00:00'}],requested_to='2026-10-13')
        assert first['added']==1
        versions=c.execute('SELECT count(*) FROM attendance_daily_versions').fetchone()[0]
        duplicate=upload(c,configured,[{'occurred_at':'2026-10-12 08:00:00'}])
        assert duplicate['added']==0 and duplicate['recalculated_days']==0
        assert c.execute('SELECT count(*) FROM attendance_daily_versions').fetchone()[0]==versions
        # Upload timestamp is not event identity; preserve original uploaded_at.
        shifted=workbook([{'occurred_at':'2026-10-12 08:00:00','uploaded_at':'2026-11-11 20:00:00'}])
        p=preview_zlink(c,shifted)
        out=import_zlink(c,p,shifted,configured[2]['id'],'same-event.xlsx')
        assert out['added']==0
        accumulated=upload(c,configured,[{'occurred_at':'2026-10-12 08:00:00'},
            {'occurred_at':'2026-10-13 08:00:00'},{'occurred_at':'2026-10-13 18:00:00'}])
        assert accumulated['added']==2 and accumulated['duplicate']==1
        assert accumulated['recalculated_days']==1
        late=upload(c,configured,[{'occurred_at':'2026-10-12 18:00:00'}])
        assert late['recalculated_days']==1
        current=current_evaluation(c,person['id'],'2026-10-12')
        assert current['punch_count']==2
        full=reconcile(c,date(2026,10,12),date(2026,10,13),configured[2]['id'],user_ids=[person['id']])
        for row in full['rows']:
            saved=current_evaluation(c,person['id'],row['work_date'])
            assert all(saved[k]==row[k] for k in ('operational_result','punch_status','justification_status','punch_count'))


def test_distinct_same_minute_and_overnight_arrivals(configured):
    person,_,_=configured
    with core.db() as c:
        link(c,configured)
        c.execute("UPDATE attendance_regime_assignments SET shift_start='22:00',shift_end='06:00' WHERE user_id=?",(person['id'],))
    with core.db() as c:
        upload(c,configured,[],requested_from='2026-10-13',requested_to='2026-10-13')
    with core.db() as c:
        out=upload(c,configured,[{'occurred_at':'2026-10-13 00:15:01'},
            {'occurred_at':'2026-10-13 00:15:02'},{'occurred_at':'2026-10-13 00:15:02','source':'Manual'}])
        assert out['added']==3
        assert out['recalculated_days']==1
        assert current_evaluation(c,person['id'],'2026-10-12')['punch_count']==3
        assert _evaluate_day(c,person,date(2026,10,13))['punch_count']==0


def test_missing_device_and_confirmed_no_activity(configured):
    person,_,_=configured
    with core.db() as c:
        link(c,configured)
        data=[{'occurred_at':'2026-10-12 08:00:00'}]
        out=upload(c,configured,data,coverage_reviewed=True,expected_devices=['SN1','SN2'],
            requested_from='2026-10-12',requested_to='2026-10-13')
        assert out['coverage_pending'] and coverage(c,'2026-10-13')['device_states']['SN2']=='MISSING'
        assert current_evaluation(c,person['id'],'2026-10-13')['operational_result']!='POSIBLE INASISTENCIA'
        out=upload(c,configured,data,coverage_reviewed=True,expected_devices=['SN1','SN2'],no_event_devices=['SN2'],
            requested_from='2026-10-12',requested_to='2026-10-13',note='TEST verified idle device')
        assert not out['coverage_pending']
        assert current_evaluation(c,person['id'],'2026-10-13')['operational_result']=='POSIBLE INASISTENCIA'


def test_open_journey_and_unconfirmed_hours(configured,monkeypatch):
    person,_,_=configured
    monkeypatch.setattr(core,'now',lambda:datetime(2026,10,12,12,0))
    with core.db() as c:
        row=_evaluate_day(c,person,date(2026,10,12));e=json.loads(row['detail_json'])
        assert e['journey']['state']=='JORNADA ABIERTA'
        assert row['operational_result']!='POSIBLE INASISTENCIA'
        assert e['journey']['refresh_after']=='2026-10-12 18:00:00'
        c.execute("UPDATE attendance_regime_assignments SET shift_start=NULL,shift_end=NULL WHERE user_id=?",(person['id'],))
        row=_evaluate_day(c,person,date(2026,10,10))
        assert json.loads(row['detail_json'])['journey']['state']=='TÉRMINO DE JORNADA NO CONFIRMADO'
        assert row['operational_result']=='INFORMACIÓN INSUFICIENTE'


def test_regime_change_invalidates_and_report_uses_current(configured):
    person,_,_=configured
    with core.db() as c:
        reconcile(c,date(2026,10,12),date(2026,10,12),configured[2]['id'],user_ids=[person['id']])
        old=current_evaluation(c,person['id'],'2026-10-12')['version_id']
        c.execute("UPDATE attendance_regime_assignments SET regime_code='MOBILE_NO_FIXED_HOURS',submodality='ASSIGNMENTS',shift_start=NULL,shift_end=NULL WHERE user_id=?",(person['id'],))
    with core.db() as c:
        saved=current_evaluation(c,person['id'],'2026-10-12')
        assert saved['version_id']!=old and saved['operational_result']=='INFORMACIÓN INSUFICIENTE'
        assert saved['work_obligation']=='SIN ASIGNACIÓN DOCUMENTADA'
        from attendance_reporting import report_from_reconciliation
        report=report_from_reconciliation(c,date(2026,10,25))
        row=next(r for r in report['detalle'] if r['user_id']==person['id'] and r['fecha']=='2026-10-12')
        assert row['estado']==saved['operational_result']


def test_original_october_file_twice_on_temporary_database(configured):
    path=Path.home()/'Downloads'/'Biometrico(20261001-20261031).xlsx'
    if not path.is_file():pytest.skip('Real workbook unavailable')
    content=path.read_bytes()
    with core.db() as c:
        p=preview_zlink(c,content);out=import_zlink(c,p,content,configured[2]['id'],path.name)
        summary=process_import(c,p,out,configured[2]['id'])
        assert summary['added']==3567
        again=process_import(c,p,import_zlink(c,p,content,configured[2]['id'],path.name),configured[2]['id'])
        assert again['added']==0 and again['duplicate']==3567 and again['recalculated_days']==0
        assert c.execute('SELECT count(*) FROM biometric_events').fetchone()[0]==3567


def test_late_event_after_close_preserves_snapshot_and_resolution(configured):
    person,_,hr=configured
    frozen='{"official":"unchanged"}'
    with core.db() as c:
        link(c,configured)
        c.execute("INSERT INTO monthly_closures(period_start,period_end,state,closed_at,closed_by,snapshot_json) VALUES('2026-09-26','2026-10-25','CERRADO','2026-10-26 08:00:00',?,?)",(hr['id'],frozen))
        resolution=c.execute("INSERT INTO attendance_resolutions(user_id,work_date,resolution_type,reason,evidence,created_by,created_at) VALUES(?,'2026-10-12','FALTA_CONFIRMADA','TEST prior resolution','TEST evidence',?,?)",(person['id'],hr['id'],core.nowstr())).lastrowid
    with core.db() as c:
        out=upload(c,configured,[{'occurred_at':'2026-10-12 08:00:00'}])
        saved=current_evaluation(c,person['id'],'2026-10-12')
        assert saved['closed_period_analysis']==1 and out['closed_period_changes']>=1
        assert saved['operational_result']=='FALTA CONFIRMADA POR RR. HH.'
        assert 'requiere revisión' in saved['reason']
        assert c.execute('SELECT active FROM attendance_resolutions WHERE id=?',(resolution,)).fetchone()[0]==1
        assert c.execute("SELECT snapshot_json FROM monthly_closures WHERE period_end='2026-10-25'").fetchone()[0]==frozen


def test_empty_export_can_evaluate_absent_roster_but_needs_explicit_range(configured):
    person,_,_=configured
    with core.db() as c:
        result=upload(c,configured,[],requested_from='2026-10-12',requested_to='2026-10-12',
            coverage_reviewed=True,expected_devices=['SN1'],no_event_devices=['SN1'],note='TEST verified no activity')
        assert result['added']==0 and not result['coverage_pending']
        assert current_evaluation(c,person['id'],'2026-10-12')['operational_result']=='POSIBLE INASISTENCIA'
        assert c.execute('SELECT count(*) FROM biometric_events').fetchone()[0]==0
        from attendance_reporting import report_from_reconciliation
        report=core.report(c,date(2026,10,25))
        assert report['administrative_source']=='CONCILIACION'


def test_clock_boundary_refreshes_only_expired_journeys(configured,monkeypatch):
    person,_,hr=configured
    monkeypatch.setattr(core,'now',lambda:datetime(2026,10,12,12,0))
    with core.db() as c:
        reconcile(c,date(2026,10,12),date(2026,10,12),hr['id'],user_ids=[person['id']])
        old=current_evaluation(c,person['id'],'2026-10-12')['version_id']
    monkeypatch.setattr(core,'now',lambda:datetime(2026,10,12,19,0))
    with core.db() as c:
        current=current_evaluation(c,person['id'],'2026-10-12')
        assert current['version_id']!=old
        assert current['operational_result']=='POSIBLE INASISTENCIA'
        assert json.loads(current['detail_json'])['journey']['state']=='JORNADA FINALIZADA'


def test_rejection_updates_current_without_reimport(configured):
    person,_,hr=configured
    with core.db() as c:
        rid=request(c,configured,'2026-10-12 08:00:00','2026-10-12 18:00:00')
    with core.db() as c:
        assert current_evaluation(c,person['id'],'2026-10-12')['operational_result']=='AUSENCIA JUSTIFICADA'
        c.execute("UPDATE requests SET status='RECHAZADA' WHERE id=?",(rid,))
    with core.db() as c:
        saved=current_evaluation(c,person['id'],'2026-10-12')
        assert saved['justification_status']=='SOLICITUD RECHAZADA'
        assert saved['operational_result']=='POSIBLE INASISTENCIA'
        assert c.execute('SELECT count(*) FROM biometric_events').fetchone()[0]==0


def test_import_preview_confirm_http_flow(configured,tmp_path,monkeypatch):
    import app as application
    from fastapi.testclient import TestClient
    from bs4 import BeautifulSoup
    from test_system import sign_in,get_csrf
    monkeypatch.setattr(application,'RECONCILIATION_STAGING',tmp_path/'staging')
    with TestClient(application.app) as client:
        sign_in(client,configured[2]['email'])
        data=workbook([{'occurred_at':'2026-10-12 08:00:00'}])
        for attempt in range(2):
            csrf=get_csrf(client,'/importar')
            response=client.post('/importar/preview',data={'csrf':csrf},files={'file':('test.xlsx',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            assert response.status_code==200
            soup=BeautifulSoup(response.text,'html.parser')
            assert soup.select_one('input[name="requested_from"]')
            fields={k:soup.select_one('input[name="'+k+'"]')['value'] for k in ('csrf','token')}
            response=client.post('/importar/confirmar',data={**fields,'requested_from':'2026-10-12','requested_to':'2026-10-13'})
            assert response.status_code==200 and 'Procesamiento continuo' in response.text
        with core.db() as c:
            assert c.execute('SELECT count(*) FROM biometric_events').fetchone()[0]==1
            receipts=c.execute('SELECT summary_json FROM attendance_import_receipts ORDER BY id').fetchall()
            assert len(receipts)==2 and json.loads(receipts[-1][0])['recalculated_days']==0
