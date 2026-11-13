"""Versioned, additive SQLite migrations for the existing attendance database."""
from __future__ import annotations

import sqlite3
from datetime import datetime


def _organization(cx: sqlite3.Connection) -> None:
    cx.execute('''CREATE TABLE corporate_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''INSERT INTO corporate_groups(name,created_at)
                  VALUES('Grupo corporativo',?)''',(datetime.now().isoformat(timespec='seconds'),))
    cx.execute('''CREATE TABLE companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER REFERENCES corporate_groups(id),
        name TEXT NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE TABLE branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER REFERENCES companies(id),
        name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        classification_status TEXT NOT NULL DEFAULT 'PENDIENTE'
          CHECK(classification_status IN ('PENDIENTE','CLASIFICADA')),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE UNIQUE INDEX idx_branches_company_name
                  ON branches(company_id,name) WHERE company_id IS NOT NULL''')
    cx.execute('''CREATE TABLE areas_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        branch_id INTEGER REFERENCES branches(id),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
    )''')
    cx.execute('INSERT INTO areas_new(id,name) SELECT id,name FROM areas')
    cx.execute('DROP TABLE areas')
    cx.execute('ALTER TABLE areas_new RENAME TO areas')
    cx.execute('CREATE INDEX idx_areas_branch_name ON areas(branch_id,name)')
    cx.execute('''CREATE TABLE area_responsibles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        area_id INTEGER NOT NULL REFERENCES areas(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        role TEXT NOT NULL CHECK(role IN ('PRINCIPAL','SUPLENTE')),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        assigned_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE UNIQUE INDEX idx_area_responsible_active
                  ON area_responsibles(area_id,role) WHERE valid_to IS NULL''')
    cx.execute('''CREATE TABLE employee_placements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        company_id INTEGER REFERENCES companies(id),
        branch_id INTEGER REFERENCES branches(id),
        area_id INTEGER NOT NULL REFERENCES areas(id),
        supervisor_id INTEGER REFERENCES users(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        classification_status TEXT NOT NULL DEFAULT 'PENDIENTE'
          CHECK(classification_status IN ('PENDIENTE','CLASIFICADA')),
        changed_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE UNIQUE INDEX idx_employee_placement_current
                  ON employee_placements(user_id) WHERE valid_to IS NULL''')
    cx.execute('''INSERT INTO employee_placements(user_id,area_id,valid_from,classification_status,created_at)
                  SELECT id,area_id,substr(created_at,1,10),'PENDIENTE',created_at FROM users''')
    cx.execute('''CREATE TABLE biometric_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_id INTEGER NOT NULL REFERENCES branches(id),
        device_code TEXT NOT NULL,
        employee_code TEXT NOT NULL,
        user_id INTEGER NOT NULL REFERENCES users(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE UNIQUE INDEX idx_biometric_device_code_active
                  ON biometric_assignments(branch_id,device_code,employee_code)
                  WHERE valid_to IS NULL''')
    cx.execute('''CREATE TABLE work_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE TABLE work_schedule_days (
        schedule_id INTEGER NOT NULL REFERENCES work_schedules(id),
        weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
        is_working INTEGER NOT NULL CHECK(is_working IN (0,1)),
        start_time TEXT,
        end_time TEXT,
        grace_minutes INTEGER NOT NULL DEFAULT 5 CHECK(grace_minutes BETWEEN 0 AND 60),
        PRIMARY KEY(schedule_id,weekday),
        CHECK((is_working=0) OR (start_time IS NOT NULL AND end_time IS NOT NULL))
    )''')
    cx.execute('''CREATE TABLE schedule_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id INTEGER NOT NULL REFERENCES work_schedules(id),
        user_id INTEGER REFERENCES users(id),
        area_id INTEGER REFERENCES areas(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        assigned_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL,
        CHECK((user_id IS NOT NULL) != (area_id IS NOT NULL))
    )''')
    cx.execute('''CREATE INDEX idx_schedule_assignment_user_dates
                  ON schedule_assignments(user_id,valid_from,valid_to)''')
    cx.execute('''CREATE INDEX idx_schedule_assignment_area_dates
                  ON schedule_assignments(area_id,valid_from,valid_to)''')
    cx.execute('''CREATE TABLE import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT NOT NULL,
        branch_id INTEGER REFERENCES branches(id),
        device_code TEXT,
        imported_by INTEGER REFERENCES users(id),
        imported_at TEXT NOT NULL,
        rows_total INTEGER NOT NULL DEFAULT 0,
        rows_added INTEGER NOT NULL DEFAULT 0,
        rows_duplicate INTEGER NOT NULL DEFAULT 0,
        rows_error INTEGER NOT NULL DEFAULT 0,
        details_json TEXT NOT NULL DEFAULT '[]'
    )''')
    for name in ('KIMSTAR', 'SEDE CENTRAL', 'MERCATECH EL ALTO',
                 'MERCATECH RIOSINIO', 'MERCATECH LA PAZ', 'MERCATECH COCHABAMBA'):
        cx.execute('''INSERT INTO branches(name,active,classification_status,created_at)
                      VALUES(?,0,'PENDIENTE',?)''', (name, datetime.now().isoformat(timespec='seconds')))


