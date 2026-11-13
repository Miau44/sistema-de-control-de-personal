"""Focused checks for real approval state and temporal coverage; isolated temporary DBs."""
import json
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
import core
from legacy_fixture import seed_legacy_fixture
from reconciliation import _evaluate_day
from app import app,create_approval_steps
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup


@pytest.fixture
def configured(tmp_path,monkeypatch):
    monkeypatch.setattr(core,'DB_PATH',tmp_path/'accuracy.sqlite3');seed_legacy_fixture()
    with core.db() as c:
        person=c.execute("SELECT * FROM users WHERE employee_code='EMP001'").fetchone()
        manager=c.execute("SELECT * FROM users WHERE role='manager' ORDER BY id LIMIT 1").fetchone()
        hr=c.execute("SELECT * FROM users WHERE role='hr' ORDER BY id LIMIT 1").fetchone()
        branch=c.execute("SELECT * FROM branches WHERE name='Sede Central'").fetchone()
        c.execute("UPDATE employee_placements SET company_id=?,branch_id=?,supervisor_id=?,valid_from='2020-01-01',classification_status='CLASIFICADA' WHERE user_id=? AND valid_to IS NULL",
            (branch['company_id'],branch['id'],manager['id'],person['id']))
        c.execute("INSERT INTO attendance_employment_periods(user_id,valid_from,confirmed,note,assigned_by,created_at) VALUES(?,'2020-01-01',1,'TEST confirmed employment',?,?)",(person['id'],hr['id'],core.nowstr()))
        c.execute("INSERT INTO attendance_regime_assignments(user_id,regime_code,weekdays_json,shift_start,shift_end,valid_from,confirmed,note,assigned_by,created_at) VALUES(?,'BRANCH_STANDARD','[0,1,2,3,4,5]','08:00','18:00','2020-01-01',1,'TEST confirmed schedule',?,?)",(person['id'],hr['id'],core.nowstr()))
        c.execute("INSERT INTO import_batches(filename,imported_at,coverage_start,coverage_end,coverage_reviewed) VALUES('TEST coverage',?,'2026-10-01','2026-10-31',1)",(core.nowstr(),))
    return dict(person),dict(manager),dict(hr)


def request(c,people,start,end,status='APROBADA',kind='PERMISO',valid=True):
    person,manager,hr=people
    rid=c.execute('''INSERT INTO requests(user_id,approver_id,kind,leave_type,starts_at,ends_at,correction_at,correction_kind,
        justification,submitted_at,status) VALUES(?,?,?,?,?,?,?,?,?,'2026-10-01 00:00:00',?)''',
        (person['id'],manager['id'],kind,'PERSONAL',start if kind=='PERMISO' else '',end if kind=='PERMISO' else '',
         start if kind=='CORRECCION' else '','IN' if kind=='CORRECCION' else '','TEST authorization fixture',status)).lastrowid
    c.execute('UPDATE requests SET folio=? WHERE id=?',(f'TEST-{rid}',rid));create_approval_steps(c,rid,kind,manager['id'])
    if status=='APROBADA' and valid:
        from approval_authority import record_decision
        r=c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()
        for role,actor,stamp in [('manager',manager,'2026-10-02 10:00:00'),('hr',hr,'2026-10-02 11:00:00')]:
            step=c.execute('SELECT * FROM request_approval_steps WHERE request_id=? AND assignee_role=?',(rid,role)).fetchone()
            record_decision(c,actor,r,step,'APROBADA',stamp)
            c.execute("UPDATE request_approval_steps SET assignee_id=?,decision='APROBADA',decided_at=? WHERE id=?",(actor['id'],stamp,step['id']))
        c.execute("UPDATE requests SET decided_by=?,decided_at='2026-10-02 11:00:00' WHERE id=?",(hr['id'],rid))
    return rid


def test_regularization_requires_effective_final_approval(configured):
    person,_,_=configured
    for status,valid in [('PENDIENTE',False),('RECHAZADA',False),('APROBADA',False)]:
        with core.db() as c:
            request(c,configured,'2026-10-12 08:00:00',None,status,'CORRECCION',valid)
            r=_evaluate_day(c,person,date(2026,10,12));d=json.loads(r['detail_json'])
            assert not any(x['authorization']['valid'] for x in d['correction_requests'])
            assert r['justification_status']!='REGULARIZACIÓN APROBADA' and r['punch_count']==0
    with core.db() as c:
        rid=request(c,configured,'2026-10-12 08:00:00',None,kind='CORRECCION')
        r=_evaluate_day(c,person,date(2026,10,12));d=json.loads(r['detail_json'])
        assert r['justification_status']=='REGULARIZACIÓN APROBADA'
        assert any(e.get('request_id')==rid and e['provenance']=='C_APPROVED_REGULARIZATION' for e in d['events'])


