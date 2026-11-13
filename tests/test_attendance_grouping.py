"""Logical repeated fingerprints; all persistent operations use isolated pytest databases."""
import json
from datetime import date
import core
from attendance_grouping import group_events
from reconciliation import _evaluate_day,reconcile
from attendance_continuous import current_evaluation
from test_attendance_accuracy import configured,request
from test_attendance_continuous import link,upload


def physical(stamps):
    return [{'person_id':'TEST','device_sn':'SN1','occurred_at':s,'original_event_id':n,
        'provenance':'A_ZLINK_PHYSICAL','source':'Device'} for n,s in enumerate(stamps,1)]


def policy(c,people):
    c.execute("INSERT INTO attendance_movement_policies(user_id,valid_from,boundary_seconds,note,approved_by,created_at) VALUES(?,'2026-10-01',120,'TEST confirmed movement policy',?,?)",(people[0]['id'],people[2]['id'],core.nowstr()))


def test_fixed_anchor_no_chain_devices_and_manual_separate():
    events=physical(['2026-10-12 08:00:10','2026-10-12 08:00:15','2026-10-12 08:01:00','2026-10-12 08:01:11'])
    groups=group_events(events,'2026-10-12',{'id':1,'window_seconds':60})
    assert [g['original_count'] for g in groups]==[3,1]
    assert groups[0]['representative_at'] is None
    events[1]['device_sn']='SN2';events[2]['provenance']='B_ZLINK_MANUAL'
    assert len(group_events(events,'2026-10-12',{'id':1,'window_seconds':60}))==4
    assert len(group_events(physical(['2026-10-12 08:00:00','2026-10-12 08:01:00']), '2026-10-12',{'id':2,'window_seconds':30}))==2


def test_repeated_entry_missing_exit_then_late_group_and_idempotence(configured):
    person,_,hr=configured
    with core.db() as c:link(c,configured);policy(c,configured)
    entry=[{'occurred_at':'2026-10-12 '+s} for s in ('08:00:10','08:00:15','08:01:00')]
    with core.db() as c:
        upload(c,configured,entry)
        saved=current_evaluation(c,person['id'],'2026-10-12');d=json.loads(saved['detail_json'])
        assert saved['punch_count']==3 and saved['punch_status']=='REGISTRO ÚNICO'
        assert d['grouping']['effective_count']==1 and d['grouping']['groups'][0]['movement']=='ENTRADA'
        assert [m['movement'] for m in d['grouping']['missing_movements']]==['SALIDA']
        before=[tuple(r) for r in c.execute('SELECT * FROM biometric_events ORDER BY id')]
        repeat=upload(c,configured,entry);assert repeat['recalculated_days']==0
        assert before==[tuple(r) for r in c.execute('SELECT * FROM biometric_events ORDER BY id')]
        exits=[{'occurred_at':'2026-10-12 '+s} for s in ('18:00:05','18:00:09')]
        result=upload(c,configured,entry+exits);assert result['recalculated_days']==1
        saved=current_evaluation(c,person['id'],'2026-10-12');d=json.loads(saved['detail_json'])
        assert d['grouping']['effective_count']==2 and not d['grouping']['missing_movements']
        assert [g['representative_at'] for g in d['grouping']['groups']]==['2026-10-12 08:00:10','2026-10-12 18:00:09']
        full=reconcile(c,date(2026,10,12),date(2026,10,12),hr['id'],user_ids=[person['id']])
        assert json.loads(full['rows'][0]['detail_json'])['grouping']==d['grouping']


def test_split_journey_retains_lunch_and_mobile_never_infers(configured):
    person,_,hr=configured
    with core.db() as c:
        link(c,configured);policy(c,configured)
        for start,end in [('08:00','12:00'),('13:00','18:00')]:
            c.execute("INSERT INTO attendance_journey_segments(user_id,weekday,valid_from,start_time,end_time,confirmed,note,approved_by) VALUES(?,0,'2026-10-01',?,?,1,'TEST split',?)",(person['id'],start,end,hr['id']))
    with core.db() as c:
        upload(c,configured,[{'occurred_at':'2026-10-12 '+s} for s in ('08:00:00','08:00:05','12:00:00','13:00:00','18:00:00')])
        d=json.loads(_evaluate_day(c,person,date(2026,10,12))['detail_json'])
        assert [g['movement'] for g in d['grouping']['groups']]==['ENTRADA','SALIDA','ENTRADA','SALIDA']
        assert not d['grouping']['missing_movements']
        c.execute("UPDATE attendance_regime_assignments SET regime_code='MOBILE_NO_FIXED_HOURS',submodality='ASSIGNMENTS' WHERE user_id=?",(person['id'],))
        d=json.loads(_evaluate_day(c,person,date(2026,10,12))['detail_json'])
        assert not d['grouping']['classification_configured'] and not d['grouping']['missing_movements']
        assert all(g['movement']=='MOVIMIENTO POR IDENTIFICAR' for g in d['grouping']['groups'])


def test_night_group_owns_late_seconds_after_shift_end(configured):
    person,_,_=configured
    with core.db() as c:
        link(c,configured);policy(c,configured)
        c.execute("UPDATE attendance_regime_assignments SET shift_start='22:00',shift_end='06:00' WHERE user_id=?",(person['id'],))
    with core.db() as c:
        upload(c,configured,[{'occurred_at':s} for s in ('2026-10-12 22:00:01','2026-10-12 22:00:03','2026-10-13 06:00:05','2026-10-13 06:00:09')])
        d=json.loads(_evaluate_day(c,person,date(2026,10,12))['detail_json'])
        assert d['grouping']['effective_count']==2 and not d['grouping']['missing_movements']
        assert d['grouping']['groups'][1]['representative_at']=='2026-10-13 06:00:09'
        assert _evaluate_day(c,person,date(2026,10,13))['punch_count']==0


def test_authorization_can_cover_missing_exit_and_closed_snapshot_stays(configured):
    person,_,hr=configured
    with core.db() as c:
        link(c,configured);policy(c,configured)
        c.execute("INSERT INTO monthly_closures(period_start,period_end,state,closed_at,closed_by,snapshot_json) VALUES('2026-09-26','2026-10-25','CERRADO',?,?,'{}')",(core.nowstr(),hr['id']))
    with core.db() as c:
        upload(c,configured,[{'occurred_at':'2026-10-12 08:00:10'},{'occurred_at':'2026-10-12 08:00:15'}])
        request(c,configured,'2026-10-12 12:00:00','2026-10-12 18:00:00')
    with core.db() as c:
        d=json.loads(current_evaluation(c,person['id'],'2026-10-12')['detail_json'])
        assert not d['grouping']['missing_movements']
        assert c.execute("SELECT snapshot_json FROM monthly_closures WHERE period_end='2026-10-25'").fetchone()[0]=='{}'