def _approval_stages(cx: sqlite3.Connection) -> None:
    cx.execute('''CREATE TABLE approval_flows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        request_kind TEXT NOT NULL CHECK(request_kind IN ('PERMISO','CORRECCION')),
        company_id INTEGER REFERENCES companies(id),
        branch_id INTEGER REFERENCES branches(id),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_by INTEGER REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('''CREATE TABLE approval_flow_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        flow_id INTEGER NOT NULL REFERENCES approval_flows(id),
        step_order INTEGER NOT NULL,
        title TEXT NOT NULL,
        assignee_role TEXT NOT NULL CHECK(assignee_role IN ('manager','hr')),
        required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
        UNIQUE(flow_id,step_order)
    )''')
    cx.execute('''CREATE TABLE request_approval_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES requests(id),
        step_order INTEGER NOT NULL,
        title TEXT NOT NULL,
        assignee_role TEXT NOT NULL,
        assignee_id INTEGER REFERENCES users(id),
        decision TEXT CHECK(decision IN ('APROBADA','RECHAZADA')),
        decided_at TEXT,
        note TEXT NOT NULL DEFAULT '',
        is_skipped INTEGER NOT NULL DEFAULT 0 CHECK(is_skipped IN (0,1)),
        skip_reason TEXT NOT NULL DEFAULT '',
        is_legacy INTEGER NOT NULL DEFAULT 0 CHECK(is_legacy IN (0,1)),
        UNIQUE(request_id,step_order)
    )''')
    cx.execute('CREATE INDEX idx_request_steps_assignee ON request_approval_steps(assignee_id,decision)')

    for kind in ('PERMISO', 'CORRECCION'):
        cur = cx.execute('''INSERT INTO approval_flows(name,request_kind,created_at)
                            VALUES(?,?,?)''', ('Flujo predeterminado ' + kind, kind,
                                               datetime.now().isoformat(timespec='seconds')))
        flow_id = cur.lastrowid
        cx.executemany('''INSERT INTO approval_flow_steps(flow_id,step_order,title,assignee_role)
                          VALUES(?,?,?,?)''', [
            (flow_id, 1, 'Revisión del encargado directo', 'manager'),
            (flow_id, 2, 'Aprobación definitiva de Recursos Humanos', 'hr'),
        ])
    cx.execute('''INSERT INTO request_approval_steps(
                    request_id,step_order,title,assignee_role,assignee_id,decision,decided_at,note,is_legacy)
                  SELECT r.id,1,'Decisión del sistema anterior',
                         CASE WHEN u.role='manager' THEN 'manager' ELSE 'hr' END,
                         r.approver_id,
                         CASE WHEN r.status='PENDIENTE' THEN NULL ELSE r.status END,
                         r.decided_at,r.decision_note,1
                  FROM requests r JOIN users u ON u.id=r.approver_id''')


def _closure_history_and_notifications(cx: sqlite3.Connection) -> None:
    cx.execute('''CREATE TABLE monthly_closure_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        closure_id INTEGER NOT NULL REFERENCES monthly_closures(id),
        version INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('CERRADO','REABIERTO')),
        state TEXT NOT NULL CHECK(state IN ('CERRADO','REABIERTO')),
        occurred_at TEXT NOT NULL,
        actor_id INTEGER REFERENCES users(id),
        snapshot_json TEXT NOT NULL,
        excel_path TEXT,
        reason TEXT NOT NULL DEFAULT '',
        UNIQUE(closure_id,version)
    )''')
    cx.execute('''INSERT INTO monthly_closure_versions(
                    closure_id,version,event_type,state,occurred_at,actor_id,snapshot_json,reason)
                  SELECT id,1,
                         CASE WHEN state='CERRADO' THEN 'CERRADO' ELSE 'REABIERTO' END,
                         state,COALESCE(reopened_at,closed_at),
                         CASE WHEN state='CERRADO' THEN closed_by ELSE reopened_by END,
                         snapshot_json,reopen_reason
                  FROM monthly_closures''')
    cx.execute('''CREATE TABLE internal_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        kind TEXT NOT NULL,
        message TEXT NOT NULL,
        entity TEXT NOT NULL DEFAULT '',
        entity_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        read_at TEXT
    )''')
    cx.execute('''CREATE INDEX idx_notifications_user_unread
                  ON internal_notifications(user_id,read_at,created_at DESC)''')


def _kimtek_and_notification_deduplication(cx: sqlite3.Connection) -> None:
    """Make KIMTEK the sole active company without rewriting historical links."""
    stamp = datetime.now().isoformat(timespec='seconds')
    row = cx.execute("SELECT id,name FROM companies WHERE upper(name)='KIMTEK' ORDER BY CASE WHEN name='KIMTEK' THEN 0 ELSE 1 END,id LIMIT 1").fetchone()
    if row:
        company_id = row[0]
        if row[1] != 'KIMTEK':
            cx.execute("UPDATE companies SET name='KIMTEK' WHERE id=?", (company_id,))
        cx.execute("UPDATE companies SET active=0 WHERE id<>?", (company_id,))
        cx.execute("UPDATE companies SET active=1 WHERE id=?", (company_id,))
    else:
        group_id = cx.execute('SELECT id FROM corporate_groups ORDER BY id LIMIT 1').fetchone()
        cur = cx.execute('INSERT INTO companies(group_id,name,active,created_at) VALUES(?,?,1,?)',
                         (group_id[0] if group_id else None, 'KIMTEK', stamp))
        company_id = cur.lastrowid
        cx.execute('UPDATE companies SET active=0 WHERE id<>?', (company_id,))

    # The six seed branches were intentionally unassigned. Their names are an
    # explicit match; all other legacy or ambiguous branches retain their links.
    names = {
        'KIMSTAR': 'Kimstar', 'SEDE CENTRAL': 'Sede Central',
        'MERCATECH EL ALTO': 'Mercatech El Alto',
        'MERCATECH RIOSINIO': 'Mercatech Riosinio',
        'MERCATECH LA PAZ': 'Mercatech La Paz',
        'MERCATECH COCHABAMBA': 'Mercatech Cochabamba',
    }
    for old_name, display_name in names.items():
        cx.execute('''UPDATE branches SET name=?,company_id=?,active=1,classification_status='CLASIFICADA'
                      WHERE upper(name)=? AND company_id IS NULL
                        AND NOT EXISTS (SELECT 1 FROM branches existing
                          WHERE existing.company_id=? AND existing.name=? AND existing.id<>branches.id)''',
                   (display_name, company_id, old_name, company_id, display_name))
        # Some demo/legacy databases already assigned a seed branch to a
        # company named Kimtek. Normalize that known branch in place while
        # keeping its company and branch IDs intact.
        cx.execute('''UPDATE branches SET name=?,active=1,classification_status='CLASIFICADA'
                      WHERE upper(name)=? AND company_id=?
                        AND NOT EXISTS (SELECT 1 FROM branches existing
                          WHERE existing.company_id=? AND existing.name=? AND existing.id<>branches.id)''',
                   (display_name, old_name, company_id, company_id, display_name))

    cx.execute('ALTER TABLE internal_notifications ADD COLUMN dedupe_key TEXT')
    cx.execute('''CREATE UNIQUE INDEX idx_notifications_dedupe
                  ON internal_notifications(user_id,dedupe_key) WHERE dedupe_key IS NOT NULL''')


def _request_history_snapshots(cx: sqlite3.Connection) -> None:
    """Freeze the employee and reviewer labels used by historical request records."""
    additions={
        'requests':(
            ('employee_name_snapshot',"TEXT NOT NULL DEFAULT ''"),
            ('employee_code_snapshot',"TEXT NOT NULL DEFAULT ''"),
            ('employee_position_snapshot',"TEXT NOT NULL DEFAULT ''"),
            ('branch_name_snapshot',"TEXT NOT NULL DEFAULT ''"),
            ('area_name_snapshot',"TEXT NOT NULL DEFAULT ''"),
        ),
        'request_approval_steps':(('assignee_name_snapshot',"TEXT NOT NULL DEFAULT ''"),),
    }
    for table,columns in additions.items():
        existing={row['name'] for row in cx.execute(f'PRAGMA table_info({table})')}
        for name,declaration in columns:
            if name not in existing:
                cx.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    requests=cx.execute('''SELECT r.id,r.user_id,r.submitted_at,u.name,u.employee_code,u.position,
        p.area_id,p.branch_id,b.name branch_name,a.name area_name
        FROM requests r JOIN users u ON u.id=r.user_id
        LEFT JOIN employee_placements p ON p.id=(SELECT p2.id FROM employee_placements p2
          WHERE p2.user_id=r.user_id AND p2.valid_from<=substr(r.submitted_at,1,10)
            AND (p2.valid_to IS NULL OR p2.valid_to>=substr(r.submitted_at,1,10))
          ORDER BY p2.valid_from DESC,p2.id DESC LIMIT 1)
        LEFT JOIN branches b ON b.id=p.branch_id LEFT JOIN areas a ON a.id=COALESCE(p.area_id,u.area_id)''').fetchall()
    for row in requests:
        cx.execute('''UPDATE requests SET employee_name_snapshot=?,employee_code_snapshot=?,
            employee_position_snapshot=?,branch_name_snapshot=?,area_name_snapshot=? WHERE id=?''',
            (row['name'],row['employee_code'],row['position'],row['branch_name'] or '',row['area_name'] or '',row['id']))
    cx.execute('''UPDATE request_approval_steps SET assignee_name_snapshot=COALESCE(
        (SELECT u.name FROM users u WHERE u.id=request_approval_steps.assignee_id),'')
        WHERE assignee_id IS NOT NULL AND assignee_name_snapshot='' ''')


def _attendance_reconciliation(cx: sqlite3.Connection) -> None:
    """Add raw ZKBio events and auditable, conservative attendance reconciliation."""
    additions = (
        ('file_sha256', "TEXT NOT NULL DEFAULT ''"),
        ('coverage_start', 'TEXT'),
        ('coverage_end', 'TEXT'),
        ('rows_observed', 'INTEGER NOT NULL DEFAULT 0'),
        ('rows_unlinked', 'INTEGER NOT NULL DEFAULT 0'),
        ('coverage_reviewed', 'INTEGER NOT NULL DEFAULT 0 CHECK(coverage_reviewed IN (0,1))'),
    )
    existing = {row['name'] for row in cx.execute('PRAGMA table_info(import_batches)')}
    for name, declaration in additions:
        if name not in existing:
            cx.execute(f'ALTER TABLE import_batches ADD COLUMN {name} {declaration}')

    cx.execute('''CREATE TABLE biometric_persons (
        person_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        department TEXT NOT NULL DEFAULT '',
        candidate_note TEXT NOT NULL DEFAULT '',
        linked_user_id INTEGER REFERENCES users(id),
        link_status TEXT NOT NULL DEFAULT 'PENDING'
          CHECK(link_status IN ('PENDING','CONFIRMED','AMBIGUOUS')),
        reviewed_by INTEGER REFERENCES users(id),
        reviewed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((link_status='CONFIRMED' AND linked_user_id IS NOT NULL) OR
              (link_status<>'CONFIRMED' AND linked_user_id IS NULL))
    )''')
    cx.execute('''CREATE TABLE biometric_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        import_batch_id INTEGER NOT NULL REFERENCES import_batches(id),
        person_id TEXT NOT NULL REFERENCES biometric_persons(person_id),
        person_name TEXT NOT NULL,
        department TEXT NOT NULL DEFAULT '',
        event_type TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        weekday TEXT NOT NULL DEFAULT '',
        occurred_at TEXT NOT NULL,
        timezone TEXT NOT NULL DEFAULT '',
        verification_mode TEXT NOT NULL DEFAULT '',
        is_mobile TEXT NOT NULL DEFAULT '',
        device_sn TEXT NOT NULL DEFAULT '',
        device_name TEXT NOT NULL DEFAULT '',
        uploaded_at TEXT NOT NULL DEFAULT '',
        row_number INTEGER NOT NULL,
        row_fingerprint TEXT NOT NULL UNIQUE,
        imported_by INTEGER REFERENCES users(id),
        imported_at TEXT NOT NULL
    )''')
    cx.execute('CREATE INDEX idx_biometric_events_person_time ON biometric_events(person_id,occurred_at)')
    cx.execute('CREATE INDEX idx_biometric_events_batch ON biometric_events(import_batch_id,row_number)')

    cx.execute('''CREATE TABLE attendance_employment_periods (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
        note TEXT NOT NULL,
        assigned_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        CHECK(valid_to IS NULL OR valid_to>=valid_from)
    )''')
    cx.execute('CREATE INDEX idx_attendance_employment_user_dates ON attendance_employment_periods(user_id,valid_from,valid_to)')
    cx.execute('''CREATE TABLE attendance_regime_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        regime_code TEXT NOT NULL CHECK(regime_code IN
          ('BRANCH_STANDARD','KIMSTAR_ROTATING','MOBILE_NO_FIXED_HOURS','CUSTOM')),
        submodality TEXT NOT NULL DEFAULT '' CHECK(submodality IN ('','DAYS','ASSIGNMENTS','ROTATING')),
        weekdays_json TEXT NOT NULL DEFAULT '[]',
        shift_start TEXT,
        shift_end TEXT,
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
        note TEXT NOT NULL DEFAULT '',
        assigned_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        CHECK(valid_to IS NULL OR valid_to>=valid_from)
    )''')
    cx.execute('CREATE INDEX idx_attendance_regime_user_dates ON attendance_regime_assignments(user_id,valid_from,valid_to)')
    cx.execute('''CREATE TABLE attendance_work_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        description TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '',
        confirmed INTEGER NOT NULL DEFAULT 1 CHECK(confirmed IN (0,1)),
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        UNIQUE(user_id,work_date,description)
    )''')
    cx.execute('''CREATE TABLE attendance_calendar_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN
          ('FERIADO','ASUETO','CUMPLEANOS','DIA_MUJER','DESCANSO_ESPECIAL','OTRO')),
        date_from TEXT NOT NULL,
        date_to TEXT NOT NULL,
        branch_id INTEGER REFERENCES branches(id),
        user_id INTEGER REFERENCES users(id),
        eligibility TEXT NOT NULL DEFAULT '',
        approved_by INTEGER REFERENCES users(id),
        rule_note TEXT NOT NULL DEFAULT '',
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        CHECK(date_to>=date_from),
        CHECK(user_id IS NULL OR branch_id IS NULL)
    )''')
    cx.execute('CREATE INDEX idx_attendance_calendar_dates ON attendance_calendar_events(date_from,date_to,branch_id,user_id)')
    cx.execute('''CREATE TABLE attendance_resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        resolution_type TEXT NOT NULL CHECK(resolution_type IN ('JUSTIFICACION','FALTA_CONFIRMADA')),
        reason TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    )''')
    cx.execute('CREATE INDEX idx_attendance_resolution_user_date ON attendance_resolutions(user_id,work_date,active)')
    cx.execute('''CREATE TABLE attendance_reconciliation_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_from TEXT NOT NULL,
        date_to TEXT NOT NULL,
        input_hash TEXT NOT NULL UNIQUE,
        actor_id INTEGER NOT NULL REFERENCES users(id),
        executed_at TEXT NOT NULL,
        people_evaluated INTEGER NOT NULL DEFAULT 0,
        days_evaluated INTEGER NOT NULL DEFAULT 0,
        source_coverage_reviewed INTEGER NOT NULL DEFAULT 0 CHECK(source_coverage_reviewed IN (0,1)),
        is_partial INTEGER NOT NULL DEFAULT 1 CHECK(is_partial IN (0,1)),
        CHECK(date_to>=date_from)
    )''')
    cx.execute('''CREATE TABLE attendance_reconciliation_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES attendance_reconciliation_runs(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        punch_status TEXT NOT NULL,
        justification_status TEXT NOT NULL,
        operational_result TEXT NOT NULL,
        work_obligation TEXT NOT NULL,
        punch_count INTEGER NOT NULL DEFAULT 0,
        source_summary TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(run_id,user_id,work_date)
    )''')
    cx.execute('CREATE INDEX idx_reconciliation_run_date_result ON attendance_reconciliation_days(run_id,work_date,operational_result)')


def _attendance_system_actor(cx: sqlite3.Connection) -> None:
    """Allow an explicitly labeled system-assisted import without impersonating a user."""
    cx.execute('''CREATE TABLE attendance_reconciliation_runs_v7 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date_from TEXT NOT NULL,
        date_to TEXT NOT NULL,
        input_hash TEXT NOT NULL UNIQUE,
        actor_id INTEGER REFERENCES users(id),
        actor_label TEXT NOT NULL DEFAULT '',
        executed_at TEXT NOT NULL,
        people_evaluated INTEGER NOT NULL DEFAULT 0,
        days_evaluated INTEGER NOT NULL DEFAULT 0,
        source_coverage_reviewed INTEGER NOT NULL DEFAULT 0 CHECK(source_coverage_reviewed IN (0,1)),
        is_partial INTEGER NOT NULL DEFAULT 1 CHECK(is_partial IN (0,1)),
        CHECK(date_to>=date_from)
    )''')
    cx.execute('''INSERT INTO attendance_reconciliation_runs_v7
        (id,date_from,date_to,input_hash,actor_id,actor_label,executed_at,people_evaluated,
         days_evaluated,source_coverage_reviewed,is_partial)
        SELECT r.id,r.date_from,r.date_to,r.input_hash,r.actor_id,COALESCE(u.name,'Sistema'),
         r.executed_at,r.people_evaluated,r.days_evaluated,r.source_coverage_reviewed,r.is_partial
        FROM attendance_reconciliation_runs r LEFT JOIN users u ON u.id=r.actor_id''')
    cx.execute('''CREATE TABLE attendance_reconciliation_days_v7 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL REFERENCES attendance_reconciliation_runs_v7(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        punch_status TEXT NOT NULL,
        justification_status TEXT NOT NULL,
        operational_result TEXT NOT NULL,
        work_obligation TEXT NOT NULL,
        punch_count INTEGER NOT NULL DEFAULT 0,
        source_summary TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(run_id,user_id,work_date)
    )''')
    cx.execute('''INSERT INTO attendance_reconciliation_days_v7
        (id,run_id,user_id,work_date,punch_status,justification_status,operational_result,
         work_obligation,punch_count,source_summary,reason,detail_json)
        SELECT id,run_id,user_id,work_date,punch_status,justification_status,operational_result,
         work_obligation,punch_count,source_summary,reason,detail_json
        FROM attendance_reconciliation_days''')
    cx.execute('DROP TABLE attendance_reconciliation_days')
    cx.execute('DROP TABLE attendance_reconciliation_runs')
    cx.execute('ALTER TABLE attendance_reconciliation_runs_v7 RENAME TO attendance_reconciliation_runs')
    cx.execute('ALTER TABLE attendance_reconciliation_days_v7 RENAME TO attendance_reconciliation_days')
    cx.execute('CREATE INDEX idx_reconciliation_run_date_result ON attendance_reconciliation_days(run_id,work_date,operational_result)')


def _biometric_identity_history(cx: sqlite3.Connection) -> None:
    """Keep effective-dated employee links for biometric identities."""
    cx.execute('''CREATE TABLE biometric_identity_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id TEXT NOT NULL REFERENCES biometric_persons(person_id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        valid_from TEXT NOT NULL,
        valid_to TEXT,
        confirmed_by INTEGER REFERENCES users(id),
        confirmed_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        CHECK(valid_to IS NULL OR valid_to>=valid_from)
    )''')
    cx.execute('CREATE INDEX idx_biometric_identity_link_person_dates ON biometric_identity_links(person_id,valid_from,valid_to)')
    cx.execute('CREATE INDEX idx_biometric_identity_link_user_dates ON biometric_identity_links(user_id,valid_from,valid_to)')
    cx.execute('''CREATE UNIQUE INDEX idx_biometric_identity_one_current_link
        ON biometric_identity_links(person_id) WHERE valid_to IS NULL''')
    cx.execute('''INSERT INTO biometric_identity_links(person_id,user_id,valid_from,valid_to,confirmed_by,confirmed_at)
        SELECT person_id,linked_user_id,substr(created_at,1,10),NULL,reviewed_by,
          COALESCE(reviewed_at,updated_at) FROM biometric_persons
        WHERE link_status='CONFIRMED' AND linked_user_id IS NOT NULL''')


def _reconciliation_report_link(cx: sqlite3.Connection) -> None:
    """Link future official snapshots to the reconciliation run they consumed."""
    for table in ('monthly_closures', 'monthly_closure_versions'):
        cols = {row['name'] for row in cx.execute(f'PRAGMA table_info({table})')}
        if 'reconciliation_run_id' not in cols:
            cx.execute(f'''ALTER TABLE {table} ADD COLUMN reconciliation_run_id INTEGER
                REFERENCES attendance_reconciliation_runs(id)''')


def _biometric_stable_event_key(cx: sqlite3.Connection) -> None:
    """Deduplicate the same ZKBio event across re-exports with changed upload times."""
    import hashlib
    import json
    cols = {row['name'] for row in cx.execute('PRAGMA table_info(biometric_events)')}
    if 'event_identity_key' not in cols:
        cx.execute("ALTER TABLE biometric_events ADD COLUMN event_identity_key TEXT NOT NULL DEFAULT ''")
    rows=cx.execute('''SELECT id,person_id,occurred_at,event_type,source,device_sn,timezone,
        verification_mode,is_mobile FROM biometric_events ORDER BY id''').fetchall()
    seen=set()
    for row in rows:
        source={key:row[key] or '' for key in ('person_id','occurred_at','event_type','source',
            'device_sn','timezone','verification_mode','is_mobile')}
        base=hashlib.sha256(json.dumps(source,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        key=base if base not in seen else f'{base}:{row["id"]}'
        seen.add(base)
        cx.execute('UPDATE biometric_events SET event_identity_key=? WHERE id=?',(key,row['id']))
    cx.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_biometric_event_identity_key
        ON biometric_events(event_identity_key) WHERE event_identity_key<>'' ''')


