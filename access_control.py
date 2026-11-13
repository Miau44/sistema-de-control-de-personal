"""Shared module capabilities; per-request authority remains a separate check.

Explicit endpoint inventory prevents a role='hr' Administration account from inheriting HR powers.
"""
from organization import authority

SELF = set('home my_requests attendance notifications_page mark_notification_read mark_notifications_read profile change_password logout_post'.split())
REQUEST = {'request_page','request_post'}
REVIEW = {'review_page','resolve'}
OWN_HISTORY = {'manager_decisions_history'}
DOCUMENT = {'download_boleta','hr_request_detail'}
TEAM = {'my_team','my_team_attendance','my_team_member'}
RECON = {'reconciliation_page','reconciliation_detail','reconciliation_biometric_identity'}
IMPORT = {'importer_page'}
HR = set('''team add_employee edit_employee_page edit_employee_post change_employee_placement add_area
 add_branch edit_branch assign_area_branch edit_area assign_area_responsible save_schedule
 exceptions_page exceptions_post hr_request_history boleta_management bulk_boletas biometric_assignment_post
 unified_import_preview unified_import_confirm importer_post
 grouping_rule_save grouping_movement_policy_save reconciliation_recalculate
 reconciliation_import_preview reconciliation_import_confirm reconciliation_confirm_coverage
 reconciliation_link_identity reconciliation_regime_save reconciliation_work_assignment
 reconciliation_calendar_event biometric_roster_page hr_reference_page hr_reference_import
 hr_reference_confirm biometric_roster_confirm reconciliation_resolve reports_page close_post
 reopen_post report_download'''.split())
PUBLIC = {'login_page','login_post','openapi'}
CLASSIFIED = SELF|REQUEST|REVIEW|OWN_HISTORY|DOCUMENT|TEAM|RECON|IMPORT|HR|PUBLIC|{'audit_page'}


def capabilities(cx,actor):
    if not actor or not actor['active']: return frozenset()
    from core import now
    result={'self.read','document.scoped'}
    a=authority(cx,actor['id'],now().date())
    role=actor['role']
    legacy=not cx.execute('SELECT 1 FROM local_preparation WHERE id=1').fetchone()
    if role=='hr' and ((not a and legacy) or (a and a['functional_role']=='hr' and a['authority'] in ('OPERADOR_RRHH','RESPONSABLE_RRHH'))):
        result.update({'hr.global','review','history.own','request','import','audit','reconciliation'})
    elif role=='hr':
        # Unknown HR functions have no global capability. Authority for a concrete step is checked separately.
        result.update({'request','review','history.own'})
    elif role=='manager':
        result.update({'request','review','history.own','team','reconciliation'})
    elif role=='employee': result.add('request')
    elif role=='admin':
        # Keep technical import diagnostics and audit; file writes require HR authority.
        result.update({'import','audit'})
    return frozenset(result)


def endpoint_allowed(caps,name):
    if name in SELF|DOCUMENT: return 'self.read' in caps
    if name in REQUEST: return 'request' in caps
    if name in REVIEW: return 'review' in caps
    if name in OWN_HISTORY: return 'history.own' in caps
    if name in TEAM: return 'team' in caps
    if name in RECON: return 'reconciliation' in caps
    if name in IMPORT: return 'import' in caps
    if name in HR: return 'hr.global' in caps
    if name=='audit_page': return 'audit' in caps
    return name in PUBLIC


def can_view_request(cx,actor,row):
    if row['user_id']==actor['id'] or 'hr.global' in capabilities(cx,actor): return True
    # A participant's access is confined to this record, never a general grant on the employee's current file.
    if cx.execute('''SELECT 1 FROM request_approval_steps WHERE request_id=? AND assignee_id=?
        AND is_skipped=0''',(row['id'],actor['id'])).fetchone(): return True
    if actor['role']=='manager':
        from organization import manager_scope
        return manager_scope(cx,actor['id'],row['user_id'],row['submitted_at'][:10])
    return False


def pending_assignment_issue(cx,request_id):
    """Explain a blocked frozen assignment without inventing or substituting an authority."""
    from core import may_approve
    row=cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
    step=cx.execute('''SELECT * FROM request_approval_steps WHERE request_id=? AND is_skipped=0
        AND decision IS NULL ORDER BY step_order LIMIT 1''',(request_id,)).fetchone()
    if not row or row['status']!='PENDIENTE' or not step: return ''
    from approval_authority import load_plan
    plan=load_plan(cx,request_id)
    ids=[step['assignee_id']] if step['assignee_id'] else (plan['hr_ids'] if plan else [])
    available=False
    for user_id in ids:
        actor=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if actor and may_approve(cx,actor,row): available=True
    if not available:
        return 'El aprobador asignado no está disponible o ya no tiene la función requerida. RR. HH. debe revisar una sustitución autorizada; no se reasignó la etapa.'
    return ''
