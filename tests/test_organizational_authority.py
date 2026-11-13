"""Stage 3A.2: isolated copies, no requests or organization edits in the active database."""
import json
import sqlite3
from datetime import date
import pytest
import core
import organization as org
from approval_authority import load_plan,record_decision
from attendance_authorizations import authorization
from app import create_approval_steps,approved_permit,manager_can_access_request
from migrations import apply_migrations
from test_local_setup import prepared,local_copy


def people(c):
    employee=c.execute("SELECT u.* FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.role='employee' AND p.valid_to IS NULL ORDER BY u.id LIMIT 1").fetchone()
    return employee,{r['employee_code']:r for r in c.execute("SELECT * FROM users WHERE employee_code IN ('OP_RRHH','RESP_RRHH','ADM_OPER','ADMIN_TEC')")}


def submit(c,person):
    superior=core.choose_approver(c,person,'manager')
    rid=c.execute('''INSERT INTO requests(user_id,approver_id,kind,leave_type,starts_at,ends_at,justification,submitted_at)
        VALUES(?,?,'PERMISO','PERSONAL','2026-10-12 08:00:00','2026-10-12 18:00:00','Independent test authorization',?)''',
        (person['id'],superior['id'],core.nowstr())).lastrowid
    c.execute('UPDATE requests SET folio=? WHERE id=?',(f'ISOLATED-{rid}',rid))
    create_approval_steps(c,rid,'PERMISO',superior['id'])
    return c.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone()


def decide(c,r,actor,decision='APROBADA'):
    step=c.execute('SELECT * FROM request_approval_steps WHERE request_id=? AND decision IS NULL AND is_skipped=0 ORDER BY step_order LIMIT 1',(r['id'],)).fetchone()
    stamp=core.nowstr()
    record_decision(c,actor,r,step,decision,stamp)
    c.execute('UPDATE request_approval_steps SET assignee_id=?,decision=?,decided_at=? WHERE id=?',(actor['id'],decision,stamp,step['id']))
    if decision=='RECHAZADA' or not c.execute('SELECT 1 FROM request_approval_steps WHERE request_id=? AND decision IS NULL AND is_skipped=0',(r['id'],)).fetchone():
        c.execute('UPDATE requests SET status=?,decided_by=?,decided_at=? WHERE id=?',(decision,actor['id'],stamp,r['id']))
    return c.execute('SELECT * FROM requests WHERE id=?',(r['id'],)).fetchone()


@pytest.mark.parametrize('code,superior',[('OP_RRHH','RESP_RRHH'),('RESP_RRHH','ADM_OPER')])
def test_operational_hierarchy(local_copy,code,superior):
    with core.db() as c:
        _,accounts=people(c)
        assert core.choose_approver(c,accounts[code],'manager')['id']==accounts[superior]['id']


def test_worker_and_all_existing_managers_have_consistent_superiors(local_copy):
    with core.db() as c:
        e,a=people(c)
        p=org.placement(c,e['id'],core.now().date())
        assert core.choose_approver(c,e,'manager')['id']==p['supervisor_id']
        for m in c.execute("SELECT * FROM users WHERE role='manager'"):
            assert core.choose_approver(c,m,'manager')['id']==a['OP_RRHH']['id']
            assert org.placement(c,m['id'],core.now().date())['supervisor_id'] is None


@pytest.mark.parametrize('code',['ADMIN_TEC','ADM_OPER'])
def test_no_implicit_labor_authority(local_copy,code):
    with core.db() as c:
        e,a=people(c); r=submit(c,e)
        assert not core.may_approve(c,a[code],r)
        m=core.choose_approver(c,e,'manager'); r=decide(c,r,m)
        assert not core.may_approve(c,a[code],r)


@pytest.mark.parametrize('role',['employee','manager'])
def test_self_supervision_rejected(local_copy,role):
    with core.db() as c:
        u=c.execute('SELECT * FROM users WHERE role=? LIMIT 1',(role,)).fetchone()
        with pytest.raises(ValueError,match='autosupervisi'):
            org.validate_superior(c,u['id'],u['id'],core.now().date())