def _biometric_roster_review(cx: sqlite3.Connection) -> None:
    cx.execute('''CREATE TABLE biometric_roster_reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        person_id TEXT NOT NULL REFERENCES biometric_persons(person_id),
        classification TEXT NOT NULL CHECK(classification IN ('PENDING','EMPLOYEE','FORMER','NON_EMPLOYEE')),
        reason TEXT NOT NULL,
        reviewed_by INTEGER NOT NULL REFERENCES users(id),
        reviewed_at TEXT NOT NULL,
        user_id INTEGER REFERENCES users(id)
    )''')
    cx.execute('CREATE INDEX idx_roster_review_identity ON biometric_roster_reviews(person_id,id)')


def _attendance_accuracy_stage_one(cx: sqlite3.Connection) -> None:
    script='''
        CREATE TABLE attendance_journey_segments (
          id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), area_id INTEGER REFERENCES areas(id),
          weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6), valid_from TEXT NOT NULL, valid_to TEXT,
          start_time TEXT NOT NULL,end_time TEXT NOT NULL,day_offset INTEGER NOT NULL DEFAULT 0,
          confirmed INTEGER NOT NULL DEFAULT 0,note TEXT NOT NULL,approved_by INTEGER REFERENCES users(id),
          CHECK((user_id IS NULL)!=(area_id IS NULL)),CHECK(valid_to IS NULL OR valid_to>=valid_from));
        CREATE TABLE attendance_presentation_policies (
          id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),valid_from TEXT NOT NULL,valid_to TEXT,
          start_time TEXT NOT NULL,end_time TEXT NOT NULL,note TEXT NOT NULL,approved_by INTEGER NOT NULL REFERENCES users(id));
        CREATE TABLE attendance_daily_versions (
          id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,
          evaluation_id INTEGER NOT NULL REFERENCES attendance_reconciliation_days(id),
          previous_id INTEGER REFERENCES attendance_daily_versions(id),request_id INTEGER REFERENCES requests(id),
          reason TEXT NOT NULL,created_at TEXT NOT NULL,closed_period_analysis INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE attendance_daily_current (
          user_id INTEGER NOT NULL REFERENCES users(id),work_date TEXT NOT NULL,
          version_id INTEGER NOT NULL REFERENCES attendance_daily_versions(id),stale INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(user_id,work_date));
        CREATE TABLE attendance_recalc_queue (
          id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),starts_at TEXT,ends_at TEXT,
          request_id INTEGER REFERENCES requests(id),reason TEXT NOT NULL,created_at TEXT NOT NULL,processed_at TEXT);
        CREATE TABLE hr_reference_imports (
          id INTEGER PRIMARY KEY,detailed_hash TEXT NOT NULL,summary_hash TEXT NOT NULL,
          filename TEXT NOT NULL,imported_by INTEGER REFERENCES users(id),imported_at TEXT NOT NULL,
          stats_json TEXT NOT NULL,UNIQUE(detailed_hash,summary_hash));
        CREATE TABLE hr_roster_proposals (
          id INTEGER PRIMARY KEY,import_id INTEGER NOT NULL REFERENCES hr_reference_imports(id),
          identity_code TEXT NOT NULL,original_name TEXT NOT NULL,branch_label TEXT NOT NULL,department TEXT NOT NULL,
          position TEXT NOT NULL,branch_id INTEGER REFERENCES branches(id),biometric_person_id TEXT REFERENCES biometric_persons(person_id),
          proposed_user_id INTEGER REFERENCES users(id),user_id INTEGER REFERENCES users(id),
          state TEXT NOT NULL DEFAULT 'PENDING',confirmed_by INTEGER REFERENCES users(id),confirmed_at TEXT,note TEXT NOT NULL DEFAULT '',
          UNIQUE(import_id,identity_code));
        CREATE TABLE hr_reference_days (
          id INTEGER PRIMARY KEY,proposal_id INTEGER NOT NULL REFERENCES hr_roster_proposals(id),work_date TEXT NOT NULL,
          category TEXT NOT NULL,detailed_text TEXT NOT NULL,summary_text TEXT NOT NULL,
          physical_json TEXT NOT NULL,manual_json TEXT NOT NULL,administrative_json TEXT NOT NULL,late_minutes INTEGER,
          UNIQUE(proposal_id,work_date));
        CREATE INDEX idx_recalc_pending ON attendance_recalc_queue(processed_at,user_id);
        CREATE TRIGGER request_attendance_insert AFTER INSERT ON requests BEGIN
          INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,request_id,reason,created_at)
          VALUES(NEW.user_id,COALESCE(NULLIF(NEW.starts_at,''),NULLIF(NEW.correction_at,'')),COALESCE(NULLIF(NEW.ends_at,''),NULLIF(NEW.correction_at,'')),NEW.id,'Solicitud creada',datetime('now'));
        END;
        CREATE TRIGGER request_attendance_update AFTER UPDATE OF user_id,starts_at,ends_at,correction_at,correction_kind,status,decided_by,decided_at ON requests BEGIN
          INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,request_id,reason,created_at)
          VALUES(OLD.user_id,COALESCE(NULLIF(OLD.starts_at,''),NULLIF(OLD.correction_at,'')),COALESCE(NULLIF(OLD.ends_at,''),NULLIF(OLD.correction_at,'')),NEW.id,'Cambio de solicitud: intervalo anterior',datetime('now'));
          INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,request_id,reason,created_at)
          VALUES(NEW.user_id,COALESCE(NULLIF(NEW.starts_at,''),NULLIF(NEW.correction_at,'')),COALESCE(NULLIF(NEW.ends_at,''),NULLIF(NEW.correction_at,'')),NEW.id,'Cambio de solicitud: intervalo nuevo',datetime('now'));
          UPDATE attendance_daily_current SET stale=1 WHERE user_id IN (OLD.user_id,NEW.user_id)
            AND work_date BETWEEN date(COALESCE(NULLIF(OLD.starts_at,''),NULLIF(OLD.correction_at,'')),'-1 day') AND substr(COALESCE(NULLIF(OLD.ends_at,''),NULLIF(OLD.correction_at,'')),1,10);
          UPDATE attendance_daily_current SET stale=1 WHERE user_id=NEW.user_id
            AND work_date BETWEEN date(COALESCE(NULLIF(NEW.starts_at,''),NULLIF(NEW.correction_at,'')),'-1 day') AND substr(COALESCE(NULLIF(NEW.ends_at,''),NULLIF(NEW.correction_at,'')),1,10);
        END;
    '''
    statement=''
    for line in script.splitlines(True):
        statement+=line
        if sqlite3.complete_statement(statement):
            cx.execute(statement)
            statement=''