def test_full_partial_and_administrative_hours_are_not_punches(configured):
    person,_,hr=configured
    with core.db() as c:
        request(c,configured,'2026-10-12 08:00:00','2026-10-12 18:00:00')
        request(c,configured,'2026-10-13 08:00:00','2026-10-13 12:00:00')
        c.execute("INSERT INTO attendance_presentation_policies(user_id,valid_from,start_time,end_time,note,approved_by) VALUES(?,'2026-10-01','08:00','18:00','TEST approved representation',?)",(person['id'],hr['id']))
        full=_evaluate_day(c,person,date(2026,10,12));part=_evaluate_day(c,person,date(2026,10,13))
        assert full['operational_result']=='AUSENCIA JUSTIFICADA'
        assert part['operational_result']=='COBERTURA PARCIAL'
        detail=json.loads(full['detail_json'])
        assert len(detail['administrative_references'])==2 and detail['original_punch_count']==0 and full['punch_count']==0
        assert json.loads(part['detail_json'])['uncovered_intervals']==[['2026-10-13 12:00:00','2026-10-13 18:00:00']]
        c.execute('UPDATE attendance_regime_assignments SET shift_start=NULL,shift_end=NULL WHERE user_id=?',(person['id'],))
        assert _evaluate_day(c,person,date(2026,10,12))['operational_result']=='INFORMACIÓN INSUFICIENTE'


def test_multiday_and_split_work_segments(configured):
    person,_,hr=configured
    with core.db() as c:
        request(c,configured,'2026-10-11 12:00:00','2026-10-14 12:00:00')
        assert _evaluate_day(c,person,date(2026,10,12))['operational_result']=='AUSENCIA JUSTIFICADA'
        assert _evaluate_day(c,person,date(2026,10,14))['operational_result']=='COBERTURA PARCIAL'
        for start,end in [('08:00','12:00'),('13:00','18:00')]:
            c.execute("INSERT INTO attendance_journey_segments(user_id,weekday,valid_from,start_time,end_time,confirmed,note,approved_by) VALUES(?,3,'2020-01-01',?,?,1,'TEST break excluded',?)",(person['id'],start,end,hr['id']))
        request(c,configured,'2026-10-15 08:00:00','2026-10-15 12:00:00')
        request(c,configured,'2026-10-15 13:00:00','2026-10-15 18:00:00')
        assert _evaluate_day(c,person,date(2026,10,15))['operational_result']=='AUSENCIA JUSTIFICADA'


def test_overnight_workday_owns_after_midnight_event(configured):
    person,_,_=configured
    with core.db() as c:
        c.execute("UPDATE attendance_regime_assignments SET shift_start='22:00',shift_end='06:00' WHERE user_id=?",(person['id'],))
        request(c,configured,'2026-10-12 22:00:00','2026-10-13 06:00:00')
        assert _evaluate_day(c,person,date(2026,10,12))['operational_result']=='AUSENCIA JUSTIFICADA'
        c.execute("INSERT INTO punches(user_id,occurred_at,kind,source,source_key,created_at) VALUES(?,'2026-10-13 00:15:00','UNSPEC','TEST event','TEST-night',?)",(person['id'],core.nowstr()))
        assert _evaluate_day(c,person,date(2026,10,12))['punch_count']==1
        assert _evaluate_day(c,person,date(2026,10,13))['punch_count']==0


def test_real_approval_updates_only_affected_day_without_reimport(configured):
    person,manager,hr=configured
    with core.db() as c:rid=request(c,configured,'2026-10-12 08:00:00','2026-10-12 18:00:00','PENDIENTE')
    with core.db() as c:
        previous=c.execute('SELECT version_id FROM attendance_daily_current WHERE user_id=? AND work_date=?',(person['id'],'2026-10-12')).fetchone()[0]
        originals=c.execute('SELECT COUNT(*) FROM punches').fetchone()[0]
    for actor in [manager,hr]:
        with TestClient(app) as client:
            token=BeautifulSoup(client.get('/login').text,'html.parser').select_one('input[name=csrf]')['value']
            client.post('/login',data={'csrf':token,'email':actor['email'],'password':'Demo2026!#'})
            token=BeautifulSoup(client.get('/mi-perfil').text,'html.parser').select_one('input[name=csrf]')['value']
            assert client.post(f'/aprobaciones/{rid}/resolver',data={'csrf':token,'decision':'APROBADA','note':'TEST approval'}).status_code==200
    with core.db() as c:
        current=c.execute('''SELECT v.*,d.operational_result FROM attendance_daily_current a JOIN attendance_daily_versions v ON v.id=a.version_id
            JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id WHERE a.user_id=? AND a.work_date='2026-10-12' ''',(person['id'],)).fetchone()
        assert current['id']!=previous and current['request_id']==rid and current['operational_result']=='AUSENCIA JUSTIFICADA'
        assert c.execute('SELECT COUNT(*) FROM punches').fetchone()[0]==originals


def test_new_authorization_in_closed_period_does_not_replace_official_snapshot(configured):
    person,_,hr=configured
    frozen='{"test":"official snapshot remains frozen"}'
    with core.db() as c:
        c.execute("INSERT INTO monthly_closures(period_start,period_end,state,closed_at,closed_by,snapshot_json) VALUES('2026-09-26','2026-10-25','CERRADO',?,?,?)",
            (core.nowstr(),hr['id'],frozen))
        request(c,configured,'2026-10-12 08:00:00','2026-10-12 18:00:00')
    with core.db() as c:
        assert c.execute("SELECT snapshot_json FROM monthly_closures WHERE period_end='2026-10-25'").fetchone()[0]==frozen
        current=c.execute('''SELECT v.closed_period_analysis,d.operational_result FROM attendance_daily_current a
            JOIN attendance_daily_versions v ON v.id=a.version_id JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id
            WHERE a.user_id=? AND a.work_date='2026-10-12' ''',(person['id'],)).fetchone()
        assert current['closed_period_analysis']==1 and current['operational_result']=='AUSENCIA JUSTIFICADA'