@pytest.mark.parametrize('length',[2,3])
def test_cycles_rejected_before_commit(local_copy,length):
    with core.db() as c:
        m=c.execute("SELECT * FROM users WHERE role='manager' LIMIT 1").fetchone();_,a=people(c)
        with org.atomic(c):
            target=a['OP_RRHH'] if length==2 else a['RESP_RRHH']
            c.execute('UPDATE organization_authority_history SET superior_id=? WHERE user_id=? AND valid_to IS NULL',(m['id'],target['id']))
            with pytest.raises(ValueError,match='ciclo'):
                org.validate_superior(c,m['id'],a['OP_RRHH']['id'],core.now().date())


@pytest.mark.parametrize('count',[2,3])
def test_same_day_changes_one_effective_row_with_audit(local_copy,count):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date()); managers=c.execute("SELECT u.* FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.role='manager' AND p.branch_id=? AND p.valid_to IS NULL",(p['branch_id'],)).fetchall()
        other=next(m for m in managers if m['id']!=p['supervisor_id'])
        org.assign_responsibles(c,p['area_id'],p['supervisor_id'],other['id'],core.now().date(),a['OP_RRHH']['id'])
        for i in range(count):
            org.change_placement(c,e['id'],p['company_id'],p['branch_id'],p['area_id'],other['id'] if i%2==0 else p['supervisor_id'],core.now().date(),a['OP_RRHH']['id'])
        assert c.execute('SELECT COUNT(*) FROM employee_placements WHERE user_id=? AND valid_from=?',(e['id'],str(core.now().date()))).fetchone()[0]==1
        assert not c.execute('SELECT 1 FROM employee_placements WHERE valid_to<valid_from').fetchone()
        assert c.execute("SELECT COUNT(*) FROM organization_revisions WHERE entity='employee_placements' AND effective_date=?",(str(core.now().date()),)).fetchone()[0]>=count


def test_replace_responsible_does_not_reassign_managers(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date())
        before=[tuple(r) for r in c.execute("SELECT p.* FROM employee_placements p JOIN users u ON u.id=p.user_id WHERE u.role='manager' ORDER BY p.id")]
        other=c.execute("SELECT u.id FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.role='manager' AND p.branch_id=? AND p.valid_to IS NULL AND u.id<>? LIMIT 1",(p['branch_id'],p['supervisor_id'])).fetchone()[0]
        org.assign_responsibles(c,p['area_id'],other,None,core.now().date(),a['OP_RRHH']['id'])
        assert before==[tuple(r) for r in c.execute("SELECT p.* FROM employee_placements p JOIN users u ON u.id=p.user_id WHERE u.role='manager' ORDER BY p.id")]
        assert org.placement(c,e['id'],core.now().date())['supervisor_id']==other


def test_atomic_error_rolls_back_all_organizational_changes(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date()); before=[tuple(r) for r in c.execute('SELECT * FROM area_responsibles ORDER BY id')]
        with pytest.raises(ValueError):
            with org.atomic(c):
                org.assign_responsibles(c,p['area_id'],p['supervisor_id'],None,core.now().date(),a['OP_RRHH']['id'])
                org.change_placement(c,e['id'],p['company_id'],p['branch_id'],p['area_id'],999999,core.now().date(),a['OP_RRHH']['id'])
        assert before==[tuple(r) for r in c.execute('SELECT * FROM area_responsibles ORDER BY id')]
        assert c.execute('SELECT COUNT(*) FROM organization_revisions').fetchone()[0]>=54 # preparation provenance only