def _continuous_attendance(cx):
    cx.execute('''CREATE TABLE attendance_import_receipts (
      id INTEGER PRIMARY KEY,batch_id INTEGER NOT NULL REFERENCES import_batches(id),
      actor_id INTEGER NOT NULL REFERENCES users(id),received_at TEXT NOT NULL,
      requested_from TEXT NOT NULL,requested_to TEXT NOT NULL,exported_at TEXT,
      included_devices_json TEXT NOT NULL,expected_devices_json TEXT NOT NULL,
      device_states_json TEXT NOT NULL,coverage_state TEXT NOT NULL,
      confirmed_by INTEGER REFERENCES users(id),note TEXT NOT NULL,summary_json TEXT NOT NULL)''')
    cx.execute('''CREATE TABLE attendance_source_changes (
      id INTEGER PRIMARY KEY,source_table TEXT NOT NULL,old_json TEXT,new_json TEXT,
      created_at TEXT NOT NULL,processed_at TEXT)''')
    cx.execute('CREATE INDEX idx_attendance_source_pending ON attendance_source_changes(processed_at)')
    cx.execute('ALTER TABLE attendance_daily_current ADD COLUMN refresh_after TEXT')
    cx.execute('CREATE INDEX idx_attendance_refresh_after ON attendance_daily_current(refresh_after)')
    cx.execute('CREATE INDEX idx_attendance_current_date ON attendance_daily_current(work_date,user_id)')
    cx.execute('CREATE INDEX idx_attendance_receipt_range ON attendance_import_receipts(requested_from,requested_to,id)')
    cx.execute('''INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,reason,created_at)
        SELECT user_id,work_date,work_date,'Actualizar evaluación al motor continuo v13',datetime('now')
        FROM attendance_daily_current''')
    cx.execute('UPDATE attendance_daily_current SET stale=1')
    tables={
      'biometric_events':('person_id','occurred_at'),
      'punches':('user_id','occurred_at'),
      'biometric_identity_links':('person_id','user_id','valid_from','valid_to'),
      'attendance_employment_periods':('user_id','valid_from','valid_to'),
      'attendance_regime_assignments':('user_id','valid_from','valid_to'),
      'employee_placements':('user_id','valid_from','valid_to'),
      'attendance_journey_segments':('user_id','area_id','valid_from','valid_to'),
      'schedule_assignments':('user_id','area_id','valid_from','valid_to'),
      'work_schedule_days':('schedule_id',),
      'attendance_work_assignments':('user_id','work_date'),
      'attendance_calendar_events':('user_id','branch_id','date_from','date_to'),
      'attendance_resolutions':('user_id','work_date'),
      'attendance_presentation_policies':('user_id','valid_from','valid_to'),
      'request_approval_steps':('request_id',),
      'calendar_exceptions':('user_id','date'),
    }
    for table,columns in tables.items():
        for action in ('INSERT','UPDATE','DELETE'):
            def payload(prefix):
                return "json_object("+','.join("'%s',%s.%s"%(col,prefix,col) for col in columns)+")"
            old=payload('OLD') if action!='INSERT' else 'NULL'
            new=payload('NEW') if action!='DELETE' else 'NULL'
            cx.execute(f'''CREATE TRIGGER continuous_{table}_{action.lower()} AFTER {action} ON {table} BEGIN
                INSERT INTO attendance_source_changes(source_table,old_json,new_json,created_at)
                VALUES('{table}',{old},{new},datetime('now')); END''')


