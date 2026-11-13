"""Focused regression checks for the shared gates implemented so far."""
import json
from datetime import date
import core
from app import approved_permit
from attendance_engine import evaluate
from test_attendance_accuracy import configured,request


def test_document_gate_requires_effective_history(configured):
    with core.db() as c:
        invalid=request(c,configured,'2026-10-12 08:00:00','2026-10-12 18:00:00',valid=False)
        valid=request(c,configured,'2026-10-13 08:00:00','2026-10-13 18:00:00')
        assert not approved_permit(c,c.execute('SELECT * FROM requests WHERE id=?',(invalid,)).fetchone())
        assert approved_permit(c,c.execute('SELECT * FROM requests WHERE id=?',(valid,)).fetchone())


def test_textual_eligibility_does_not_authorize(configured):
    person,_,hr=configured
    with core.db() as c:
        c.execute("INSERT INTO attendance_calendar_events(title,event_type,date_from,date_to,eligibility,approved_by,created_by,created_at) VALUES('TEST selective','ASUETO','2026-10-12','2026-10-12','Unverified condition',?,?,?)",(hr['id'],hr['id'],core.nowstr()))
        row=evaluate(c,person,date(2026,10,12))
        assert row['operational_result']!='AUSENCIA JUSTIFICADA'
        assert json.loads(row['detail_json'])['calendar_pending_eligibility']


def test_daily_query_uses_central_result(configured):
    person,_,_=configured
    with core.db() as c:
        assert core.daily_status(c,person,date(2026,10,12))['estado']==evaluate(c,person,date(2026,10,12))['operational_result']