def test_historical_authority_survives_role_and_superior_changes(local_copy):
    with core.db() as c:
        e,a=people(c);r=submit(c,e);m=core.choose_approver(c,e,'manager')
        r=decide(c,r,m);r=decide(c,r,a['OP_RRHH']);assert authorization(c,r)['valid']
        proof=c.execute('SELECT evidence_json FROM approval_authority_evidence ORDER BY step_id').fetchall()
        c.execute("UPDATE users SET role='employee' WHERE id=?",(m['id'],))
        c.execute("UPDATE users SET role='admin' WHERE id=?",(a['OP_RRHH']['id'],))
        c.execute('UPDATE local_staff_profiles SET superior_id=NULL WHERE user_id=?',(e['id'],))
        assert authorization(c,r)['valid'] and approved_permit(c,r)
        assert proof==c.execute('SELECT evidence_json FROM approval_authority_evidence ORDER BY step_id').fetchall()
        assert manager_can_access_request(c,m['id'],r['id'])


@pytest.mark.parametrize('code',['OP_RRHH','RESP_RRHH'])
def test_personal_operational_request_only_explicit_superior(local_copy,code):
    with core.db() as c:
        _,a=people(c);person=a[code];r=submit(c,person);s=core.choose_approver(c,person,'manager')
        assert not core.may_approve(c,person,r)
        assert core.may_approve(c,s,r)
        r=decide(c,r,s);assert authorization(c,r)['valid']
        assert load_plan(c,r['id'])['manager_skipped']


def test_manager_personal_request_no_self_approval(local_copy):
    with core.db() as c:
        _,a=people(c);m=c.execute("SELECT * FROM users WHERE role='manager' LIMIT 1").fetchone();r=submit(c,m)
        assert not core.may_approve(c,m,r)
        r=decide(c,r,a['OP_RRHH']);assert authorization(c,r)['valid']


@pytest.mark.parametrize('state',['PENDIENTE','RECHAZADA','APROBADA'])
def test_missing_history_never_justifies_or_issues_document(local_copy,state):
    with core.db() as c:
        e,_=people(c);r=submit(c,e)
        c.execute('UPDATE requests SET status=? WHERE id=?',(state,r['id']))
        r=c.execute('SELECT * FROM requests WHERE id=?',(r['id'],)).fetchone()
        assert not authorization(c,r)['valid'] and not approved_permit(c,r)


def test_request_assignment_and_evidence_immutable_after_same_day_replacement(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date());r=submit(c,e);plan=load_plan(c,r['id']);old=core.choose_approver(c,e,'manager')
        new=c.execute("SELECT u.id FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.role='manager' AND p.branch_id=? AND p.valid_to IS NULL AND u.id<>? LIMIT 1",(p['branch_id'],old['id'])).fetchone()[0]
        org.assign_responsibles(c,p['area_id'],new,None,core.now().date(),a['OP_RRHH']['id'])
        assert load_plan(c,r['id'])==plan and core.may_approve(c,old,r)
        assert not core.may_approve(c,c.execute('SELECT * FROM users WHERE id=?',(new,)).fetchone(),r)
        r=decide(c,r,old);r=decide(c,r,a['OP_RRHH']);assert authorization(c,r)['valid']
        with pytest.raises(sqlite3.IntegrityError): c.execute("UPDATE request_authority_plans SET plan_json='{}'")
        with pytest.raises(sqlite3.IntegrityError): c.execute("DELETE FROM approval_authority_evidence")


def test_migration_repeatable_and_all_protected_local_counts(local_copy):
    with sqlite3.connect(local_copy) as c:
        apply_migrations(c);apply_migrations(c)
        assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]==16
        for table,n in [('users',54),('branches',6),('areas',19),('biometric_events',3567),('biometric_identity_links',50),('hr_roster_proposals',51)]:
            assert c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==n
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert c.execute('SELECT COUNT(*) FROM requests').fetchone()[0]==0


@pytest.mark.parametrize('invalid',['inactive','nonexistent','technical','manager_superior'])
def test_invalid_superior_rejected(local_copy,invalid):
    with core.db() as c:
        e,a=people(c);m=core.choose_approver(c,e,'manager')
        if invalid=='inactive':
            c.execute('UPDATE users SET active=0 WHERE id=?',(m['id'],));who=e;superior=m['id']
        elif invalid=='nonexistent': who=e;superior=999999
        elif invalid=='technical': who=m;superior=a['ADMIN_TEC']['id']
        else: who=m;superior=c.execute("SELECT id FROM users WHERE role='manager' AND id<>? LIMIT 1",(m['id'],)).fetchone()[0]
        with pytest.raises(ValueError): org.validate_superior(c,who['id'],superior,core.now().date())