def _logical_punch_groups(cx):
    cx.execute('''CREATE TABLE attendance_grouping_rules (
      id INTEGER PRIMARY KEY,valid_from TEXT NOT NULL,window_seconds INTEGER NOT NULL CHECK(window_seconds BETWEEN 1 AND 300),
      note TEXT NOT NULL,created_by INTEGER REFERENCES users(id),created_at TEXT NOT NULL)''')
    cx.execute("INSERT INTO attendance_grouping_rules VALUES(1,'1900-01-01',60,'Regla provisional: validar con RR. HH.',NULL,datetime('now'))")
    cx.execute('''CREATE TABLE attendance_movement_policies (
      id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),valid_from TEXT NOT NULL,valid_to TEXT,
      boundary_seconds INTEGER NOT NULL CHECK(boundary_seconds BETWEEN 1 AND 3600),
      note TEXT NOT NULL,approved_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
      CHECK(valid_to IS NULL OR valid_to>=valid_from))''')
    for table,cols in [('attendance_grouping_rules',('valid_from',)),
                       ('attendance_movement_policies',('user_id','valid_from','valid_to'))]:
        for action in ('INSERT','UPDATE','DELETE'):
            def data(prefix):return 'json_object('+','.join("'%s',%s.%s"%(k,prefix,k) for k in cols)+')'
            old=data('OLD') if action!='INSERT' else 'NULL';new=data('NEW') if action!='DELETE' else 'NULL'
            cx.execute(f'''CREATE TRIGGER groups_{table}_{action.lower()} AFTER {action} ON {table} BEGIN
              INSERT INTO attendance_source_changes(source_table,old_json,new_json,created_at)
              VALUES('{table}',{old},{new},datetime('now')); END''')
    cx.execute('''INSERT INTO attendance_recalc_queue(user_id,starts_at,ends_at,reason,created_at)
        SELECT user_id,work_date,work_date,'Incorporar grupos lógicos v14',datetime('now') FROM attendance_daily_current''')
    cx.execute('UPDATE attendance_daily_current SET stale=1')


def _local_preparation_metadata(cx):
    cx.execute('''CREATE TABLE local_staff_profiles (
        user_id INTEGER PRIMARY KEY REFERENCES users(id),
        person_id TEXT UNIQUE REFERENCES biometric_persons(person_id),
        account_kind TEXT NOT NULL CHECK(account_kind IN ('LABORAL','OPERATIVA')),
        operational_authority TEXT NOT NULL DEFAULT '',superior_id INTEGER REFERENCES users(id),
        source_json TEXT NOT NULL,provisional INTEGER NOT NULL DEFAULT 1,
        CHECK(superior_id IS NULL OR superior_id<>user_id))''')
    cx.execute('''CREATE TABLE local_preparation (
        id INTEGER PRIMARY KEY CHECK(id=1),source_hashes_json TEXT NOT NULL,seed INTEGER NOT NULL,
        prepared_at TEXT NOT NULL,summary_json TEXT NOT NULL)''')


def _organizational_authority(cx):
    cx.execute('''CREATE TABLE organization_authority_history (
        id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),
        superior_id INTEGER REFERENCES users(id),authority TEXT NOT NULL,functional_role TEXT NOT NULL,
        valid_from TEXT NOT NULL,valid_to TEXT,provisional INTEGER NOT NULL,
        source_json TEXT NOT NULL,changed_by INTEGER REFERENCES users(id),created_at TEXT NOT NULL,
        CHECK(superior_id IS NULL OR superior_id<>user_id),
        CHECK(valid_to IS NULL OR valid_to>=valid_from))''')
    cx.execute('''INSERT INTO organization_authority_history
        (user_id,superior_id,authority,functional_role,valid_from,provisional,source_json,created_at)
        SELECT l.user_id,l.superior_id,l.operational_authority,(SELECT role FROM users WHERE id=l.user_id),
          COALESCE((SELECT MIN(p.valid_from) FROM employee_placements p WHERE p.user_id=l.user_id),
                   (SELECT substr(prepared_at,1,10) FROM local_preparation WHERE id=1),date('now')),
          l.provisional,l.source_json,datetime('now') FROM local_staff_profiles l''')
    cx.execute('CREATE UNIQUE INDEX organization_authority_current ON organization_authority_history(user_id) WHERE valid_to IS NULL')
    cx.execute('''CREATE TABLE organization_revisions (
        id INTEGER PRIMARY KEY,entity TEXT NOT NULL,entity_id INTEGER NOT NULL,effective_date TEXT NOT NULL,
        before_json TEXT NOT NULL,after_json TEXT NOT NULL,actor_id INTEGER REFERENCES users(id),
        recorded_at TEXT NOT NULL)''')
    cx.execute('''CREATE TABLE request_authority_plans (
        request_id INTEGER PRIMARY KEY REFERENCES requests(id),plan_json TEXT NOT NULL,created_at TEXT NOT NULL)''')
    cx.execute('''CREATE TABLE approval_authority_evidence (
        step_id INTEGER PRIMARY KEY REFERENCES request_approval_steps(id),actor_id INTEGER NOT NULL REFERENCES users(id),
        decided_at TEXT NOT NULL,decision TEXT NOT NULL,evidence_json TEXT NOT NULL)''')
    for table in ('request_authority_plans','approval_authority_evidence','organization_revisions'):
        for action in ('UPDATE','DELETE'):
            cx.execute(f'''CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'La evidencia histórica es inmutable'); END''')
    for table in ('employee_placements','area_responsibles'):
        for action in ('INSERT','UPDATE'):
            cx.execute(f'''CREATE TRIGGER safe_{table}_{action.lower()} BEFORE {action} ON {table}
                WHEN NEW.valid_to IS NOT NULL AND NEW.valid_to<NEW.valid_from
                BEGIN SELECT RAISE(ABORT,'Vigencia invertida'); END''')
        key="p.user_id=NEW.user_id" if table=='employee_placements' else "p.area_id=NEW.area_id AND p.role=NEW.role"
        for action in ('INSERT','UPDATE'):
            cx.execute(f'''CREATE TRIGGER overlap_{table}_{action.lower()} BEFORE {action} ON {table}
                WHEN EXISTS(SELECT 1 FROM {table} p WHERE {key} AND p.id<>COALESCE(NEW.id,-1)
                    AND p.valid_from<=COALESCE(NEW.valid_to,'9999-12-31')
                    AND COALESCE(p.valid_to,'9999-12-31')>=NEW.valid_from)
                BEGIN SELECT RAISE(ABORT,'Vigencias incompatibles superpuestas'); END''')
    for action in ('INSERT','UPDATE'):
        cx.execute(f'''CREATE TRIGGER placement_no_self_{action.lower()} BEFORE {action} ON employee_placements
            WHEN NEW.supervisor_id=NEW.user_id BEGIN SELECT RAISE(ABORT,'Autosupervisión no permitida'); END''')