def test_area_transfer_keeps_old_scope_and_assignment_history(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date())
        target=c.execute('''SELECT ar.area_id,ar.user_id FROM area_responsibles ar JOIN areas a ON a.id=ar.area_id
            WHERE a.branch_id=? AND ar.area_id<>? AND ar.user_id<>? AND ar.role='PRINCIPAL' AND ar.valid_to IS NULL LIMIT 1''',
            (p['branch_id'],p['area_id'],p['supervisor_id'])).fetchone()
        org.change_placement(c,e['id'],p['company_id'],p['branch_id'],target['area_id'],target['user_id'],core.now().date(),a['OP_RRHH']['id'])
        assert org.manager_scope(c,p['supervisor_id'],e['id'],'2026-10-12')
        assert org.manager_scope(c,target['user_id'],e['id'],core.now().date())
        assert not org.manager_scope(c,p['supervisor_id'],e['id'],core.now().date())
        assert c.execute('SELECT COUNT(*) FROM employee_placements WHERE user_id=?',(e['id'],)).fetchone()[0]==2


def test_superior_projection_and_provisional_provenance_preserved(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date());old=org.authority(c,e['id'],core.now().date())
        other=c.execute("SELECT u.id FROM users u JOIN employee_placements p ON p.user_id=u.id WHERE u.role='manager' AND p.branch_id=? AND p.valid_to IS NULL AND u.id<>? LIMIT 1",(p['branch_id'],p['supervisor_id'])).fetchone()[0]
        org.assign_responsibles(c,p['area_id'],p['supervisor_id'],other,core.now().date(),a['OP_RRHH']['id'])
        org.set_superior(c,e['id'],other,core.now().date(),a['OP_RRHH']['id'])
        assert org.placement(c,e['id'],core.now().date())['supervisor_id']==other
        assert c.execute('SELECT superior_id FROM local_staff_profiles WHERE user_id=?',(e['id'],)).fetchone()[0]==other
        new=org.authority(c,e['id'],core.now().date())
        assert new['source_json']==old['source_json'] and new['provisional']==old['provisional']
        assert c.execute('SELECT confirmed_by FROM biometric_identity_links WHERE user_id=?',(e['id'],)).fetchone()[0] is None


def test_db_blocks_invalid_intervals_and_overlap(local_copy):
    with core.db() as c:
        e,_=people(c);p=org.placement(c,e['id'],core.now().date())
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE employee_placements SET valid_to='1900-01-01' WHERE id=?",(p['id'],))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute('''INSERT INTO employee_placements(user_id,area_id,valid_from,valid_to,created_at)
                VALUES(?,?,'2026-10-02','2026-10-03',?)''',(e['id'],p['area_id'],core.nowstr()))


def test_closed_period_and_backdated_mutations_blocked(local_copy):
    with core.db() as c:
        e,a=people(c);p=org.placement(c,e['id'],core.now().date())
        with pytest.raises(ValueError):
            org.change_placement(c,e['id'],p['company_id'],p['branch_id'],p['area_id'],p['supervisor_id'],'2026-09-01',a['OP_RRHH']['id'])
        c.execute("INSERT INTO monthly_closures(period_start,period_end,state,snapshot_json,closed_at,closed_by) VALUES('2026-09-26','2026-10-25','CERRADO','[]',?,?)",(core.nowstr(),a['OP_RRHH']['id']))
        before=tuple(c.execute('SELECT * FROM monthly_closures').fetchone())
        with pytest.raises(ValueError):
            org.change_placement(c,e['id'],p['company_id'],p['branch_id'],p['area_id'],p['supervisor_id'],'2026-10-12',a['OP_RRHH']['id'])
        assert before==tuple(c.execute('SELECT * FROM monthly_closures').fetchone())