MIGRATIONS = (
    (1, _organization),
    (2, _approval_stages),
    (3, _closure_history_and_notifications),
    (4, _kimtek_and_notification_deduplication),
    (5, _request_history_snapshots),
    (6, _attendance_reconciliation),
    (7, _attendance_system_actor),
    (8, _biometric_identity_history),
    (9, _reconciliation_report_link),
    (10, _biometric_stable_event_key),
    (11, _biometric_roster_review),
    (12, _attendance_accuracy_stage_one),
    (13, _continuous_attendance),
    (14, _logical_punch_groups),
    (15, _local_preparation_metadata),
    (16, _organizational_authority),
)


def apply_migrations(cx: sqlite3.Connection) -> None:
    cx.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                  )''')
    current = cx.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
    for version, migration in MIGRATIONS:
        if version <= current:
            continue
        if version in (1, 7):
            cx.execute('PRAGMA foreign_keys=OFF')
        try:
            cx.execute('BEGIN IMMEDIATE')
            migration(cx)
            cx.execute('INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)',
                       (version, datetime.now().isoformat(timespec='seconds')))
            violations = cx.execute('PRAGMA foreign_key_check').fetchall()
            if violations:
                raise sqlite3.IntegrityError(f'Foreign key violations after migration {version}: {violations}')
            cx.commit()
        except Exception:
            cx.rollback()
            raise
        finally:
            if version in (1, 7):
                cx.execute('PRAGMA foreign_keys=ON')
