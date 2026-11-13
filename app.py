"""Control de Asistencia y Boletas. Prototipo funcional local (no producción)."""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import unicodedata
import uuid
import organization
import access_control
from approval_authority import plan_request, record_decision
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil import parser
from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from openpyxl import load_workbook

from core import (BASE, DB_PATH, LEAVE_TYPES, STATUS_LABELS, TZ, audit,
                  auto_close_if_due, blocked_range, choose_approver, close_period, db,
                  daily_status, get_report, hash_password, initialize, may_approve,
                  month_end_for_day, now, nowstr, notify, period_start, reopen_period, report,
                  verify_password)
from documents import excel_report, pdf_boleta, pdf_boletas
from roster import confirm_roster_identity
from reconciliation import (import_zlink, latest_import_summary, pending_identities,
                            parse_zlink_xlsx, preview_zlink, reconcile)

COMPANY = 'KIMTEK'
COOKIE_SECRET = os.environ.get('SESSION_SECRET') or secrets.token_urlsafe(40)

@asynccontextmanager
async def lifespan(application):
    initialize()
    tasks=[asyncio.create_task(periodic_notification_task())]
    if os.environ.get('AUTO_CLOSE')=='1':
        tasks.append(asyncio.create_task(periodic_close_task()))
    try:
        yield
    finally:
        for task in tasks: task.cancel()
        for task in tasks:
            try: await task
            except asyncio.CancelledError: pass

app = FastAPI(title='Control de Asistencia y Boletas de Permiso', docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=COOKIE_SECRET,
                   session_cookie='asis_session',
                   same_site='lax', max_age=3600*8, https_only=os.environ.get('HTTPS_ONLY')=='1')
app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
templates=Jinja2Templates(directory=BASE/'templates')

MONTHS_ES=('ENERO','FEBRERO','MARZO','ABRIL','MAYO','JUNIO',
           'JULIO','AGOSTO','SEPTIEMBRE','OCTUBRE','NOVIEMBRE','DICIEMBRE')


def visible_date(value):
    try:
        if isinstance(value,datetime):
            return value.strftime('%d/%m/%Y')
        if isinstance(value,date):
            return value.strftime('%d/%m/%Y')
        raw=str(value)
        parsed=datetime.fromisoformat(raw.replace('Z','+00:00')) if 'T' in raw or ' ' in raw else date.fromisoformat(raw)
        return parsed.strftime('%d/%m/%Y')
    except (ValueError,TypeError):
        return value or '—'


def visible_datetime(value):
    try:
        parsed=value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
        return parsed.strftime('%d/%m/%Y %H:%M')
    except (ValueError,TypeError):
        return visible_date(value)


def period_label(start,end):
    first=date.fromisoformat(str(start)[:10])
    last=date.fromisoformat(str(end)[:10])
    left=MONTHS_ES[first.month-1]
    right=MONTHS_ES[last.month-1]
    if first.year != last.year:
        left+=f' {first.year}'
        right+=f' {last.year}'
    return f'{left} → {right}'


templates.env.filters['fecha']=visible_date
templates.env.filters['fecha_hora']=visible_datetime
def visible_datetime_seconds(value):
    try:return datetime.fromisoformat(str(value)).strftime('%d/%m/%Y %H:%M:%S')
    except (ValueError,TypeError):return value or '—'


templates.env.filters['fecha_hora_segundos']=visible_datetime_seconds

async def periodic_close_task():
    while True:
        try:
            auto_close_if_due()
        except Exception as exc:
            print('Aviso cierre automático:',str(exc),file=sys.stderr)
        await asyncio.sleep(60)


async def periodic_notification_task():
    while True:
        try:
            notify_preclose_if_due()
        except Exception as exc:
            print('Aviso de precierre:',str(exc),file=sys.stderr)
        await asyncio.sleep(3600)


def notify_preclose_if_due():
    timestamp=now()
    if timestamp.day!=25 or timestamp.hour<8:
        return
    end=date(timestamp.year,timestamp.month,25)
    with db() as cx:
        closed=cx.execute("SELECT 1 FROM monthly_closures WHERE period_end=? AND state='CERRADO'",(end.isoformat(),)).fetchone()
        if closed:return
        for recipient in cx.execute("SELECT id FROM users WHERE role='hr' AND active=1"):
            notify(cx,recipient['id'],'PRECierre',
                   f'Revisa incidencias y asistencia antes del cierre del periodo {period_start(end).strftime("%d/%m/%Y")} al {end.strftime("%d/%m/%Y")}.',
                   'periodo',end.isoformat(),f'precierre:{end.isoformat()}')


def actor_for(cx,request:Request):
    uid=request.session.get('uid')
    if not uid:return None
    row=cx.execute('''SELECT u.*, COALESCE(a.name,'KIMTEK') AS area_name FROM users u
       LEFT JOIN areas a ON a.id=u.area_id WHERE u.id=? AND u.active=1''',(uid,)).fetchone()
    return row


def require_actor(cx,request):
    a=actor_for(cx,request)
    if a is None:raise HTTPException(status_code=401,detail='Primero inicia sesión.')
    caps=access_control.capabilities(cx,a)
    route=request.scope.get('route')
    if route and not access_control.endpoint_allowed(caps,route.name):
        raise HTTPException(403,'Tu función no tiene autorización para esta operación.')
    return a


def require_hr(actor):
    if actor['role'] not in ('hr','admin'):
        raise HTTPException(status_code=403, detail='Solo Recursos Humanos / Administracion.')


def require_personnel_hr(actor):
    if actor['role']!='hr':
        raise HTTPException(status_code=403,detail='Solo Recursos Humanos puede modificar la estructura y los datos laborales.')


def csrf_token(request):
    token=request.session.get('csrf')
    if not token:
        token=secrets.token_urlsafe(24);request.session['csrf']=token
    return token


def check_csrf(request, form):
    token=request.session.get('csrf')
    supplied=str(form.get('csrf',''))
    if not token or not secrets.compare_digest(token,supplied):
        raise HTTPException(status_code=403,detail='Validación del formulario fallida.')


def redirect(target='/', message=None):
    # flash is passed separately by route to avoid setting unsaved sessions
    return RedirectResponse(target,status_code=303)


def flash(request,message,success=True):
    request.session['flash']={'message':message,'ok':success}


def page(request,name,actor=None,**ctx):
    thisperiod=month_end_for_day(now().date())
    periodfrom=period_start(thisperiod)
    unread=0
    caps=frozenset()
    if actor:
        with db() as cx:
            caps=access_control.capabilities(cx,actor)
            unread=cx.execute('SELECT COUNT(*) FROM internal_notifications WHERE user_id=? AND read_at IS NULL',
                              (actor['id'],)).fetchone()[0]
    params={'request':request,'actor':actor,'csrf':csrf_token(request),'unread_notifications':unread,
     'flash':request.session.pop('flash',None),'company':COMPANY,'capabilities':caps,
     'thisperiod':thisperiod.isoformat(),'today':now().date().isoformat(),
     'period_label':period_label(periodfrom,thisperiod),
     'leave_types':LEAVE_TYPES,'status_labels':STATUS_LABELS,
     }
    params.update(ctx)
    return templates.TemplateResponse(request,name,params)

@app.get('/login',response_class=HTMLResponse)
async def login_page(request:Request):
    with db() as cx:
        if actor_for(cx,request):return redirect('/')
    return page(request,'login.html')









@app.post('/login')
async def login_post(request:Request):
    form=await request.form();check_csrf(request,form)
    email=str(form.get('email','')).strip().lower()
    password=str(form.get('password',''))
    with db() as cx:
        user=cx.execute('SELECT * FROM users WHERE lower(email)=? AND active=1',(email,)).fetchone()
        if not user or not verify_password(password,user['password_hash']):
            flash(request,'Correo o contraseña incorrectos.',False)
            return redirect('/login')
        request.session.clear()
        request.session['uid']=user['id'];request.session['csrf']=secrets.token_urlsafe(24)
        audit(cx,user['id'],'LOGIN','sesion',user['id'],'Inicio de sesión')
    return redirect('/')

@app.post('/logout')
async def logout_post(request:Request):
    form=await request.form();check_csrf(request,form)
    request.session.clear()
    return redirect('/login')

@app.get('/',response_class=HTMLResponse)
async def home(request:Request):
    with db() as cx:
        a=actor_for(cx,request)
        if not a:return redirect('/login')
        today=now().date()
        yesterday=today-timedelta(days=1)
        yesterdayrow=daily_status(cx,a,yesterday)
        my_reqs=cx.execute('''SELECT r.*,u.name AS approver_name FROM requests r JOIN users u ON u.id=r.approver_id
                               WHERE r.user_id=? ORDER BY r.id DESC LIMIT 5''',(a['id'],)).fetchall()
        if 'hr.global' in access_control.capabilities(cx,a):
            pending=cx.execute('SELECT COUNT(*) n FROM requests WHERE status="PENDIENTE"').fetchone()['n']
            personal=cx.execute("SELECT COUNT(*) n FROM users u WHERE active=1 AND NOT EXISTS(SELECT 1 FROM local_staff_profiles p WHERE p.user_id=u.id AND p.account_kind='OPERATIVA')").fetchone()['n']
        elif a['role']=='manager':
            pending=cx.execute('''SELECT COUNT(*) FROM request_approval_steps s
                JOIN requests r ON r.id=s.request_id
                WHERE r.status='PENDIENTE' AND s.assignee_id=? AND s.assignee_role='manager'
                  AND s.decision IS NULL AND s.step_order=(SELECT MIN(s2.step_order)
                    FROM request_approval_steps s2 WHERE s2.request_id=r.id AND s2.decision IS NULL)''',
                (a['id'],)).fetchone()[0]
            personal=sum(organization.manager_scope(cx,a['id'],u['id'],today)
                         for u in cx.execute('SELECT id FROM users'))
        else:
            pending=cx.execute('SELECT COUNT(*) n FROM requests WHERE status="PENDIENTE" AND user_id=?',(a['id'],)).fetchone()['n']
            personal=None
        periodfrom=period_start(month_end_for_day(today))
        return page(request,'dashboard.html',a,yesterdayrow=yesterdayrow,my_reqs=my_reqs,pending=pending,
                personal=personal,period_from=periodfrom.isoformat())

@app.get('/mis-solicitudes',response_class=HTMLResponse)
async def my_requests(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        rows=cx.execute('''SELECT r.*,u.name AS approver_name,
            (SELECT s.title FROM request_approval_steps s WHERE s.request_id=r.id
             AND s.decision IS NULL AND s.is_skipped=0 ORDER BY s.step_order LIMIT 1) AS current_step,
            (SELECT s.decision FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) AS manager_decision,
            (SELECT s.decided_at FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) AS manager_decided_at,
            (SELECT s.note FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) AS manager_note,
            (SELECT s.decision FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) AS hr_decision,
            (SELECT s.decided_at FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) AS hr_decided_at,
            (SELECT s.note FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) AS hr_note
            ,(SELECT COALESCE(NULLIF(s.assignee_name_snapshot,''),u.name) FROM request_approval_steps s LEFT JOIN users u ON u.id=s.assignee_id WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) AS manager_name
            ,(SELECT COALESCE(NULLIF(s.assignee_name_snapshot,''),u.name) FROM request_approval_steps s LEFT JOIN users u ON u.id=s.assignee_id WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) AS hr_name
            FROM requests r JOIN users u ON u.id=r.approver_id
            WHERE r.user_id=? ORDER BY r.id DESC''',(a['id'],)).fetchall()
        return page(request,'my_requests.html',a,rows=rows)

@app.get('/solicitar',response_class=HTMLResponse)
async def request_page(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        issue=''
        approver_name=None
        try: approver_name=choose_approver(cx,a,'manager')['name']
        except ValueError as exc: issue=str(exc)
        return page(request,'request_form.html',a,submission_issue=issue,submission_approver=approver_name)


def canonical_dt(value:str):
    try:return datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError):raise ValueError('Fecha y hora invalida.')


def create_approval_steps(cx,request_id,kind,manager_id):
    flow=cx.execute('''SELECT id FROM approval_flows
        WHERE request_kind=? AND active=1 AND company_id IS NULL AND branch_id IS NULL
        ORDER BY id LIMIT 1''',(kind,)).fetchone()
    if not flow:
        raise ValueError('No existe un flujo de aprobación activo para este trámite.')
    steps=cx.execute('''SELECT step_order,title,assignee_role FROM approval_flow_steps
        WHERE flow_id=? AND required=1 ORDER BY step_order''',(flow['id'],)).fetchall()
    if not steps:
        raise ValueError('El flujo de aprobación no tiene etapas obligatorias.')
    approver=cx.execute('SELECT role,name FROM users WHERE id=?',(manager_id,)).fetchone()
    plan=plan_request(cx,request_id,manager_id)
    for step in steps:
        skipped=int(step['assignee_role']=='manager' and plan['manager_skipped'])
        assignee_id=manager_id if (step['assignee_role']=='manager' and not skipped) or (step['assignee_role']=='hr' and plan['manager_skipped']) else None
        cx.execute("""INSERT INTO request_approval_steps
            (request_id,step_order,title,assignee_role,assignee_id,assignee_name_snapshot,is_skipped,skip_reason)
            VALUES(?,?,?,?,?,?,?,?)""",
            (request_id,step['step_order'],step['title'],step['assignee_role'],assignee_id,
             approver['name'] if assignee_id else '',skipped,
             'Superior operativo directo acreditado en el plan; procedimiento local provisional.' if skipped else ''))


def snapshot_request_employee(cx,request_id,user_id,submitted_at):
    person=cx.execute('''SELECT u.name,u.employee_code,u.position,
        b.name branch_name,a.name area_name FROM users u
        LEFT JOIN employee_placements p ON p.id=(SELECT p2.id FROM employee_placements p2
          WHERE p2.user_id=u.id AND p2.valid_from<=substr(?,1,10)
            AND (p2.valid_to IS NULL OR p2.valid_to>=substr(?,1,10))
          ORDER BY p2.valid_from DESC,p2.id DESC LIMIT 1)
        LEFT JOIN branches b ON b.id=p.branch_id LEFT JOIN areas a ON a.id=COALESCE(p.area_id,u.area_id)
        WHERE u.id=?''',(submitted_at,submitted_at,user_id)).fetchone()
    cx.execute('''UPDATE requests SET employee_name_snapshot=?,employee_code_snapshot=?,
        employee_position_snapshot=?,branch_name_snapshot=?,area_name_snapshot=? WHERE id=?''',
        (person['name'],person['employee_code'],person['position'],person['branch_name'] or '',person['area_name'] or '',request_id))


@app.post('/solicitar')
async def request_post(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request)
        kind=str(form.get('kind','PERMISO')).upper()
        if kind not in ('PERMISO','CORRECCION'):
            raise HTTPException(400,'Tipo de solicitud no admitido.')
        justification=str(form.get('justification','')).strip()
        if len(justification)<12 or len(justification)>1800:
            flash(request,'La justificación debe tener entre 12 y 1800 caracteres.',False)
            return redirect('/solicitar')
        cx.execute('SAVEPOINT route_change')
        try:
            approver=choose_approver(cx,a,'manager')
            if kind=='PERMISO':
                leave_type=str(form.get('leave_type','PERSONAL'))
                if leave_type not in LEAVE_TYPES:raise ValueError('Motivo de permiso no admitido.')
                start=canonical_dt(str(form.get('starts_at','')))
                end=canonical_dt(str(form.get('ends_at','')))
                begind=datetime.fromisoformat(start);finishd=datetime.fromisoformat(end)
                if end<=start or finishd-begind>timedelta(days=31):
                    raise ValueError('Verifica el inicio y fin; no se permiten mas de 31 dias.')
                if blocked_range(cx,begind.date(),finishd.date()):
                    raise ValueError('El periodo esta cerrado; RR. HH. debe autorizar su reapertura.')
                place=str(form.get('place','')).strip()[:200]
                contact=str(form.get('contact','')).strip()[:100]
                paytype=str(form.get('pay_type','CON_GOCE'))
                if paytype not in ('CON_GOCE','SIN_GOCE'):raise ValueError('Modalidad de goce invalida.')
                cur=cx.execute('''INSERT INTO requests(user_id,approver_id,kind,leave_type,starts_at,ends_at,
                    place,contact,pay_type,justification,submitted_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                    (a['id'],approver['id'],'PERMISO',leave_type,start,end,place,contact,paytype,
                     justification,nowstr()))
                prefix='BP'
            else:
                correction_kind=str(form.get('correction_kind','')).upper()
                if correction_kind not in ('IN','OUT'):raise ValueError('Debe indicar Entrada o Salida.')
                correction_at=canonical_dt(str(form.get('correction_at','')))
                when=datetime.fromisoformat(correction_at)
                if when.date()>now().date():raise ValueError('No se corrige una fecha futura.')
                if blocked_range(cx,when.date(),when.date()):
                    raise ValueError('No se puede corregir un periodo cerrado.')
                cur=cx.execute('''INSERT INTO requests(user_id,approver_id,kind,correction_at,correction_kind,
                   justification,submitted_at) VALUES(?,?,?,?,?,?,?)''',
                   (a['id'],approver['id'],'CORRECCION',correction_at,correction_kind,justification,nowstr()))
                prefix='CM'
            rid=cur.lastrowid
            folio=f'{prefix}-{now().year}-{rid:06d}'
            cx.execute('UPDATE requests SET folio=? WHERE id=?',(folio,rid))
            snapshot_request_employee(cx,rid,a['id'],nowstr())
            create_approval_steps(cx,rid,kind,approver['id'])
            notify(cx,approver['id'],'SOLICITUD_RECIBIDA',
                   f'{a["name"]} envió la solicitud {folio} para tu revisión.',
                   'solicitud',rid)
            audit(cx,a['id'],'SOLICITUD','solicitud',rid,f'{kind} -> {approver["name"]}')
        except ValueError as exc:
            cx.execute('ROLLBACK TO route_change')
            flash(request,str(exc),False)
            return redirect('/solicitar')
        finally:
            cx.execute('RELEASE route_change')
        flash(request,f'Solicitud {folio} enviada al flujo autorizado; primera revisión: {approver["name"]}.')
    return redirect('/mis-solicitudes')

@app.get('/aprobaciones',response_class=HTMLResponse)
async def review_page(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        if a['role'] not in ('manager','hr','admin'):raise HTTPException(403,'No tienes rol de aprobación.')
        where='r.status="PENDIENTE" AND r.user_id<>? AND s.decision IS NULL AND s.is_skipped=0'
        params=[a['id']]
        if a['role']=='manager':
            where+=' AND s.assignee_role="manager" AND s.assignee_id=?'
            params.append(a['id'])
        elif a['role']=='hr':
            where+=' AND s.assignee_role="hr" AND (s.assignee_id IS NULL OR s.assignee_id=?)'
            params.append(a['id'])
        else:
            where+=' AND s.assignee_role="hr" AND s.assignee_id=?'
            params.append(a['id'])
        rows=cx.execute('''SELECT r.*,u.name AS employee_name,u.employee_code,u.position,
                b.name AS approver_name,d.name AS area_name,s.id AS approval_step_id,
                s.step_order,s.title AS step_title FROM requests r
                JOIN users u ON u.id=r.user_id JOIN areas d ON d.id=u.area_id
                JOIN request_approval_steps s ON s.request_id=r.id
                LEFT JOIN users b ON b.id=s.assignee_id WHERE '''+where+'''
                AND s.step_order=(SELECT MIN(s2.step_order) FROM request_approval_steps s2
                                  WHERE s2.request_id=r.id AND s2.decision IS NULL AND s2.is_skipped=0)
                ORDER BY r.id DESC''',params).fetchall()
        rows=[r for r in rows if may_approve(cx,a,r)]
        return page(request,'approvals.html',a,rows=rows)

@app.post('/aprobaciones/{request_id}/resolver')
async def resolve(request:Request,request_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request)
        if a['role'] not in ('manager','hr','admin'):
            raise HTTPException(403,'Solo encargados asignados y Recursos Humanos pueden aprobar.')
        row=cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
        if not row:raise HTTPException(404,'Solicitud no encontrada.')
        step=cx.execute('''SELECT * FROM request_approval_steps
            WHERE request_id=? AND decision IS NULL AND is_skipped=0
            ORDER BY step_order LIMIT 1''',(request_id,)).fetchone()
        if not step or not may_approve(cx,a,row):
            raise HTTPException(403,'No puedes aprobar esta etapa de la solicitud.')
        if row['status']!='PENDIENTE':
            flash(request,'Esta solicitud ya fue resuelta.',False);return redirect('/aprobaciones')
        decision=str(form.get('decision','')).upper()
        if decision not in ('APROBADA','RECHAZADA'):raise HTTPException(400,'Decisión invalida.')
        note=str(form.get('note','')).strip()[:800]
        if decision=='RECHAZADA' and len(note)<8:
            flash(request,'Para rechazar, explica el motivo con al menos 8 caracteres.',False)
            return redirect('/aprobaciones')
        if row['kind']=='PERMISO':
            first=date.fromisoformat(row['starts_at'][:10]);last=date.fromisoformat(row['ends_at'][:10])
        else:
            first=last=date.fromisoformat(row['correction_at'][:10])
        if blocked_range(cx,first,last):
            flash(request,'El periodo fue cerrado. Solicita reapertura antes de resolver.',False)
            return redirect('/aprobaciones')
        stamp=nowstr()
        record_decision(cx,a,row,step,decision,stamp)
        changed=cx.execute('''UPDATE request_approval_steps
            SET assignee_id=?,assignee_name_snapshot=?,decision=?,decided_at=?,note=?
            WHERE id=? AND decision IS NULL''',
            (a['id'],a['name'],decision,stamp,note,step['id'])).rowcount
        if changed != 1:
            raise HTTPException(409,'La etapa ya fue resuelta por otro usuario.')
        if decision=='RECHAZADA':
            cx.execute('''UPDATE request_approval_steps SET is_skipped=1,skip_reason=?
                WHERE request_id=? AND step_order>? AND decision IS NULL AND is_skipped=0''',
                (f'Etapa no ejecutada: rechazo en {step["title"]}.',request_id,step['step_order']))
        next_step=cx.execute('''SELECT * FROM request_approval_steps
            WHERE request_id=? AND decision IS NULL AND is_skipped=0
            ORDER BY step_order LIMIT 1''',(request_id,)).fetchone()
        if decision=='RECHAZADA' or not next_step:
            cx.execute('''UPDATE requests SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?
                          AND status='PENDIENTE' ''',
                       ('RECHAZADA' if decision=='RECHAZADA' else 'APROBADA',
                        a['id'],stamp,note,request_id))
        audit(cx,a['id'],'DECISION','solicitud',request_id,
              f'Etapa {step["step_order"]} ({step["title"]}): {decision}; {note}')
        if decision=='RECHAZADA':
            message=f'{row["folio"]} rechazada en {step["title"]}.'
            notify(cx,row['user_id'],'SOLICITUD_RECHAZADA',message,'solicitud',request_id)
            if step['assignee_role']=='hr':
                manager_step=cx.execute("SELECT assignee_id FROM request_approval_steps WHERE request_id=? AND assignee_role='manager' AND decision='APROBADA' ORDER BY step_order LIMIT 1",(request_id,)).fetchone()
                if manager_step and manager_step['assignee_id'] and manager_step['assignee_id']!=a['id']:
                    notify(cx,manager_step['assignee_id'],'SOLICITUD_RECHAZADA',f'{row["folio"]} fue rechazada definitivamente por Recursos Humanos.','solicitud',request_id)
        elif next_step:
            message=f'{row["folio"]} aprobada por esta etapa; pasa a {next_step["title"]}.'
            if next_step['assignee_id']:
                notify(cx,next_step['assignee_id'],'SOLICITUD_PENDIENTE',
                       f'{row["folio"]} requiere tu revisión en {next_step["title"]}.',
                       'solicitud',request_id)
            elif next_step['assignee_role']=='hr':
                for recipient in cx.execute("SELECT id FROM users WHERE active=1 AND role='hr'"):
                    notify(cx,recipient['id'],'SOLICITUD_PENDIENTE',
                           f'{row["folio"]} requiere aprobación de Recursos Humanos.',
                           'solicitud',request_id)
            notify(cx,row['user_id'],'SOLICITUD_AVANZA',message,'solicitud',request_id)
        else:
            message=f'{row["folio"]} aprobada definitivamente. '+(
                'Boleta PDF disponible.' if row['kind']=='PERMISO' else '')
            notify(cx,row['user_id'],'SOLICITUD_APROBADA',message,
                   'boleta' if row['kind']=='PERMISO' else 'solicitud',request_id)
            if step['assignee_role']=='hr':
                manager_step=cx.execute("SELECT assignee_id FROM request_approval_steps WHERE request_id=? AND assignee_role='manager' AND decision='APROBADA' ORDER BY step_order LIMIT 1",(request_id,)).fetchone()
                if manager_step and manager_step['assignee_id'] and manager_step['assignee_id'] not in (a['id'],row['user_id']):
                    notify(cx,manager_step['assignee_id'],'SOLICITUD_APROBADA',f'{row["folio"]} fue aprobada definitivamente por Recursos Humanos.','boleta' if row['kind']=='PERMISO' else 'solicitud',request_id)
        from reconciliation import enqueue_request_recalculation
        enqueue_request_recalculation(cx,row)
        flash(request,message)
    return redirect('/aprobaciones')

@app.get('/boleta/{request_id}.pdf')
async def download_boleta(request:Request,request_id:int):
    with db() as cx:
        a=require_actor(cx,request)
        r=cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
        if not r:raise HTTPException(404,'No existe.')
        if not access_control.can_view_request(cx,a,r):
            raise HTTPException(403,'No autorizado.')
        if not approved_permit(cx,r):
            raise HTTPException(400,'El PDF se genera unicamente para permisos aprobados.')
        item=load_boleta_data(cx,request_id)
        try:pdf=pdf_boleta(item['request'],item['employee'],item['approver'],COMPANY,item['approvals'])
        except ValueError as exc:raise HTTPException(422,str(exc))
        audit(cx,a['id'],'GENERACION_PDF','solicitud',request_id,r['folio'])
        audit(cx,a['id'],'DESCARGA_PDF','solicitud',request_id,r['folio'])
        return StreamingResponse(io.BytesIO(pdf),media_type='application/pdf',headers={
          'Content-Disposition':f'attachment; filename="Boleta_Permiso_{r["folio"]}.pdf"'})


def approved_permit(cx,row):
    from attendance_authorizations import authorization
    return bool(row and row['kind']=='PERMISO' and authorization(cx,row)['valid'])


def manager_can_access_request(cx,manager_id,request_id):
    r=cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone()
    actor=cx.execute('SELECT * FROM users WHERE id=?',(manager_id,)).fetchone()
    return bool(r and actor and access_control.can_view_request(cx,actor,r))


def load_boleta_data(cx,request_id):
    request_row=dict(cx.execute('SELECT * FROM requests WHERE id=?',(request_id,)).fetchone())
    employee=cx.execute('''SELECT u.id,u.document_id,
        COALESCE(NULLIF(r.employee_name_snapshot,''),u.name) name,
        COALESCE(NULLIF(r.employee_code_snapshot,''),u.employee_code) employee_code,
        COALESCE(NULLIF(r.employee_position_snapshot,''),u.position) position,
        COALESCE(NULLIF(r.branch_name_snapshot,''),b.name,'') branch_name,
        COALESCE(NULLIF(r.area_name_snapshot,''),a.name,'') area_name,
        p.company_id,p.branch_id,c.name AS company_name FROM users u
        JOIN requests r ON r.id=?
        LEFT JOIN employee_placements p ON p.id=(SELECT p2.id FROM employee_placements p2
          WHERE p2.user_id=u.id AND p2.valid_from<=substr(r.submitted_at,1,10)
            AND (p2.valid_to IS NULL OR p2.valid_to>=substr(r.submitted_at,1,10))
          ORDER BY p2.valid_from DESC,p2.id DESC LIMIT 1)
        LEFT JOIN areas a ON a.id=COALESCE(p.area_id,u.area_id)
        LEFT JOIN companies c ON c.id=p.company_id LEFT JOIN branches b ON b.id=p.branch_id
        WHERE u.id=r.user_id''',(request_id,)).fetchone()
    approvals=cx.execute('''SELECT s.*,COALESCE(NULLIF(s.assignee_name_snapshot,''),u.name) AS approver_name,u.position AS approver_position
        FROM request_approval_steps s LEFT JOIN users u ON u.id=s.assignee_id
        WHERE s.request_id=? ORDER BY s.step_order''',(request_id,)).fetchall()
    manager_step=next((s for s in approvals if s['assignee_role']=='manager'),None)
    hr_step=next((s for s in approvals if s['assignee_role']=='hr'),None)
    final_id=hr_step['assignee_id'] if hr_step and hr_step['assignee_id'] else request_row['decided_by']
    approver=cx.execute('SELECT * FROM users WHERE id=?',(final_id,)).fetchone() if final_id else None
    return {'request':request_row,'employee':dict(employee),'approver':dict(approver) if approver else {},
            'approvals':[dict(s) for s in approvals]}


HISTORY_FROM='''FROM requests r JOIN users u ON u.id=r.user_id
 LEFT JOIN employee_placements p ON p.id=(SELECT p2.id FROM employee_placements p2
   WHERE p2.user_id=r.user_id AND p2.valid_from<=substr(r.submitted_at,1,10)
     AND (p2.valid_to IS NULL OR p2.valid_to>=substr(r.submitted_at,1,10))
   ORDER BY p2.valid_from DESC,p2.id DESC LIMIT 1)
 LEFT JOIN branches b ON b.id=p.branch_id LEFT JOIN areas ar ON ar.id=COALESCE(p.area_id,u.area_id)
 LEFT JOIN users m ON m.id=(SELECT s.assignee_id FROM request_approval_steps s WHERE s.request_id=r.id
   AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1)
 LEFT JOIN users h ON h.id=(SELECT s.assignee_id FROM request_approval_steps s WHERE s.request_id=r.id
   AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1)'''
HISTORY_SELECT='''SELECT r.*,COALESCE(NULLIF(r.employee_name_snapshot,''),u.name) employee_name,
 COALESCE(NULLIF(r.employee_code_snapshot,''),u.employee_code) employee_code,
 COALESCE(NULLIF(r.employee_position_snapshot,''),u.position) position,
 COALESCE(NULLIF(r.branch_name_snapshot,''),b.name) branch_name,
 COALESCE(NULLIF(r.area_name_snapshot,''),ar.name) area_name,
 COALESCE(NULLIF((SELECT s.assignee_name_snapshot FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1),''),m.name) manager_name,
 COALESCE(NULLIF((SELECT s.assignee_name_snapshot FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1),''),h.name) hr_name,
 (SELECT s.decision FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) manager_decision,
 (SELECT s.decided_at FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) manager_decided_at,
 (SELECT s.note FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' ORDER BY s.step_order LIMIT 1) manager_note,
 (SELECT s.decision FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) hr_decision,
 (SELECT s.decided_at FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) hr_decided_at,
 (SELECT s.note FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='hr' ORDER BY s.step_order LIMIT 1) hr_note,
 (SELECT s.title FROM request_approval_steps s WHERE s.request_id=r.id AND s.decision IS NULL AND s.is_skipped=0 ORDER BY s.step_order LIMIT 1) current_step '''


def request_status_label(r):
    if r['status']=='APROBADA':
        from attendance_authorizations import authorization
        with db() as cx:
            original=cx.execute('SELECT * FROM requests WHERE id=?',(r['id'],)).fetchone()
            if original and not authorization(cx,original)['valid']:return 'APROBACIÓN HISTÓRICA NO VERIFICADA'
    if r['status']=='APROBADA':return 'APROBADA DEFINITIVAMENTE'
    if r['status']=='RECHAZADA':
        return 'RECHAZADA POR ENCARGADO' if r['manager_decision']=='RECHAZADA' else 'RECHAZADA POR RR. HH.'
    return 'PENDIENTE DE '+(r['current_step'] or 'REVISIÓN')


templates.env.globals['request_status_label']=request_status_label


def period_bounds(value):
    if not value:return None
    try:
        year,month=(int(part) for part in value.split('-',1))
        end=date(year,month,25)
        start=date(year-1,12,26) if month==1 else date(year,month-1,26)
        return start.isoformat(),end.isoformat()
    except (ValueError,TypeError):raise HTTPException(400,'Período inválido; usa AAAA-MM.')


def history_filters(query,params,*,hr=False,boletas=False):
    if query.get('employee'):
        term='%'+query['employee'].strip()+'%'
        params.extend([term,term]);query_sql=' AND (u.name LIKE ? OR u.employee_code LIKE ?)'
    else:query_sql=''
    if query.get('q'):
        term='%'+query['q'].strip()+'%';params.extend([term,term,term])
        query_sql+=' AND (u.name LIKE ? OR u.employee_code LIKE ? OR r.folio LIKE ?)'
    for key,col in [('branch_id','p.branch_id'),('area_id','COALESCE(p.area_id,u.area_id)')]:
        if query.get(key):
            try:params.append(int(query[key]))
            except ValueError:raise HTTPException(400,'Filtro organizacional inválido.')
            query_sql+=f' AND {col}=?'
    if query.get('kind') in ('PERMISO','CORRECCION'):
        query_sql+=' AND r.kind=?';params.append(query['kind'])
    elif query.get('kind') in LEAVE_TYPES:
        query_sql+=' AND r.kind=\'PERMISO\' AND r.leave_type=?';params.append(query['kind'])
    if query.get('manager_id'):
        try:manager_id=int(query['manager_id'])
        except ValueError:raise HTTPException(400,'Encargado inválido.')
        query_sql+=" AND EXISTS(SELECT 1 FROM request_approval_steps sm WHERE sm.request_id=r.id AND sm.assignee_role='manager' AND sm.assignee_id=?)";params.append(manager_id)
    if query.get('from'):
        try:date.fromisoformat(query['from'])
        except ValueError:raise HTTPException(400,'Fecha desde inválida.')
        query_sql+=' AND substr(COALESCE(NULLIF(r.starts_at,\'\'),NULLIF(r.correction_at,\'\'),r.submitted_at),1,10)>=?';params.append(query['from'])
    if query.get('to'):
        try:date.fromisoformat(query['to'])
        except ValueError:raise HTTPException(400,'Fecha hasta inválida.')
        query_sql+=' AND substr(COALESCE(NULLIF(r.starts_at,\'\'),NULLIF(r.correction_at,\'\'),r.submitted_at),1,10)<=?';params.append(query['to'])
    bounds=period_bounds(query.get('period'))
    if bounds:
        query_sql+=" AND substr(COALESCE(NULLIF(r.starts_at,''),NULLIF(r.correction_at,''),r.submitted_at),1,10) BETWEEN ? AND ?";params.extend(bounds)
    if boletas:
        query_sql+=" AND r.kind='PERMISO' AND r.status='APROBADA' AND NOT EXISTS(SELECT 1 FROM request_approval_steps sx WHERE sx.request_id=r.id AND sx.is_skipped=0 AND (sx.decision IS NULL OR sx.decision<>'APROBADA'))"
    if hr and query.get('status'):
        expressions={
          'PENDIENTE_ENCARGADO':"r.status='PENDIENTE' AND EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.decision IS NULL AND s.is_skipped=0 AND s.assignee_role='manager')",
          'PENDIENTE_RRHH':"r.status='PENDIENTE' AND EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.decision IS NULL AND s.is_skipped=0 AND s.assignee_role='hr')",
          'APROBADA_ENCARGADO':"EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' AND s.decision='APROBADA')",
          'RECHAZADA_ENCARGADO':"EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' AND s.decision='RECHAZADA')",
          'APROBADA':'r.status=\'APROBADA\'','RECHAZADA':'r.status=\'RECHAZADA\''}
        if query['status'] in expressions:query_sql+=' AND ('+expressions[query['status']]+')'
    return query_sql


def paged_requests(cx,where,params,page_no=1,limit=25,order='r.id DESC'):
    total=cx.execute('SELECT COUNT(*) '+HISTORY_FROM+where,params).fetchone()[0]
    rows=cx.execute(HISTORY_SELECT+' '+HISTORY_FROM+where+f' ORDER BY {order} LIMIT ? OFFSET ?',
                    [*params,limit,(page_no-1)*limit]).fetchall()
    return rows,total


@app.get('/historial-decisiones',response_class=HTMLResponse)
async def manager_decisions_history(request:Request):
    with db() as cx:
        actor=require_actor(cx,request)
        q=dict(request.query_params);params=[actor['id']]
        where=" WHERE EXISTS(SELECT 1 FROM request_approval_steps own WHERE own.request_id=r.id AND own.assignee_id=? AND own.decision IS NOT NULL AND own.is_skipped=0)"
        if q.get('decision') in ('APROBADA','RECHAZADA'):
            where+=" AND EXISTS(SELECT 1 FROM request_approval_steps own WHERE own.request_id=r.id AND own.assignee_id=? AND own.decision=?)";params.extend([actor['id'],q['decision']])
        where+=history_filters(q,params)
        if q.get('final') in ('APROBADA','RECHAZADA','PENDIENTE'):
            where+=' AND r.status=?';params.append(q['final'])
        try:page_no=max(1,int(q.get('page','1')))
        except ValueError:page_no=1
        rows,total=paged_requests(cx,where,params,page_no,25,'r.submitted_at DESC,r.id DESC')
        enriched=[]
        for row in rows:
            own=cx.execute('SELECT decision,decided_at,note FROM request_approval_steps WHERE request_id=? AND assignee_id=? AND decision IS NOT NULL ORDER BY step_order LIMIT 1',(row['id'],actor['id'])).fetchone()
            enriched.append({**dict(row),'own_decision':own['decision'],'own_decided_at':own['decided_at'],'own_note':own['note']})
        rows=enriched
        return page(request,'manager_decisions.html',actor,rows=rows,total=total,page_no=page_no,pages=max(1,(total+24)//25),filters=q)


@app.get('/historial-solicitudes',response_class=HTMLResponse)
async def hr_request_history(request:Request):
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        q=dict(request.query_params);params=[];where=' WHERE 1=1'+history_filters(q,params,hr=True)
        try:page_no=max(1,int(q.get('page','1')))
        except ValueError:page_no=1
        rows,total=paged_requests(cx,where,params,page_no,25,'r.submitted_at DESC,r.id DESC')
        stats=cx.execute('''SELECT COUNT(*) total,
          SUM(CASE WHEN r.status='PENDIENTE' AND (SELECT s.assignee_role FROM request_approval_steps s WHERE s.request_id=r.id AND s.decision IS NULL AND s.is_skipped=0 ORDER BY s.step_order LIMIT 1)='manager' THEN 1 ELSE 0 END) pending_manager,
          SUM(CASE WHEN r.status='PENDIENTE' AND (SELECT s.assignee_role FROM request_approval_steps s WHERE s.request_id=r.id AND s.decision IS NULL AND s.is_skipped=0 ORDER BY s.step_order LIMIT 1)='hr' THEN 1 ELSE 0 END) pending_hr,
          SUM(CASE WHEN EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' AND s.decision='APROBADA') THEN 1 ELSE 0 END) manager_approved,
          SUM(CASE WHEN EXISTS(SELECT 1 FROM request_approval_steps s WHERE s.request_id=r.id AND s.assignee_role='manager' AND s.decision='RECHAZADA') THEN 1 ELSE 0 END) manager_rejected,
          SUM(CASE WHEN r.status='APROBADA' THEN 1 ELSE 0 END) final_approved,
          SUM(CASE WHEN r.status='RECHAZADA' THEN 1 ELSE 0 END) final_rejected
          FROM requests r''').fetchone()
        branches=cx.execute("SELECT id,name FROM branches WHERE company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ORDER BY name").fetchall()
        areas=cx.execute('SELECT id,name,branch_id FROM areas WHERE active=1 ORDER BY name').fetchall()
        managers=cx.execute("SELECT id,name FROM users WHERE role='manager' ORDER BY name").fetchall()
        return page(request,'request_history.html',actor,rows=rows,total=total,page_no=page_no,pages=max(1,(total+24)//25),filters=q,stats=stats,branches=branches,areas=areas,managers=managers)


@app.get('/historial-solicitudes/{request_id}',response_class=HTMLResponse)
async def hr_request_detail(request:Request,request_id:int):
    with db() as cx:
        actor=require_actor(cx,request)
        row=cx.execute(HISTORY_SELECT+' '+HISTORY_FROM+' WHERE r.id=?',(request_id,)).fetchone()
        if not row:raise HTTPException(404,'Solicitud no encontrada.')
        if not access_control.can_view_request(cx,actor,row):raise HTTPException(403,'No tienes acceso a esta solicitud.')
        steps=cx.execute('''SELECT s.*,COALESCE(NULLIF(s.assignee_name_snapshot,''),u.name) reviewer_name FROM request_approval_steps s
            LEFT JOIN users u ON u.id=s.assignee_id WHERE s.request_id=? ORDER BY s.step_order''',(request_id,)).fetchall()
        caps=access_control.capabilities(cx,actor)
        back='/historial-solicitudes' if 'hr.global' in caps else '/mis-solicitudes' if row['user_id']==actor['id'] else '/historial-decisiones' if 'history.own' in caps else '/'
        return page(request,'request_detail.html',actor,row=row,steps=steps,back_url=back,
                    assignment_issue=access_control.pending_assignment_issue(cx,request_id))


def boleta_filters(q,cx=None):
    params=[];where=' WHERE 1=1'+history_filters(q,params,hr=True,boletas=True)
    if cx is not None:
        valid=[r['id'] for r in cx.execute("SELECT * FROM requests WHERE kind='PERMISO' AND status='APROBADA'") if approved_permit(cx,r)]
        where+=' AND r.id IN ('+','.join('?' for _ in valid)+')' if valid else ' AND 0=1'
        params.extend(valid)
    return where,params


@app.get('/boletas',response_class=HTMLResponse)
async def boleta_management(request:Request):
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        q=dict(request.query_params);where,params=boleta_filters(q,cx)
        try:page_no=max(1,int(q.get('page','1')))
        except ValueError:page_no=1
        rows,total=paged_requests(cx,where,params,page_no,25,'COALESCE(NULLIF(r.starts_at,\'\'),r.submitted_at),r.id')
        branches=cx.execute("SELECT id,name FROM branches WHERE company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ORDER BY name").fetchall()
        areas=cx.execute('SELECT id,name,branch_id FROM areas WHERE active=1 ORDER BY name').fetchall()
        return page(request,'boleta_management.html',actor,rows=rows,total=total,page_no=page_no,pages=max(1,(total+24)//25),filters=q,branches=branches,areas=areas)


@app.post('/boletas/masivo.pdf')
async def bulk_boletas(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        q={key:str(form.get(key,'')).strip() for key in ('employee','q','branch_id','area_id','kind','from','to','period')}
        where,params=boleta_filters(q,cx)
        if str(form.get('download_all',''))=='1' or str(form.get('all_filtered',''))=='1':
            rows,total=paged_requests(cx,where,params,1,100000,'COALESCE(NULLIF(r.starts_at,\'\'),r.submitted_at),r.id')
            if total>100000:
                raise HTTPException(413,'La descarga supera 100000 boletas. Aplica filtros para reducir los resultados.')
        else:
            ids=[]
            for value in form.getlist('request_ids'):
                try:rid=int(value)
                except ValueError:continue
                if rid not in ids:ids.append(rid)
            if not ids:raise HTTPException(400,'Selecciona al menos una boleta.')
            rows,total=paged_requests(cx,where+' AND r.id IN ('+','.join('?' for _ in ids)+')',[*params,*ids],1,len(ids),'COALESCE(NULLIF(r.starts_at,\'\'),r.submitted_at),r.id')
        if not rows:raise HTTPException(404,'No hay boletas autorizadas que coincidan con la selección.')
        items=[load_boleta_data(cx,r['id']) for r in rows]
        try:pdf=pdf_boletas(items)
        except ValueError as exc:raise HTTPException(422,str(exc))
        folios=', '.join(item['request']['folio'] for item in items)
        audit(cx,actor['id'],'GENERACION_MASIVA_PDF','boletas',len(items),f'Folios: {folios}')
        audit(cx,actor['id'],'DESCARGA_MASIVA_PDF','boletas',len(items),f'Folios: {folios}')
        return StreamingResponse(io.BytesIO(pdf),media_type='application/pdf',headers={'Content-Disposition':'attachment; filename="Boletas_KIMTEK.pdf"'})

@app.get('/asistencia',response_class=HTMLResponse)
async def attendance(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        d=month_end_for_day(now().date());s=period_start(d)
        dates=[s+timedelta(days=n) for n in range((min(d,now().date())-s).days+1)]
        dates.reverse()
        rows=[daily_status(cx,a,x) for x in dates]
        return page(request,'attendance.html',a,rows=rows,period_end=d.isoformat())


def _manager_employee(cx,manager_id,user_id):
    return cx.execute('''SELECT u.*,a.name AS area_name,b.name AS branch_name,
            c.name AS company_name,p.supervisor_id
        FROM employee_placements p JOIN users u ON u.id=p.user_id
        JOIN areas a ON a.id=p.area_id
        LEFT JOIN branches b ON b.id=p.branch_id
        LEFT JOIN companies c ON c.id=p.company_id
        WHERE p.user_id=? AND p.valid_from<=?
          AND (p.valid_to IS NULL OR p.valid_to>=?)''',
        (user_id,now().date().isoformat(),now().date().isoformat())).fetchone() if organization.manager_scope(cx,manager_id,user_id,now().date()) else None


@app.get('/mi-equipo',response_class=HTMLResponse)
async def my_team(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        if a['role']!='manager':
            raise HTTPException(403,'Solo encargados de área pueden consultar su equipo.')
        rows=cx.execute('''SELECT u.id,u.name,u.employee_code,u.position,u.active,
            d.name AS area_name,b.name AS branch_name,c.name AS company_name
            FROM employee_placements p JOIN users u ON u.id=p.user_id
            JOIN areas d ON d.id=p.area_id LEFT JOIN branches b ON b.id=p.branch_id
            LEFT JOIN companies c ON c.id=p.company_id
            WHERE p.valid_from<=? AND (p.valid_to IS NULL OR p.valid_to>=?)
            ORDER BY u.name''',(now().date().isoformat(),now().date().isoformat())).fetchall()
        rows=[r for r in rows if organization.manager_scope(cx,a['id'],r['id'],now().date())]
        return page(request,'team_members.html',a,rows=rows)


@app.get('/mi-equipo/asistencia',response_class=HTMLResponse)
async def my_team_attendance(request:Request,fecha:str|None=None):
    with db() as cx:
        a=require_actor(cx,request)
        if a['role']!='manager':
            raise HTTPException(403,'Solo encargados de área pueden consultar su equipo.')
        try:
            day=(date.fromisoformat(fecha) if '-' in fecha else datetime.strptime(fecha,'%d/%m/%Y').date()) if fecha else now().date()-timedelta(days=1)
        except ValueError:
            raise HTTPException(400,'Usa una fecha válida en formato DD/MM/AAAA.')
        members=cx.execute('''SELECT u.*,d.name AS area_name FROM employee_placements p
            JOIN users u ON u.id=p.user_id JOIN areas d ON d.id=p.area_id
            WHERE p.valid_from<=? AND (p.valid_to IS NULL OR p.valid_to>=?)
            ORDER BY u.name''',(day.isoformat(),day.isoformat())).fetchall()
        members=[p for p in members if organization.manager_scope(cx,a['id'],p['id'],day)]
        rows=[{**dict(person),'result':daily_status(cx,person,day)} for person in members]
        return page(request,'team_attendance.html',a,rows=rows,selected_date=day.isoformat())


@app.get('/mi-equipo/{user_id}',response_class=HTMLResponse)
async def my_team_member(request:Request,user_id:int):
    with db() as cx:
        a=require_actor(cx,request)
        if a['role']!='manager':
            raise HTTPException(status_code=403,detail='Solo encargados de área pueden consultar fichas.')
        person=_manager_employee(cx,a['id'],user_id)
        if not person:
            raise HTTPException(404,'Trabajador no encontrado en tu equipo.')
        end=now().date()
        days=[end-timedelta(days=n) for n in range(30)]
        attendance_rows=[daily_status(cx,person,day) for day in days if organization.manager_scope(cx,a['id'],person['id'],day)]
        requests=cx.execute('''SELECT folio,kind,leave_type,starts_at,ends_at,correction_at,status,
                submitted_at FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 50''',(user_id,)).fetchall()
        return page(request,'team_member.html',a,person=person,attendance_rows=attendance_rows,
                    requests=requests)


@app.get('/equipo',response_class=HTMLResponse)
async def team(request:Request,q:str='',branch_id:str='',area_id:str='',pagina:int=1):
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        rows=cx.execute('''SELECT u.*,d.name area_name,p.company_id,p.branch_id,
                b.name branch_name,c.name company_name,p.supervisor_id,
                (SELECT su.name FROM local_staff_profiles lp JOIN users su ON su.id=COALESCE(p.supervisor_id,lp.superior_id) WHERE lp.user_id=u.id) AS supervisor_name,
                p.classification_status
            FROM users u JOIN areas d ON d.id=u.area_id
            LEFT JOIN employee_placements p ON p.user_id=u.id AND p.valid_to IS NULL
            LEFT JOIN branches b ON b.id=p.branch_id
            LEFT JOIN companies c ON c.id=p.company_id ORDER BY u.name''').fetchall()
        areas=cx.execute('''SELECT a.*,b.name AS branch_name,b.company_id AS company_id,
                (SELECT ar.user_id FROM area_responsibles ar JOIN users mgr ON mgr.id=ar.user_id
                 WHERE ar.area_id=a.id AND ar.role='PRINCIPAL' AND ar.valid_from<=?
                   AND ar.valid_to IS NULL AND mgr.active=1 AND mgr.role='manager' ORDER BY ar.id DESC LIMIT 1) AS principal_id,
                (SELECT ar.user_id FROM area_responsibles ar JOIN users mgr ON mgr.id=ar.user_id
                 WHERE ar.area_id=a.id AND ar.role='SUPLENTE' AND ar.valid_from<=?
                   AND ar.valid_to IS NULL AND mgr.active=1 AND mgr.role='manager' ORDER BY ar.id DESC LIMIT 1) AS substitute_id
            FROM areas a
            LEFT JOIN branches b ON b.id=a.branch_id ORDER BY b.name,a.name''',(now().date().isoformat(),now().date().isoformat())).fetchall()
        companies=cx.execute("SELECT * FROM companies WHERE name='KIMTEK' AND active=1").fetchall()
        branches=cx.execute('''SELECT b.*,c.name AS company_name FROM branches b
            JOIN companies c ON c.id=b.company_id AND c.name='KIMTEK' AND c.active=1 ORDER BY b.name''').fetchall()
        managers=cx.execute('''SELECT u.id,u.name,u.employee_code,
            COALESCE((SELECT group_concat(ar.area_id, ',') FROM area_responsibles ar
              WHERE ar.user_id=u.id AND ar.valid_to IS NULL AND ar.valid_from<=?), '') AS responsible_areas
            FROM users u WHERE u.role='manager' AND u.active=1 ORDER BY u.name''',(now().date().isoformat(),)).fetchall()
        schedules=cx.execute('''SELECT s.id,s.name,a.valid_from,a.valid_to,u.name AS employee_name,
                d.name AS area_name FROM schedule_assignments a
            JOIN work_schedules s ON s.id=a.schedule_id
            LEFT JOIN users u ON u.id=a.user_id LEFT JOIN areas d ON d.id=a.area_id
            ORDER BY a.valid_from DESC,a.id DESC LIMIT 100''').fetchall()
        from personnel_presenter import describe
        profiles={r['user_id']:dict(r) for r in cx.execute('SELECT * FROM local_staff_profiles')}
        confirmed={r[0] for r in cx.execute('SELECT user_id FROM biometric_identity_links WHERE confirmed_by IS NOT NULL')}
        rows,personnel_stats=describe(rows,profiles,confirmed)
        all_personnel=rows
        rows=[r for r in rows if (not q.strip() or normalize(q) in normalize(r['name']+' '+r['employee_code']))
              and (not branch_id or str(r['branch_id'])==branch_id) and (not area_id or str(r['area_id'])==area_id)]
        total=len(rows);pages=max(1,(total+19)//20);pagina=max(1,min(pagina,pages))
        rows=rows[(pagina-1)*20:pagina*20]
        return page(request,'team.html',a,rows=rows,all_personnel=all_personnel,areas=areas,companies=companies,
                    kimtek_id=companies[0]['id'] if companies else None,branches=branches,managers=managers,schedules=schedules,
                    pending_classification=personnel_stats['pending'],personnel_stats=personnel_stats,
                    directory_total=total,directory_page=pagina,directory_pages=pages,
                    directory_filters={'q':q,'branch_id':branch_id,'area_id':area_id})

@app.post('/equipo/nuevo')
async def add_employee(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        name=str(form.get('name','')).strip(); code=str(form.get('code','')).strip()
        email=str(form.get('email','')).strip().lower()
        document_id=str(form.get('document_id','')).strip(); position=str(form.get('position','')).strip()
        role=str(form.get('role','employee')); start=str(form.get('start_time','08:00'))
        finish=str(form.get('end_time','17:00')); password=str(form.get('password',''))
        try:
            area_id=int(str(form.get('area_id','0'))); branch_id=int(str(form.get('branch_id','0')))
            supervisor_raw=str(form.get('supervisor_id','')).strip()
            supervisor_id=int(supervisor_raw) if supervisor_raw else None
        except ValueError:
            flash(request,'Selecciona una sucursal y \u00e1rea v\u00e1lidas.',False);return redirect('/equipo')
        cx.execute('SAVEPOINT route_change')
        try:
            if len(name)<3 or len(code)<2 or '@' not in email:
                raise ValueError('Nombre, c\u00f3digo y correo v\u00e1lidos son obligatorios.')
            if role not in ('employee','manager','hr'):
                raise ValueError('Rol no reconocido.')
            if not re.fullmatch(r'\d{2}:\d{2}',start) or not re.fullmatch(r'\d{2}:\d{2}',finish) or start>=finish:
                raise ValueError('El horario debe ser v\u00e1lido y la salida posterior a la entrada.')
            company=cx.execute("SELECT id FROM companies WHERE name='KIMTEK' AND active=1").fetchone()
            branch=cx.execute("SELECT id FROM branches WHERE id=? AND company_id=? AND active=1 AND classification_status='CLASIFICADA'",(branch_id,company['id'] if company else -1)).fetchone()
            area=cx.execute('SELECT id,branch_id,active FROM areas WHERE id=?',(area_id,)).fetchone()
            if not company or not branch or not area or not area['active'] or area['branch_id']!=branch_id:
                raise ValueError('Selecciona una sucursal activa de KIMTEK y un \u00e1rea activa de esa sucursal.')
            if role=='employee':
                if not supervisor_id:
                    raise ValueError('El trabajador debe tener un encargado directo asignado.')
                responsible=cx.execute('''SELECT u.id FROM area_responsibles ar JOIN users u ON u.id=ar.user_id
                    WHERE ar.area_id=? AND ar.user_id=? AND ar.role IN ('PRINCIPAL','SUPLENTE')
                    AND ar.valid_from<=? AND (ar.valid_to IS NULL OR ar.valid_to>=?)
                    AND u.active=1 AND u.role='manager' ''',
                    (area_id,supervisor_id,now().date().isoformat(),now().date().isoformat())).fetchone()
                if not responsible:
                    raise ValueError('El encargado directo debe ser responsable vigente de esta \u00e1rea.')
            elif supervisor_id is not None:
                if supervisor_id==actor['id']:
                    raise ValueError('No puedes asignarte como tu propio encargado.')
                raise ValueError('Encargados y Recursos Humanos no requieren encargado directo.')
            pw=hash_password(password)
            cur=cx.execute('''INSERT INTO users(employee_code,name,document_id,position,area_id,email,role,
              password_hash,start_time,end_time,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (code,name,document_id,position,area_id,email,role,pw,start,finish,nowstr()))
            organization.change_placement(cx,cur.lastrowid,company['id'],branch_id,area_id,
                supervisor_id if role=='employee' else None,now().date(),actor['id'],role=role)
            organization.register_new_person(cx,cur.lastrowid,now().date(),actor['id'])
            audit(cx,actor['id'],'ALTA','usuario',cur.lastrowid,code+' '+role)
            flash(request,('Encargado' if role=='manager' else 'Trabajador')+' creado correctamente.')
        except ValueError as exc:
            cx.execute('ROLLBACK TO route_change')
            flash(request,str(exc),False)
        except Exception:
            cx.execute('ROLLBACK TO route_change')
            flash(request,'No se pudo crear: verifica que el c\u00f3digo y correo no est\u00e1n duplicados.',False)
        finally:
            cx.execute('RELEASE route_change')
    return redirect('/equipo')


@app.get('/equipo/{user_id}/editar',response_class=HTMLResponse)
async def edit_employee_page(request:Request,user_id:int):
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        person=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not person:raise HTTPException(404,'Trabajador no encontrado.')
        if person['role']=='admin':
            raise HTTPException(403,'Las cuentas técnicas de Administrador no se gestionan como personal laboral.')
        areas=cx.execute('''SELECT a.*,b.name AS branch_name,b.company_id AS company_id,
            (SELECT ar.user_id FROM area_responsibles ar JOIN users mgr ON mgr.id=ar.user_id
             WHERE ar.area_id=a.id AND ar.role='PRINCIPAL' AND ar.valid_from<=?
               AND ar.valid_to IS NULL AND mgr.active=1 AND mgr.role='manager' ORDER BY ar.id DESC LIMIT 1) AS principal_id,
            (SELECT ar.user_id FROM area_responsibles ar JOIN users mgr ON mgr.id=ar.user_id
             WHERE ar.area_id=a.id AND ar.role='SUPLENTE' AND ar.valid_from<=?
               AND ar.valid_to IS NULL AND mgr.active=1 AND mgr.role='manager' ORDER BY ar.id DESC LIMIT 1) AS substitute_id
            FROM areas a LEFT JOIN branches b ON b.id=a.branch_id ORDER BY b.name,a.name''',(now().date().isoformat(),now().date().isoformat())).fetchall()
        placement=cx.execute('''SELECT p.*,ar.name AS area_name,b.name AS branch_name,m.name AS supervisor_name
            FROM employee_placements p JOIN areas ar ON ar.id=p.area_id
            LEFT JOIN branches b ON b.id=p.branch_id LEFT JOIN users m ON m.id=p.supervisor_id
            WHERE p.user_id=? AND p.valid_to IS NULL''',(user_id,)).fetchone()
        companies=cx.execute("SELECT * FROM companies WHERE active=1 AND name='KIMTEK' ORDER BY name").fetchall()
        branches=cx.execute('''SELECT b.*,c.name AS company_name FROM branches b
            JOIN companies c ON c.id=b.company_id WHERE b.active=1
            AND b.classification_status='CLASIFICADA' AND c.active=1 AND c.name='KIMTEK' ORDER BY b.name''').fetchall()
        managers=cx.execute('''SELECT u.id,u.name,u.employee_code,
            COALESCE((SELECT group_concat(ar.area_id, ',') FROM area_responsibles ar
              WHERE ar.user_id=u.id AND ar.valid_to IS NULL AND ar.valid_from<=?), '') AS responsible_areas
            FROM users u WHERE u.role='manager' AND u.active=1 ORDER BY u.name''',(now().date().isoformat(),)).fetchall()
        return page(request,'edit_user.html',a,person=person,areas=areas,placement=placement,
                    companies=companies,kimtek_id=companies[0]['id'] if companies else None,branches=branches,managers=managers)

@app.post('/equipo/{user_id}/editar')
async def edit_employee_post(request:Request,user_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        person=cx.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not person:raise HTTPException(404,'Trabajador no encontrado.')
        if person['role']=='admin':
            raise HTTPException(403,'Las cuentas tecnicas de Administrador no se gestionan como personal laboral.')
        cx.execute('SAVEPOINT route_change')
        try:
            name=str(form.get('name','')).strip();code=str(form.get('code','')).strip()
            email=str(form.get('email','')).strip().lower();document_id=str(form.get('document_id','')).strip()
            position=str(form.get('position','')).strip();role=str(form.get('role',person['role']))
            start=str(form.get('start_time','08:00'));end=str(form.get('end_time','17:00'))
            grace=int(str(form.get('grace_minutes','5')));active=1 if str(form.get('active',''))=='1' else 0
            if len(name)<3 or len(code)<2 or '@' not in email:
                raise ValueError('Completa nombre, codigo y correo correctamente.')
            if role not in ('employee','manager','hr'):
                raise ValueError('Rol no valido.')
            if (start or end) and (not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',start) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',end) or start>=end):
                raise ValueError('Horario no valido.')
            if grace<0 or grace>60:raise ValueError('Tolerancia entre 0 y 60 minutos.')
            if actor['id']==user_id and (not active or role!='hr'):
                raise ValueError('No puedes retirar tu propia cuenta de Recursos Humanos.')
            current=cx.execute('SELECT * FROM employee_placements WHERE user_id=? AND valid_to IS NULL',(user_id,)).fetchone()
            branch_raw=str(form.get('branch_id','')).strip();area_raw=str(form.get('area_id','')).strip()
            supervisor_raw=str(form.get('supervisor_id','')).strip()
            effective_raw=str(form.get('effective_from','')).strip()
            changed_organization=False
            desired_supervisor=current['supervisor_id'] if current else None
            if branch_raw or area_raw:
                if not branch_raw or not area_raw:
                    raise ValueError('Selecciona sucursal y area para cambiar la adscripcion.')
                branch_id=int(branch_raw);area_id=int(area_raw)
                company=cx.execute("SELECT id FROM companies WHERE name='KIMTEK' AND active=1").fetchone()
                company_id=company['id'] if company else 0
                branch=cx.execute("SELECT id FROM branches WHERE id=? AND company_id=? AND active=1 AND classification_status='CLASIFICADA'",(branch_id,company_id)).fetchone()
                area=cx.execute('SELECT id,branch_id,active FROM areas WHERE id=?',(area_id,)).fetchone()
                if not branch or not area or not area['active'] or area['branch_id']!=branch_id:
                    raise ValueError('La sucursal y el area deben estar activas y corresponder a KIMTEK.')
                if role=='employee':
                    if supervisor_raw:
                        desired_supervisor=int(supervisor_raw)
                    if not desired_supervisor or desired_supervisor==user_id:
                        raise ValueError('El trabajador requiere un encargado directo distinto de si mismo.')
                    valid=cx.execute("SELECT 1 FROM area_responsibles ar JOIN users m ON m.id=ar.user_id WHERE ar.area_id=? AND ar.user_id=? AND ar.role IN ('PRINCIPAL','SUPLENTE') AND ar.valid_from<=? AND (ar.valid_to IS NULL OR ar.valid_to>=?) AND m.active=1 AND m.role='manager'",(area_id,desired_supervisor,now().date().isoformat(),now().date().isoformat())).fetchone()
                    if not valid:raise ValueError('El encargado directo debe estar asignado a esa area.')
                else:
                    desired_supervisor=None
                changed_organization=bool(not current or current['branch_id']!=branch_id or current['area_id']!=area_id or current['supervisor_id']!=desired_supervisor)
                company_id=company['id']
            elif current and role=='employee':
                area_id=current['area_id']
                if supervisor_raw:
                    desired_supervisor=int(supervisor_raw)
                if not desired_supervisor or desired_supervisor==user_id:
                    raise ValueError('El trabajador requiere un encargado directo distinto de si mismo.')
                valid=cx.execute("SELECT 1 FROM area_responsibles ar JOIN users m ON m.id=ar.user_id WHERE ar.area_id=? AND ar.user_id=? AND ar.role IN ('PRINCIPAL','SUPLENTE') AND ar.valid_from<=? AND (ar.valid_to IS NULL OR ar.valid_to>=?) AND m.active=1 AND m.role='manager'",(area_id,desired_supervisor,now().date().isoformat(),now().date().isoformat())).fetchone()
                if not valid:raise ValueError('El encargado directo debe estar asignado a esa area.')
                changed_organization=changed_organization or desired_supervisor!=current['supervisor_id']
            if role!=person['role'] and current and role in ('manager','hr'):
                desired_supervisor=None
                changed_organization=changed_organization or current['supervisor_id'] is not None
            if changed_organization:
                effective=date.fromisoformat(effective_raw) if effective_raw else now().date()
                if blocked_range(cx,effective,effective):raise ValueError('El periodo esta cerrado; debe reabrirse antes de cambiar la adscripcion.')
                if current and effective<date.fromisoformat(current['valid_from']):raise ValueError('La fecha efectiva no puede ser anterior a la adscripcion vigente.')
                company_id=company_id if branch_raw else current['company_id']
                branch_id=branch_id if branch_raw else current['branch_id']
                area_id=area_id if area_raw else current['area_id']
                organization.change_placement(cx,user_id,company_id,branch_id,area_id,desired_supervisor,
                    effective,actor['id'],'CLASIFICADA' if branch_raw and area_raw else current['classification_status'],role)
                area_id_for_user=area_id
            else:
                area_id_for_user=person['area_id']
            organization.record_role(cx,user_id,role,date.fromisoformat(effective_raw) if effective_raw else now().date(),actor['id'])
            if not active and cx.execute('SELECT 1 FROM users u JOIN local_staff_profiles p ON p.superior_id=? WHERE p.user_id=u.id AND u.active=1 UNION SELECT 1 FROM employee_placements p JOIN users u ON u.id=p.user_id WHERE p.supervisor_id=? AND p.valid_to IS NULL AND u.active=1',(user_id,user_id)).fetchone():
                raise ValueError('Sustituye primero las dependencias activas antes de desactivar al superior.')
            reset=str(form.get('new_password','')).strip()
            if reset and actor['role'] not in ('hr','admin'):raise ValueError('Solo Recursos Humanos o Administracion puede restablecer contrasenas de terceros.')
            reset_hash=hash_password(reset) if reset else None
            cx.execute('''UPDATE users SET name=?,employee_code=?,email=?,document_id=?,position=?,area_id=?,role=?,start_time=?,end_time=?,grace_minutes=?,active=? WHERE id=?''',
                (name,code,email,document_id,position,area_id_for_user,role,start,end,grace,active,user_id))
            if reset_hash:cx.execute('UPDATE users SET password_hash=? WHERE id=?',(reset_hash,user_id))
            audit(cx,actor['id'],'EDICION','usuario',user_id,f'area={area_id_for_user},rol={role},activo={active},horario={start}-{end},tolerancia={grace}')
            flash(request,'Datos y adscripcion actualizados; el historial de traslados se conserva.')
            return redirect('/equipo')
        except (ValueError,Exception) as exc:
            cx.execute('ROLLBACK TO route_change')
            flash(request,str(exc) if isinstance(exc,ValueError) else 'No se pudo guardar: verifica codigo y correo no duplicados.',False)
            return redirect(f'/equipo/{user_id}/editar')
        finally:
            cx.execute('RELEASE route_change')


@app.post('/equipo/{user_id}/adscripcion')
async def change_employee_placement(request:Request,user_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        employee=cx.execute("SELECT * FROM users WHERE id=? AND role<>'admin'",(user_id,)).fetchone()
        if not employee: raise HTTPException(404,'Trabajador no encontrado.')
        try:
            organization.change_placement(cx,user_id,
                cx.execute("SELECT id FROM companies WHERE name='KIMTEK' AND active=1").fetchone()['id'],
                int(form.get('branch_id','0')),int(form.get('area_id','0')),
                int(form['supervisor_id']) if form.get('supervisor_id') else None,
                date.fromisoformat(str(form.get('effective_from',''))),actor['id'])
            audit(cx,actor['id'],'TRANSFERENCIA','usuario',user_id,'Cambio organizacional con historia civil y revisión inmutable.')
            flash(request,'Adscripción guardada con fecha efectiva e historial.')
        except ValueError as exc: flash(request,str(exc),False)

    return redirect(f'/equipo/{user_id}/editar')


@app.post('/equipo/area')
async def add_area(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        name=str(form.get('name','')).strip()[:80]
        try:
            branch_id=int(str(form.get('branch_id','0')))
        except ValueError:
            branch_id=0
        if len(name)<3:
            flash(request,'El nombre del área debe tener al menos 3 letras.',False)
        else:
            try:
                branch=cx.execute('''SELECT id FROM branches WHERE id=? AND active=1
                    AND classification_status='CLASIFICADA' AND company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ''',(branch_id,)).fetchone()
                if not branch:
                    raise ValueError('Selecciona una sucursal clasificada y activa.')
                cur=cx.execute('INSERT INTO areas(name,branch_id) VALUES(?,?)',(name,branch_id))
                audit(cx,a['id'],'ALTA','area',cur.lastrowid,name)
                flash(request,'Área creada.')
            except ValueError as exc:
                flash(request,str(exc),False)
            except Exception:
                flash(request,'No se pudo crear el área. Revisa sus datos.',False)
    return redirect('/equipo')


@app.post('/equipo/sucursal')
async def add_branch(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        name=str(form.get('name','')).strip()[:120]
        if len(name)<2:
            flash(request,'El nombre de la sucursal es obligatorio.',False)
        else:
            try:
                company=cx.execute("SELECT id FROM companies WHERE name='KIMTEK' AND active=1").fetchone()
                cur=cx.execute("INSERT INTO branches(company_id,name,active,classification_status,created_at) VALUES(?,?,1,'CLASIFICADA',?)",(company['id'],name,nowstr()))
                audit(cx,actor['id'],'ALTA','sucursal',cur.lastrowid,name+'; KIMTEK')
                flash(request,'Sucursal creada y asignada a KIMTEK.')
            except Exception:
                flash(request,'No se pudo crear la sucursal. Verifica si el nombre ya existe.',False)
    return redirect('/equipo')


@app.post('/equipo/sucursal/{branch_id}/editar')
async def edit_branch(request:Request,branch_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        branch=cx.execute('SELECT * FROM branches WHERE id=?',(branch_id,)).fetchone()
        if not branch:
            raise HTTPException(404,'Sucursal no encontrada.')
        name=str(form.get('name','')).strip()[:120]
        active=1 if str(form.get('active',''))=='1' else 0
        company=cx.execute("SELECT active FROM companies WHERE id=? AND name='KIMTEK'",(branch['company_id'],)).fetchone() if branch['company_id'] else None
        if len(name)<2:
            flash(request,'El nombre de la sucursal es obligatorio.',False)
        elif active and (not company or not company['active']):
            flash(request,'Asigna la sucursal a una empresa activa antes de activarla.',False)
        else:
            try:
                cx.execute('UPDATE branches SET name=?,active=? WHERE id=?',(name,active,branch_id))
                audit(cx,actor['id'],'EDICION','sucursal',branch_id,
                      f'Nombre={branch["name"]} -> {name}; activa={active}')
                flash(request,'Sucursal actualizada.')
            except Exception:
                flash(request,'No se pudo guardar; verifica si el nombre se repite en esa empresa.',False)
    return redirect('/equipo')


@app.post('/equipo/area/adscribir')
async def assign_area_branch(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        try:
            area_id=int(str(form.get('area_id','0')))
            branch_id=int(str(form.get('branch_id','0')))
            area=cx.execute('SELECT * FROM areas WHERE id=?',(area_id,)).fetchone()
            branch=cx.execute('''SELECT id FROM branches WHERE id=? AND active=1
                AND classification_status='CLASIFICADA'
                                 AND company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ''',(branch_id,)).fetchone()
            if not area or not branch:
                raise ValueError('Selecciona un área y una sucursal clasificada.')
            placed=cx.execute('''SELECT 1 FROM employee_placements
                WHERE area_id=? AND valid_to IS NULL AND classification_status='CLASIFICADA' LIMIT 1''',
                (area_id,)).fetchone()
            if placed:
                raise ValueError('Transfiere primero a los trabajadores vigentes antes de mover el área.')
            cx.execute('UPDATE areas SET branch_id=? WHERE id=?',(branch_id,area_id))
            audit(cx,a['id'],'ADSCRIPCION','area',area_id,
                  f'Sucursal anterior {area["branch_id"] or "pendiente"} -> {branch_id}')
            flash(request,'Área adscrita. Los trabajadores existentes conservan clasificación pendiente hasta su revisión.')
        except ValueError as exc:
            flash(request,str(exc),False)
    return redirect('/equipo')


@app.post('/equipo/area/{area_id}/editar')
async def edit_area(request:Request,area_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        area=cx.execute('SELECT * FROM areas WHERE id=?',(area_id,)).fetchone()
        if not area:
            raise HTTPException(404,'Área no encontrada.')
        name=str(form.get('name','')).strip()[:80]
        active=1 if str(form.get('active',''))=='1' else 0
        if len(name)<2:
            flash(request,'El nombre del área es obligatorio.',False)
        else:
            try:
                cx.execute('UPDATE areas SET name=?,active=? WHERE id=?',(name,active,area_id))
                audit(cx,actor['id'],'EDICION','area',area_id,
                      f'Nombre={area["name"]} -> {name}; activa={active}')
                flash(request,'Área actualizada.')
            except Exception:
                flash(request,'No se pudo guardar el área.',False)
    return redirect('/equipo')


@app.post('/equipo/responsable')
async def assign_area_responsible(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        try:
            area_id=int(form.get('area_id','0'))
            organization.assign_responsibles(cx,area_id,int(form.get('principal_id','0')),
                int(form['substitute_id']) if form.get('substitute_id') else None,now().date(),a['id'])
            audit(cx,a['id'],'ASIGNACION_RESPONSABLE','area',area_id,'Responsables actualizados mediante transacción organizacional.')
            flash(request,'Responsables actualizados; las decisiones históricas no se modifican.')
        except ValueError as exc: flash(request,str(exc),False)

    return redirect('/equipo')


@app.post('/equipo/horarios')
async def save_schedule(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            name=str(form.get('name','')).strip()[:100]
            target_kind=str(form.get('target_kind',''))
            target_raw=str(form.get('target_id',''))
            target_id=int(target_raw)
            effective=date.fromisoformat(str(form.get('valid_from','')))
            if len(name)<2:
                raise ValueError('El horario debe tener un nombre.')
            if target_kind not in ('user','area'):
                raise ValueError('Selecciona si el horario aplica a una persona o a un área.')
            if blocked_range(cx,effective,effective):
                raise ValueError('No puedes cambiar horarios de un período cerrado.')
            if target_kind=='user':
                target=cx.execute("SELECT id FROM users WHERE id=? AND active=1 AND role<>'admin'",
                                  (target_id,)).fetchone()
                if not target:
                    raise ValueError('Trabajador no encontrado o inactivo.')
                column='user_id'
            else:
                target=cx.execute('SELECT id FROM areas WHERE id=? AND active=1',(target_id,)).fetchone()
                if not target:
                    raise ValueError('Área no encontrada o inactiva.')
                column='area_id'
            weekdays=[]
            for weekday in range(7):
                working=str(form.get(f'working_{weekday}',''))=='1'
                start=str(form.get(f'start_{weekday}','')).strip()
                finish=str(form.get(f'end_{weekday}','')).strip()
                grace=int(str(form.get(f'grace_{weekday}','5')))
                if grace<0 or grace>60:
                    raise ValueError('La tolerancia debe estar entre 0 y 60 minutos.')
                if working:
                    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',start) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',finish):
                        raise ValueError('Completa horas válidas para cada día laborable.')
                    if start>=finish:
                        raise ValueError('Este módulo no admite todavía turnos que crucen medianoche; no se guardó el horario.')
                else:
                    start=finish=None
                weekdays.append((weekday,int(working),start,finish,grace))
            cur=cx.execute('''INSERT INTO work_schedules(name,created_by,created_at)
                VALUES(?,?,?)''',(name,actor['id'],nowstr()))
            schedule_id=cur.lastrowid
            cx.executemany('''INSERT INTO work_schedule_days
                (schedule_id,weekday,is_working,start_time,end_time,grace_minutes)
                VALUES(?,?,?,?,?,?)''',[(schedule_id,*day) for day in weekdays])
            previous=(effective-timedelta(days=1)).isoformat()
            cx.execute(f'''UPDATE schedule_assignments SET valid_to=?
                WHERE {column}=? AND valid_to IS NULL''',(previous,target_id))
            cx.execute(f'''INSERT INTO schedule_assignments
                (schedule_id,{column},valid_from,assigned_by,created_at)
                VALUES(?,?,?,?,?)''',(schedule_id,target_id,effective.isoformat(),actor['id'],nowstr()))
            audit(cx,actor['id'],'ASIGNACION_HORARIO',target_kind,target_id,
                  f'{name}; vigencia {effective.isoformat()}; horario semanal configurado')
            flash(request,'Horario semanal asignado con vigencia e historial.')
        except ValueError as exc:
            flash(request,str(exc),False)
    return redirect('/equipo')

@app.get('/calendario',response_class=HTMLResponse)
async def exceptions_page(request:Request):
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        people=cx.execute('SELECT id,name,employee_code FROM users WHERE active=1 ORDER BY name').fetchall()
        rows=cx.execute('''SELECT e.*,u.name AS employee_name FROM calendar_exceptions e
             LEFT JOIN users u ON u.id=e.user_id ORDER BY e.date DESC LIMIT 100''').fetchall()
        return page(request,'calendar.html',a,rows=rows,people=people)

@app.post('/calendario')
async def exceptions_post(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        try:
            d=date.fromisoformat(str(form.get('date','')))
            if blocked_range(cx,d,d):raise ValueError('Periodo cerrado; requiere reapertura.')
            employee=str(form.get('employee','')).strip()
            uid=int(employee) if employee else None
            if uid and not cx.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():
                raise ValueError('Trabajador no encontrado.')
            kind=str(form.get('kind',''))
            if kind not in ('ASUETO','LIBRE'):raise ValueError('Tipo no permitido.')
            description=str(form.get('description','')).strip()
            if len(description)<8:raise ValueError('Indica un motivo con 8 caracteres.')
            cur=cx.execute('''INSERT INTO calendar_exceptions(date,user_id,kind,description,created_by,created_at)
                      VALUES(?,?,?,?,?,?)''',(d.isoformat(),uid,kind,description[:250],a['id'],nowstr()))
            audit(cx,a['id'],'CALENDARIO','excepcion',cur.lastrowid,f'{d} {kind}')
            flash(request,'Excepción registrada para el cálculo de asistencia.')
        except ValueError as exc:
            flash(request,str(exc),False)
    return redirect('/calendario')

@app.get('/importar',response_class=HTMLResponse)
async def importer_page(request:Request):
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        branches=cx.execute('''SELECT id,name FROM branches
                               WHERE active=1 AND classification_status='CLASIFICADA'
                               AND company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ORDER BY name''').fetchall()
        users=cx.execute('''SELECT id,employee_code,name FROM users WHERE active=1
                            ORDER BY name''').fetchall()
        batches=cx.execute('''SELECT b.*,br.name AS branch_name,u.name AS importer_name
            FROM import_batches b LEFT JOIN branches br ON br.id=b.branch_id
            LEFT JOIN users u ON u.id=b.imported_by ORDER BY b.id DESC LIMIT 20''').fetchall()
        return page(request,'import.html',a,branches=branches,users=users,batches=batches)


def normalize(s):
    s=unicodedata.normalize('NFKD',str(s or '').lower()).encode('ascii','ignore').decode()
    return re.sub('[^a-z0-9]+',' ',s).strip()

HEADER_ALIASES={
  'code': ['empleado id','id empleado','codigo','codigo empleado','empleado','employee id',
           'personnel id','personal id','numero empleado','numero de empleado','employee code','emp code','id'],
  'date':['fecha','date','record date','log date','dia'],
  'time':['hora','time','record time','check time','hora marcacion','punch time'],
  'datetime':['fecha y hora','fecha hora','datetime','timestamp','event time','check datetime','fecha hora de marcacion','marcacion'],
  'kind':['tipo','tipo marcacion','entrada salida','check type','punch state','estado marcacion',
          'punch status','direction'],
}

def identify_headers(headers):
    found={}
    for field,names in HEADER_ALIASES.items():
        for i,h in enumerate(headers):
            if normalize(h) in names:
                found[field]=i;break
    if 'code' not in found or ('datetime' not in found and not ('date' in found and 'time' in found)):
        raise ValueError('Encabezados no reconocidos. Se requiere codigo/id de empleado y fecha+hora. Usa la plantilla CSV incluida.')
    return found


def parse_event(row, mapping):
    def val(k):
        idx=mapping.get(k)
        return row[idx] if idx is not None and idx < len(row) else ''
    code=str(val('code') or '').strip()
    if not code:raise ValueError('Código de empleado vacío')
    if re.fullmatch('\\d+\\.0',code):code=code[:-2]
    if 'datetime' in mapping:
        raw=val('datetime')
    else:
        dateval=val('date');timeval=val('time')
        if isinstance(dateval,datetime):dateval=dateval.date()
        if isinstance(timeval,datetime):timeval=timeval.time()
        raw=f'{dateval} {timeval}'
    if isinstance(raw,datetime):when=raw
    elif isinstance(raw,(int,float)):
        from openpyxl.utils.datetime import from_excel
        when=from_excel(raw)
    else:
        when=parser.parse(str(raw),dayfirst=not str(raw).startswith(('20','19')))
    stamp=when.strftime('%Y-%m-%d %H:%M:%S')
    kind_raw=normalize(val('kind'))
    if kind_raw in ('in','entrada','ingreso','check in','0','1 in','entrada 0'):
        kind='IN'
    elif kind_raw in ('out','salida','egreso','check out','1','salida 1'):
        kind='OUT'
    else:kind='UNSPEC'
    return code,stamp,kind


def resolve_biometric_user(cx, code, branch_id=None, device_code=None, event_date=None):
    event_date=(event_date or now().date()).isoformat()
    assignments=cx.execute('''SELECT DISTINCT user_id FROM biometric_assignments
        WHERE employee_code=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',
        (code,event_date,event_date)).fetchall()
    if branch_id is not None and device_code:
        exact=cx.execute('''SELECT user_id FROM biometric_assignments
            WHERE branch_id=? AND device_code=? AND employee_code=? AND valid_from<=?
              AND (valid_to IS NULL OR valid_to>=?)''',
            (branch_id,device_code,code,event_date,event_date)).fetchall()
        if len(exact)==1:
            return exact[0]['user_id']
        if exact:
            raise ValueError(f'Código biométrico {code!r} tiene asignaciones duplicadas para ese dispositivo.')
        if assignments:
            raise ValueError(f'El código {code!r} está asignado a otra sucursal o dispositivo.')
    elif len(assignments)>1:
        raise ValueError(f'Código biométrico {code!r} ambiguo: especifica sucursal y dispositivo.')
    elif len(assignments)==1:
        return assignments[0]['user_id']
    user=cx.execute('SELECT id FROM users WHERE employee_code=? AND active=1',(code,)).fetchone()
    if not user:
        raise ValueError(f'Código {code!r} sin asignación biométrica inequívoca.')
    return user['id']


def rows_from_file(filename,content):
    ext=Path(filename).suffix.lower()
    if ext=='.csv':
        for encoding in ('utf-8-sig','cp1252','latin-1'):
            try:text=content.decode(encoding);break
            except UnicodeDecodeError:continue
        sample=text[:5000]
        try:dialect=csv.Sniffer().sniff(sample,delimiters=',;\t')
        except csv.Error:dialect=csv.excel
        reader=csv.reader(io.StringIO(text),dialect)
        rows=list(reader)
    elif ext=='.xlsx':
        workbook=load_workbook(io.BytesIO(content),read_only=True,data_only=True)
        try:
            worksheet=workbook.active
            worksheet.reset_dimensions()
            rows=list(worksheet.iter_rows(values_only=True))
        finally:workbook.close()
    else:
        raise ValueError('Formato no permitido. Usa CSV o Excel .xlsx sin contraseña.')
    rows=[list(r) for r in rows if any(v is not None and str(v).strip() for v in r)]
    if len(rows)<2:raise ValueError('Archivo vacío o sin marcaciones.')
    if len(rows)>200001:raise ValueError('Límite: 200.000 filas por archivo.')
    try:
        mapping=identify_headers(rows[0])
    except ValueError:
        if ext=='.xlsx':
            try:parse_zlink_xlsx(content)
            except ValueError:raise
            raise ValueError('Excel ZKBio Zlink detectado. Usa Conciliación de asistencia para conservar tipo, fuente, dispositivos y hora de carga.')
        raise
    return rows[1:],mapping


async def _stage_unified_attendance_import(request: Request, *, zlink_hr_only: bool = False):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        uploaded=form.get('file')
        if not isinstance(uploaded,UploadFile):
            flash(request,'Selecciona un archivo CSV o XLSX.',False);return redirect('/importar')
        content=await uploaded.read(20*1024*1024+1)
        if len(content)>20*1024*1024:
            flash(request,'El archivo supera el límite de 20 MB.',False);return redirect('/importar')
        filename=Path(uploaded.filename or 'marcaciones').name[:150]
        source='LEGACY'
        preview=None
        if Path(filename).suffix.lower()=='.xlsx':
            try:
                preview=preview_zlink(cx,content);source='ZKBIO'
            except ValueError:
                preview=None
        if source=='ZKBIO' and (zlink_hr_only and actor['role']!='hr'):
            raise HTTPException(403,'Solo Recursos Humanos puede importar el formato ZKBio Zlink.')
        branch_raw=str(form.get('branch_id','')).strip()
        device_code=str(form.get('device_code','')).strip()[:80]
        branch_id=None
        if branch_raw:
            try:branch_id=int(branch_raw)
            except ValueError:raise HTTPException(400,'Sucursal inválida.')
            branch=cx.execute('''SELECT id FROM branches WHERE id=? AND active=1
                AND classification_status='CLASIFICADA' AND company_id=(SELECT id FROM companies
                WHERE name='KIMTEK' AND active=1)''',(branch_id,)).fetchone()
            if not branch:raise HTTPException(400,'La sucursal debe estar activa y clasificada.')
        if source=='LEGACY':
            try:records,mapping=rows_from_file(filename,content)
            except Exception as exc:
                flash(request,'Formato no reconocido: '+str(exc),False);return redirect('/importar')
            if bool(branch_id)!=bool(device_code):
                flash(request,'Sucursal y dispositivo deben indicarse juntos o dejarse vacíos.',False)
                return redirect('/importar')
            valid=duplicate=0;errors=[];seen=set();dates=[]
            for idx,row in enumerate(records,2):
                try:
                    code,stamp,kind=parse_event(row,mapping);workday=date.fromisoformat(stamp[:10]);dates.append(workday)
                    user_id=resolve_biometric_user(cx,code,branch_id,device_code or None,workday)
                    if blocked_range(cx,workday,workday):raise ValueError('Pertenece a un período cerrado.')
                    key=hashlib.sha256(f'{user_id}|{stamp}|{kind}'.encode()).hexdigest()
                    if key in seen or cx.execute('SELECT 1 FROM punches WHERE source_key=?',(key,)).fetchone():
                        duplicate+=1;continue
                    seen.add(key);valid+=1
                except Exception as exc:
                    if len(errors)<25:errors.append({'row_number':idx,'parse_error':str(exc)})
            preview={'source':source,'sha256':hashlib.sha256(content).hexdigest(),
                'row_count':len(records),'valid_count':valid,'duplicate_existing':duplicate,
                'errors':errors,'error_count':max(0,len(records)-valid-duplicate),
                'identity_count':0,'identity_pending':0,'device_count':1 if device_code else 0,
                'date_from':min(dates).isoformat() if dates else '',
                'date_to':max(dates).isoformat() if dates else ''}
        else:
            preview['source']=source
            preview['error_count']=preview.get('error_count',len(preview.get('errors',[])))
        token=uuid.uuid4().hex
        RECONCILIATION_STAGING.mkdir(parents=True,exist_ok=True)
        (RECONCILIATION_STAGING/f'{token}.xlsx').write_bytes(content)
        (RECONCILIATION_STAGING/f'{token}.json').write_text(json.dumps({
            'source':source,'sha256':preview['sha256'],'filename':filename,'actor_id':actor['id'],
            'branch_id':branch_id,'device_code':device_code,'date_from':preview.get('date_from'),
            'date_to':preview.get('date_to')},ensure_ascii=False),encoding='utf-8')
        return page(request,'import_preview.html',actor,preview=preview,token=token,filename=filename)


@app.post('/importar/preview')
async def unified_import_preview(request:Request):
    return await _stage_unified_attendance_import(request,zlink_hr_only=True)


def _import_legacy_events(cx, content, filename, actor, branch_id, device_code):
    records,mapping=rows_from_file(filename,content)
    file_hash=hashlib.sha256(content).hexdigest()
    previous=cx.execute('SELECT id FROM import_batches WHERE file_sha256=? AND rows_error=0 ORDER BY id LIMIT 1',(file_hash,)).fetchone()
    if previous:
        attempt=cx.execute('''INSERT INTO import_batches(filename,branch_id,device_code,imported_by,
            imported_at,rows_total,rows_added,rows_duplicate,rows_error,file_sha256,details_json)
            VALUES(?,?,?,?,?,?,0,?,0,?,?)''',(filename,branch_id,device_code or None,actor['id'],
            nowstr(),len(records),len(records),file_hash,json.dumps({'original_batch_id':previous['id']})))
        dates=[]
        for row in records:
            try:
                _,stamp,_=parse_event(row,mapping);dates.append(date.fromisoformat(stamp[:10]))
            except Exception:
                pass
        audit(cx,actor['id'],'IMPORTACION_DUPLICADA','lote',previous['id'],
              f'Archivo {filename}; SHA-256 ya importado; no se insertaron eventos.')
        return {'batch_id':attempt.lastrowid,'added':0,'duplicate':len(records),'errors':0,
                'date_from':min(dates).isoformat() if dates else '',
                'date_to':max(dates).isoformat() if dates else '',
                'run_id':None,'recalculated_days':0,'already_imported':True}
    added=duplicate=error_count=0;errors=[];dates=[]
    for idx,row in enumerate(records,2):
        try:
            code,stamp,kind=parse_event(row,mapping);workday=date.fromisoformat(stamp[:10]);dates.append(workday)
            user_id=resolve_biometric_user(cx,code,branch_id,device_code or None,workday)
            if blocked_range(cx,workday,workday):raise ValueError('Pertenece a un período cerrado.')
            if cx.execute('''SELECT 1 FROM biometric_events e JOIN biometric_identity_links l
                ON l.person_id=e.person_id WHERE l.user_id=? AND e.occurred_at=?
                AND l.valid_from<=substr(e.occurred_at,1,10)
                AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10)) LIMIT 1''',
                (user_id,stamp)).fetchone():
                raise ValueError('Coincide con un evento ZKBio ya registrado; revisar el origen antes de importar otra copia.')
            key=hashlib.sha256(f'{user_id}|{stamp}|{kind}'.encode()).hexdigest()
            if cx.execute('SELECT 1 FROM punches WHERE source_key=?',(key,)).fetchone():
                duplicate+=1;continue
            cx.execute('''INSERT INTO punches(user_id,occurred_at,kind,source,source_key,imported_by,created_at)
                VALUES(?,?,?,?,?,?,?)''',(user_id,stamp,kind,filename,key,actor['id'],nowstr()))
            added+=1
        except Exception as exc:
            error_count+=1
            if len(errors)<12:errors.append(f'Fila {idx}: {exc}')
    batch=cx.execute('''INSERT INTO import_batches(filename,branch_id,device_code,imported_by,imported_at,
        rows_total,rows_added,rows_duplicate,rows_error,file_sha256,details_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(filename,branch_id,device_code or None,actor['id'],nowstr(),
        len(records),added,duplicate,error_count,file_hash,json.dumps(errors,ensure_ascii=False)))
    audit(cx,actor['id'],'IMPORTACION','archivo',filename,
          f'Lote {batch.lastrowid}; nuevas={added}; duplicadas={duplicate}; errores={error_count}')
    from attendance_continuous import capture_changes
    from reconciliation import drain_recalculations
    capture_changes(cx)
    incremental=drain_recalculations(cx)
    last_run=cx.execute('SELECT id FROM attendance_reconciliation_runs ORDER BY id DESC LIMIT 1').fetchone() if incremental['affected'] else None
    return {'batch_id':batch.lastrowid,'added':added,'duplicate':duplicate,
            'errors':error_count,'error_rows':errors,'date_from':min(dates).isoformat() if dates else '',
            'date_to':max(dates).isoformat() if dates else '', 'run_id':last_run['id'] if last_run else None,
            'recalculated_days':len(incremental['affected'])}


@app.post('/importar/confirmar')
async def unified_import_confirm(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        token=str(form.get('token',''))
        if not re.fullmatch(r'[a-f0-9]{32}',token):raise HTTPException(400,'Vista previa inválida.')
        workbook=RECONCILIATION_STAGING/f'{token}.xlsx';metadata_path=RECONCILIATION_STAGING/f'{token}.json'
        if not workbook.is_file() or not metadata_path.is_file():raise HTTPException(410,'La vista previa venció; vuelve a cargar el archivo.')
        metadata=json.loads(metadata_path.read_text(encoding='utf-8'))
        if metadata.get('actor_id')!=actor['id']:raise HTTPException(403,'La vista previa pertenece a otra sesión.')
        content=workbook.read_bytes();filename=metadata['filename'];source=metadata['source']
        if hashlib.sha256(content).hexdigest()!=metadata.get('sha256'):raise HTTPException(400,'El archivo cambió después de la vista previa.')
        try:
            if source=='ZKBIO':
                if actor['role']!='hr':raise HTTPException(403,'Solo Recursos Humanos puede confirmar importaciones ZKBio.')
                preview=preview_zlink(cx,content)
                confirmed=str(form.get('confirm_coverage',''))=='1'
                outcome=import_zlink(cx,preview,content,actor['id'],filename,confirmed)
                from attendance_continuous import process_import
                raw_expected=str(form.get('expected_devices','')).strip()
                outcome=process_import(cx,preview,outcome,actor['id'],confirmed,
                    requested_from=str(form.get('requested_from','')).strip() or None,
                    requested_to=str(form.get('requested_to','')).strip() or None,
                    expected_devices=[v.strip() for v in raw_expected.split(',') if v.strip()] if raw_expected else None,
                    no_event_devices=[v.strip() for v in str(form.get('no_event_devices','')).split(',') if v.strip()],
                    exported_at=str(form.get('exported_at','')).strip() or None,note=str(form.get('coverage_note','')).strip()[:2000])
                start=date.fromisoformat(outcome['date_from']);end=date.fromisoformat(outcome['date_to'])
                audit(cx,actor['id'],'IMPORTACION_UNIFICADA','lote',outcome['batch_id'],
                      f'ZKBio sha256={preview["sha256"]}; nuevos={outcome["added"]}; duplicados={outcome["duplicate"]}; errores={outcome["errors"]}')
                audit(cx,actor['id'],'RECONCILIACION_INCREMENTAL','lote',outcome['batch_id'],
                      f'{outcome["recalculated_days"]} jornadas; {start}..{end}; recepción {outcome["receipt_id"]}')
                flash(request,(f'El archivo ya estaba importado en el lote #{outcome["batch_id"]}; no se insertaron eventos.'
                    if outcome.get('already_imported') else
                    f'Lote #{outcome["batch_id"]}: {outcome["added"]} eventos nuevos, {outcome["duplicate"]} ya existentes, {outcome["errors"]} observados; {outcome["unlinked"]} identidades pendientes.'))
                flash(request,('Archivo ya importado. No se encontraron marcaciones nuevas. ' if outcome.get('already_imported') else '')+f'Recálculo incremental: {outcome["recalculated_days"]} jornadas; '
                    f'{outcome["activity_days"]} con actividad; {outcome["justified_days"]} justificadas; '
                    f'{outcome["review_days"]} requieren revisión. Cobertura: '
                    f'{"pendiente" if outcome["coverage_pending"] else "confirmada"}. '
                    f'{outcome["closed_period_changes"]} revisiones provisionales de períodos cerrados; no modifican el cierre.')
                redirect_to=f'/conciliacion?desde={start}&hasta={end}'
            else:
                outcome=_import_legacy_events(cx,content,filename,actor,metadata.get('branch_id'),metadata.get('device_code'))
                flash(request,(f'El archivo ya estaba importado en el lote #{outcome["batch_id"]}; no se insertaron marcas.'
                    if outcome.get('already_imported') else
                    f'Lote #{outcome["batch_id"]}: {outcome["added"]} marcas nuevas, {outcome["duplicate"]} duplicadas, {outcome["errors"]} observadas. Conciliación: {"ejecutada" if outcome["run_id"] else "rango mayor a 367 días; ejecutar manualmente"}.'))
                redirect_to='/conciliacion'+(f'?desde={outcome["date_from"]}&hasta={outcome["date_to"]}' if outcome['date_from'] and outcome['date_to'] and outcome['run_id'] else '')
        except ValueError as exc:
            cx.rollback()
            flash(request,str(exc),False)
            redirect_to='/importar'
        finally:
            workbook.unlink(missing_ok=True);metadata_path.unlink(missing_ok=True)
    return redirect(redirect_to)

@app.post('/importar')
async def importer_post(request:Request):
    # Compatibility endpoint uses exactly the same persistence service as the preview flow.
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        uploaded=form.get('file')
        if not isinstance(uploaded,UploadFile):
            flash(request,'Selecciona un archivo CSV o XLSX.',False);return redirect('/importar')
        content=await uploaded.read(20*1024*1024+1)
        if len(content)>20*1024*1024:
            flash(request,'El archivo supera 20 MB.',False);return redirect('/importar')
        if Path(uploaded.filename or '').suffix.lower()=='.xlsx':
            try:
                parse_zlink_xlsx(content)
            except ValueError:
                pass
            else:
                return await _stage_unified_attendance_import(request,zlink_hr_only=True)
        raw=str(form.get('branch_id','')).strip();device=str(form.get('device_code','')).strip()[:80]
        try:
            branch_id=int(raw) if raw else None
            if bool(branch_id)!=bool(device):raise ValueError('Indica sucursal y dispositivo juntos.')
            if branch_id and not cx.execute("SELECT id FROM branches WHERE id=? AND active=1 AND classification_status='CLASIFICADA' AND company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1)",(branch_id,)).fetchone():
                raise ValueError('Sucursal no disponible.')
            outcome=_import_legacy_events(cx,content,Path(uploaded.filename or 'marcaciones').name[:150],actor,branch_id,device)
            message=f"Importacion: {outcome['added']} marcaciones nuevas; {outcome['duplicate']} duplicadas."
            if outcome.get('error_rows'):message+=' Incidencias: '+' | '.join(outcome['error_rows'])
            flash(request,message,not outcome['errors'])
        except ValueError as exc:
            flash(request,str(exc),False)
    return redirect('/importar')

@app.post('/importar/biometria')
async def biometric_assignment_post(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_personnel_hr(a)
        try:
            branch_id=int(str(form.get('branch_id','0')))
            user_id=int(str(form.get('user_id','0')))
            device_code=str(form.get('device_code','')).strip()[:80]
            employee_code=str(form.get('employee_code','')).strip()[:80]
            branch=cx.execute('''SELECT id FROM branches WHERE id=? AND active=1
                AND classification_status='CLASIFICADA'
                                 AND company_id=(SELECT id FROM companies WHERE name='KIMTEK' AND active=1) ''',(branch_id,)).fetchone()
            user=cx.execute('SELECT id FROM users WHERE id=? AND active=1',(user_id,)).fetchone()
            if not branch or not user or len(device_code)<1 or len(employee_code)<1:
                raise ValueError('Completa una sucursal clasificada, un dispositivo y una persona activa.')
            cx.execute('''INSERT INTO biometric_assignments
                (branch_id,device_code,employee_code,user_id,valid_from,created_by,created_at)
                VALUES(?,?,?,?,?,?,?)''',
                (branch_id,device_code,employee_code,user_id,now().date().isoformat(),a['id'],nowstr()))
            audit(cx,a['id'],'ASIGNACION_BIOMETRICA','usuario',user_id,
                  f'Código {employee_code}, dispositivo {device_code}, sucursal {branch_id}')
            flash(request,'Asignación biométrica guardada.')
        except ValueError as exc:
            flash(request,str(exc),False)
        except sqlite3.IntegrityError:
            flash(request,'No se pudo guardar; verifica que el código no esté ya asignado en ese dispositivo.',False)
    return redirect('/importar')


RECONCILIATION_STAGING = BASE / 'instance' / 'import_staging'


def _reconciliation_range(start_text, end_text, latest=None):
    if start_text and end_text:
        try:
            first, last = date.fromisoformat(start_text), date.fromisoformat(end_text)
        except ValueError:
            raise HTTPException(400, 'Usa fechas ISO válidas para el rango.')
    elif latest and latest.get('coverage_start') and latest.get('coverage_end'):
        first, last = date.fromisoformat(latest['coverage_start']), date.fromisoformat(latest['coverage_end'])
    else:
        first, last = date(2026, 10, 1), date(2026, 10, 31)
    if last < first or (last-first).days > 366:
        raise HTTPException(400, 'El rango debe ser válido y no superar 367 días.')
    return first, last


def _manager_scope_clause(actor_id):
    return '''EXISTS(SELECT 1 FROM employee_placements ep
        WHERE ep.user_id=d.user_id AND ep.supervisor_id=? AND ep.valid_from<=d.work_date
        AND (ep.valid_to IS NULL OR ep.valid_to>=d.work_date)) OR EXISTS(
        SELECT 1 FROM employee_placements ep JOIN area_responsibles ar ON ar.area_id=ep.area_id
        WHERE ep.user_id=d.user_id AND ep.valid_from<=d.work_date
        AND (ep.valid_to IS NULL OR ep.valid_to>=d.work_date) AND ar.user_id=?
        AND ar.valid_from<=d.work_date AND (ar.valid_to IS NULL OR ar.valid_to>=d.work_date))'''


def _manager_has_scope(cx, manager_id, user_id, work_date):
    return organization.manager_scope(cx,manager_id,user_id,work_date)


def _reconciliation_users(cx):
    return cx.execute("SELECT id,name,employee_code FROM users WHERE active=1 AND role<>'admin' ORDER BY name").fetchall()


def _reconciliation_render_context(cx, actor, first, last, **extra):
    run = cx.execute('''SELECT * FROM attendance_reconciliation_runs WHERE date_from=? AND date_to=?
        ORDER BY id DESC LIMIT 1''', (first.isoformat(), last.isoformat())).fetchone()
    if not run:
        run=cx.execute('''SELECT r.* FROM attendance_daily_current c JOIN attendance_daily_versions v ON v.id=c.version_id
            JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id JOIN attendance_reconciliation_runs r ON r.id=d.run_id
            WHERE c.work_date BETWEEN ? AND ? ORDER BY v.id DESC LIMIT 1''',(first.isoformat(),last.isoformat())).fetchone()
    rows = []
    stats = {}
    if run:
        rows = [dict(row) for row in cx.execute('''SELECT d.*,u.name AS employee_name,
            u.employee_code,u.position,COALESCE(json_extract(d.detail_json,'$.branch_name'),'') AS branch_name,
            COALESCE(json_extract(d.detail_json,'$.area_name'),'') AS area_name,
            COALESCE(json_extract(d.detail_json,'$.regime_code'),'') AS regime_code
            FROM attendance_reconciliation_days d JOIN users u ON u.id=d.user_id
            WHERE d.run_id=? ORDER BY d.work_date DESC,u.name''', (run['id'],))]
        current_rows=[dict(row) for row in cx.execute('''SELECT d.*,u.name employee_name,u.employee_code,u.position,
            json_extract(d.detail_json,'$.branch_name') branch_name,json_extract(d.detail_json,'$.area_name') area_name,
            json_extract(d.detail_json,'$.regime_code') regime_code,c.stale
            FROM attendance_daily_current c JOIN attendance_daily_versions v ON v.id=c.version_id
            JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id JOIN users u ON u.id=c.user_id
            WHERE c.work_date BETWEEN ? AND ? ORDER BY c.work_date DESC,u.name''',(first.isoformat(),last.isoformat()))]
        if current_rows:rows=current_rows
        if actor['role'] == 'manager':
            scope = [row for row in rows if _manager_has_scope(cx,actor['id'],row['user_id'],row['work_date'])]
            rows = scope
        stats = Counter(row['operational_result'] for row in rows)
        stats.update({
            'people_evaluated': len({row['user_id'] for row in rows}),
            'days_evaluated': len(rows),
            'activity_days': sum(bool(row['punch_count']) for row in rows),
            'justified_days': sum(row['operational_result'] == 'AUSENCIA JUSTIFICADA' for row in rows),
            'pending_requests': sum(row['justification_status'] == 'SOLICITUD PENDIENTE' for row in rows),
            'single_records': sum(row['punch_status'] == 'REGISTRO ÚNICO' for row in rows),
            'unconfigured_schedule': len({row['user_id'] for row in rows
                if row['operational_result']=='HORARIO NO CONFIGURADO' or not row.get('regime_code')}),
            'possible_absences': sum(row['operational_result'] == 'POSIBLE INASISTENCIA' for row in rows),
            'confirmed_absences': sum(row['operational_result'] == 'FALTA CONFIRMADA POR RR. HH.' for row in rows),
            'technical_incidents': sum(row['operational_result'] == 'INCIDENCIA TÉCNICA' for row in rows),
            'information_insufficient': sum(row['operational_result'] == 'INFORMACIÓN INSUFICIENTE' for row in rows),
            'justifications_found': sum(row['justification_status'] not in
                ('SIN JUSTIFICACIÓN APROBADA','PENDIENTE DE REVISIÓN') for row in rows),
        })
        from_stamp, to_stamp = first.isoformat()+' 00:00:00',last.isoformat()+' 23:59:59'
        if actor['role']=='manager':
            user_ids=sorted({row['user_id'] for row in rows})
            if user_ids:
                marks=','.join('?' for _ in user_ids)
                source=cx.execute(f'''SELECT COUNT(*) events,
                    COUNT(DISTINCT e.person_id||'|'||substr(e.occurred_at,1,10)) person_days,
                    COUNT(DISTINCT e.person_id) identities,COUNT(DISTINCT NULLIF(e.device_sn,'')) devices
                    FROM biometric_events e JOIN biometric_identity_links l ON l.person_id=e.person_id
                    WHERE l.user_id IN ({marks}) AND l.valid_from<=substr(e.occurred_at,1,10)
                    AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
                    AND e.occurred_at>=? AND e.occurred_at<=?''',(*user_ids,from_stamp,to_stamp)).fetchone()
                manual_source_events=sum(1 for r in cx.execute(f'''SELECT e.source,e.event_type FROM biometric_events e
                    JOIN biometric_identity_links l ON l.person_id=e.person_id
                    WHERE l.user_id IN ({marks}) AND l.valid_from<=substr(e.occurred_at,1,10)
                    AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
                    AND e.occurred_at>=? AND e.occurred_at<=?''',
                    (*user_ids,from_stamp,to_stamp)) if 'administrador' in normalize(r['source']) or 'manual' in normalize(r['event_type']))
            else:
                source={'events':0,'person_days':0,'identities':0,'devices':0}
                manual_source_events=0
            stats.update({'source_events':source['events'],'source_person_days':source['person_days'],
                          'source_identities':source['identities'],'source_devices':source['devices'],
                          'manual_source_events':manual_source_events,
                          'unlinked_identities':0,'unlinked_events':0})
        else:
            source=cx.execute('''SELECT COUNT(*) events,
                COUNT(DISTINCT person_id||'|'||substr(occurred_at,1,10)) person_days,
                COUNT(DISTINCT person_id) identities,COUNT(DISTINCT NULLIF(device_sn,'')) devices
                FROM biometric_events WHERE occurred_at>=? AND occurred_at<=?''',(from_stamp,to_stamp)).fetchone()
            unlinked=cx.execute('''SELECT COUNT(DISTINCT p.person_id) identities,COUNT(e.id) events
                FROM biometric_persons p JOIN biometric_events e ON e.person_id=p.person_id
                WHERE p.link_status<>'CONFIRMED' AND COALESCE((SELECT classification FROM biometric_roster_reviews r WHERE r.person_id=p.person_id ORDER BY r.id DESC LIMIT 1),'PENDING')<>'NON_EMPLOYEE' AND e.occurred_at>=? AND e.occurred_at<=?''',
                (from_stamp,to_stamp)).fetchone()
            stats.update({'source_events':source['events'],'source_person_days':source['person_days'],
                          'source_identities':source['identities'],'source_devices':source['devices'],
                          'manual_source_events':sum(1 for r in cx.execute('SELECT source,event_type FROM biometric_events WHERE occurred_at>=? AND occurred_at<=?',
                              (from_stamp,to_stamp)) if 'administrador' in normalize(r['source']) or 'manual' in normalize(r['event_type'])),
                          'unlinked_identities':unlinked['identities'],'unlinked_events':unlinked['events']})
    selected = dict(extra.get('filters') or {})
    filtered = rows
    if selected.get('branch'):
        filtered = [r for r in filtered if str(r.get('branch_name') or '') == selected['branch']]
    if selected.get('area'):
        filtered = [r for r in filtered if str(r.get('area_name') or '') == selected['area']]
    if selected.get('employee'):
        term = normalize(selected['employee'])
        filtered = [r for r in filtered if term in normalize(r['employee_name']) or term in normalize(r['employee_code'])]
    if selected.get('regime'):
        filtered = [r for r in filtered if r.get('regime_code') == selected['regime']]
    if selected.get('punch_status'):
        filtered = [r for r in filtered if r['punch_status'] == selected['punch_status']]
    if selected.get('justification_status'):
        filtered = [r for r in filtered if r['justification_status'] == selected['justification_status']]
    if selected.get('result'):
        filtered = [r for r in filtered if r['operational_result'] == selected['result']]
    try: page_no = max(1, int(selected.get('page') or 1))
    except (TypeError,ValueError): page_no=1
    per_page = 50
    total = len(filtered)
    from ui_presenters import attendance_overview
    overview=attendance_overview(filtered)
    view=extra.get('request').query_params.get('vista','atencion') if extra.get('request') else 'todas'
    if view!='todas': filtered=overview['attention']
    total=len(filtered)
    page_no=min(page_no,max(1,(total+per_page-1)//per_page))
    display_rows = filtered[(page_no-1)*per_page:page_no*per_page]
    latest = latest_import_summary(cx)
    branches = cx.execute('SELECT name FROM branches WHERE active=1 ORDER BY name').fetchall()
    areas = cx.execute('SELECT DISTINCT name FROM areas WHERE active=1 ORDER BY name').fetchall()
    identities = pending_identities(cx) if actor['role'] == 'hr' else []
    batches = [dict(r) for r in cx.execute('''SELECT b.*,u.name AS importer_name FROM import_batches b
        LEFT JOIN users u ON u.id=b.imported_by ORDER BY b.id DESC LIMIT 10''')]
    for batch in batches:
        assessment=cx.execute('SELECT coverage_state FROM attendance_import_receipts WHERE batch_id=? ORDER BY id DESC LIMIT 1',(batch['id'],)).fetchone()
        if assessment:batch['coverage_reviewed']=assessment['coverage_state']=='CONFIRMED'
    staff = _reconciliation_users(cx) if actor['role'] == 'hr' else []
    roster_count=cx.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role<>'admin'").fetchone()[0]
    period_fragments=[]
    oct_first,oct_25,oct_26,oct_last=date(2026,10,1),date(2026,10,25),date(2026,10,26),date(2026,10,31)
    if first<=oct_25 and last>=oct_first:period_fragments.append('01/10–25/10/2026: fragmento del cierre 26/09–25/10')
    if first<=oct_last and last>=oct_26:period_fragments.append('26/10–31/10/2026: fragmento del cierre 26/10–25/11')
    active_extra = {k:v for k,v in extra.items() if k not in ('filters','request')}
    receipts=[]
    if actor['role'] in ('hr','admin'):
        receipts=[{**dict(r),'summary':json.loads(r['summary_json']),
            'devices':json.loads(r['device_states_json'])} for r in cx.execute('''SELECT r.*,b.filename,u.name actor_name
            FROM attendance_import_receipts r JOIN import_batches b ON b.id=r.batch_id JOIN users u ON u.id=r.actor_id
            WHERE r.requested_from<=? AND r.requested_to>=? ORDER BY r.id DESC LIMIT 20''',(last.isoformat(),first.isoformat()))]
    grouping_rule=cx.execute('SELECT * FROM attendance_grouping_rules ORDER BY valid_from DESC,id DESC LIMIT 1').fetchone()
    return {'actor':actor,'date_from':first.isoformat(),'date_to':last.isoformat(),'import_receipts':receipts,'grouping_rule':grouping_rule,
            'run':dict(run) if run else None,'stats':stats,'rows':display_rows,'total':total,'overview':overview,'view':view,
            'page_no':page_no,'page_count':max(1,(total+per_page-1)//per_page),
            'filters':selected,'latest_import':latest,'branches':branches,'areas':areas,
            'identities':identities,'batches':batches,'staff':staff,
            'period_fragments':period_fragments,
            'csrf':csrf_token(extra.get('request')) if extra.get('request') else '',
            **active_extra}


@app.get('/conciliacion',response_class=HTMLResponse)
async def reconciliation_page(request:Request,desde:str|None=None,hasta:str|None=None,
        sucursal:str|None=None,area:str|None=None,empleado:str|None=None,regimen:str|None=None,
        marcaciones:str|None=None,justificacion:str|None=None,resultado:str|None=None,pagina:str|None=None):
    with db() as cx:
        actor=require_actor(cx,request)
        if actor['role'] not in ('manager','hr','admin'):
            raise HTTPException(403,'No tienes acceso a conciliación.')
        latest=latest_import_summary(cx)
        first,last=_reconciliation_range(desde,hasta,latest)
        context=_reconciliation_render_context(cx,actor,first,last,request=request,filters={
            'branch':sucursal or '', 'area':area or '', 'employee':empleado or '', 'regime':regimen or '',
            'punch_status':marcaciones or '', 'justification_status':justificacion or '',
            'result':resultado or '', 'page':pagina or '1'})
        return page(request,'reconciliation.html',actor,**{k:v for k,v in context.items() if k!='actor'})


@app.post('/conciliacion/agrupacion/regla')
async def grouping_rule_save(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            start=date.fromisoformat(str(form.get('valid_from','')))
            seconds=int(str(form.get('window_seconds','')));note=str(form.get('note','')).strip()
            if not 1<=seconds<=300 or len(note)<12:raise ValueError('Ventana permitida: 1–300 segundos; registra respaldo de al menos 12 caracteres.')
            rid=cx.execute('''INSERT INTO attendance_grouping_rules(valid_from,window_seconds,note,created_by,created_at)
                VALUES(?,?,?,?,?)''',(start.isoformat(),seconds,note,actor['id'],nowstr())).lastrowid
            audit(cx,actor['id'],'REGLA_AGRUPACION','regla',rid,f'{seconds}s desde {start}; {note}')
            flash(request,'Nueva versión de agrupación registrada; las jornadas afectadas se actualizan sin alterar cierres.')
        except (ValueError,TypeError) as exc:flash(request,str(exc),False)
    return redirect('/conciliacion')


@app.post('/conciliacion/agrupacion/politica')
async def grouping_movement_policy_save(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            uid=int(str(form.get('user_id','')));seconds=int(str(form.get('boundary_seconds','')))
            start=date.fromisoformat(str(form.get('valid_from','')))
            end=date.fromisoformat(str(form.get('valid_to'))) if form.get('valid_to') else None
            note=str(form.get('note','')).strip()
            if not 1<=seconds<=3600 or len(note)<12 or (end and end<start):raise ValueError('Revisa proximidad, vigencia y respaldo de la política.')
            if not cx.execute("SELECT 1 FROM users WHERE id=? AND role<>'admin'",(uid,)).fetchone():raise ValueError('Trabajador inválido.')
            rid=cx.execute('''INSERT INTO attendance_movement_policies(user_id,valid_from,valid_to,boundary_seconds,note,approved_by,created_at)
                VALUES(?,?,?,?,?,?,?)''',(uid,start.isoformat(),end.isoformat() if end else None,seconds,note,actor['id'],nowstr())).lastrowid
            audit(cx,actor['id'],'POLITICA_MOVIMIENTOS','politica',rid,f'Usuario={uid}; {start}..{end}; {seconds}s; {note}')
            flash(request,'Política registrada. Los movimientos ambiguos seguirán pendientes; no se calculan atrasos.')
        except (ValueError,TypeError) as exc:flash(request,str(exc),False)
    return redirect('/conciliacion')


@app.post('/conciliacion/recalcular')
async def reconciliation_recalculate(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        latest=latest_import_summary(cx)
        first,last=_reconciliation_range(str(form.get('desde','')),str(form.get('hasta','')),latest)
        result=reconcile(cx,first,last,actor['id'])
        provisional=blocked_range(cx,first,last)
        audit(cx,actor['id'],'RECONCILIACION','rango',result['run']['id'],
              f'{first}..{last}; dias={result["stats"]["days_evaluated"]}; parcial={result["is_partial"]}; provisional_cierre={provisional}')
        flash(request,f"Conciliación calculada: {result['stats']['days_evaluated']} evaluaciones diarias. "
              f"{'Análisis provisional; no modifica cierres.' if provisional else 'No modifica cierres oficiales.'}")
    return redirect(f'/conciliacion?desde={first}&hasta={last}')


@app.post('/conciliacion/importar/preview')
async def reconciliation_import_preview(request:Request):
    return await _stage_unified_attendance_import(request,zlink_hr_only=True)


@app.post('/conciliacion/importar/confirmar')
async def reconciliation_import_confirm(request:Request):
    return await unified_import_confirm(request)


@app.post('/conciliacion/cobertura/{batch_id}')
async def reconciliation_confirm_coverage(request:Request,batch_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        row=cx.execute('SELECT * FROM import_batches WHERE id=? AND file_sha256<>\'\'',(batch_id,)).fetchone()
        if not row:raise HTTPException(404,'Lote de conciliación no encontrado.')
        confirm=str(form.get('confirm',''))=='1'
        if not confirm:raise HTTPException(400,'Debes confirmar expresamente la cobertura del lote.')
        if row['rows_error']:
            raise HTTPException(400,'El lote tiene filas observadas; resuelve sus incidencias antes de confirmar cobertura.')
        from attendance_continuous import process_import
        events=[dict(r) for r in cx.execute('SELECT * FROM biometric_events WHERE import_batch_id=?',(batch_id,))]
        preview={'rows':events,'date_from':row['coverage_start'],'date_to':row['coverage_end']}
        raw=str(form.get('expected_devices','')).strip()
        try:
            outcome=process_import(cx,preview,{'batch_id':batch_id,'added':0,'duplicate':0,'errors':0,
                'unlinked':row['rows_unlinked']},actor['id'],True,
                expected_devices=[v.strip() for v in raw.split(',') if v.strip()] if raw else None,
                no_event_devices=[v.strip() for v in str(form.get('no_event_devices','')).split(',') if v.strip()],
                note=str(form.get('coverage_note','')).strip()[:2000])
        except ValueError as exc:raise HTTPException(400,str(exc))
        audit(cx,actor['id'],'COBERTURA_ASISTENCIA','lote',batch_id,json.dumps(outcome,ensure_ascii=False))
        first,last=date.fromisoformat(row['coverage_start']),date.fromisoformat(row['coverage_end'])
        flash(request,f'Cobertura {"pendiente" if outcome["coverage_pending"] else "confirmada"}; '
            f'{outcome["recalculated_days"]} jornadas recalculadas. Consulta dispositivos en la última recepción.')
    return redirect(f'/conciliacion?desde={first}&hasta={last}')


@app.post('/conciliacion/identidades/{person_id}')
async def reconciliation_link_identity(request:Request,person_id:str):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        identity=cx.execute('SELECT * FROM biometric_persons WHERE person_id=?',(person_id,)).fetchone()
        if not identity:raise HTTPException(404,'Identidad biométrica no encontrada.')
        raw_user=str(form.get('user_id','')).strip()
        if raw_user:
            try:user_id=int(raw_user)
            except ValueError:raise HTTPException(400,'Trabajador inválido.')
            user=cx.execute("SELECT id FROM users WHERE id=? AND active=1 AND role<>'admin'",(user_id,)).fetchone()
            if not user:raise HTTPException(400,'Trabajador no disponible para vincular.')
            try:
                link_from=date.fromisoformat(str(form.get('valid_from','')))
                link_to=date.fromisoformat(str(form.get('valid_to',''))) if str(form.get('valid_to','')).strip() else None
            except ValueError:raise HTTPException(400,'La vigencia del vínculo debe usar fechas válidas.')
            if link_to and link_to<link_from:raise HTTPException(400,'La vigencia del vínculo es inválida.')
            note=str(form.get('note','')).strip()
            if len(note)<8:raise HTTPException(400,'Registra el respaldo del vínculo (mínimo 8 caracteres).')
            overlap=cx.execute('''SELECT * FROM biometric_identity_links WHERE person_id=?
                AND valid_from<=COALESCE(?,'9999-12-31') AND COALESCE(valid_to,'9999-12-31')>=? LIMIT 1''',
                (person_id,link_to.isoformat() if link_to else None,link_from.isoformat())).fetchone()
            if overlap:
                raise HTTPException(409,'Ya existe un vínculo biométrico que se superpone con esas fechas; conserva el historial y revisa el intervalo.')
            cx.execute('''INSERT INTO biometric_identity_links
                (person_id,user_id,valid_from,valid_to,confirmed_by,confirmed_at,note)
                VALUES(?,?,?,?,?,?,?)''',(person_id,user_id,link_from.isoformat(),
                link_to.isoformat() if link_to else None,actor['id'],nowstr(),note))
            status='CONFIRMED'
        else:
            user_id=None;status='PENDING';link_from=None;link_to=None;note=''
            closing=cx.execute('''SELECT * FROM biometric_identity_links WHERE person_id=? AND valid_to IS NULL''',
                               (person_id,)).fetchone()
            raw_close=str(form.get('valid_to','')).strip()
            if closing and raw_close:
                try:link_to=date.fromisoformat(raw_close)
                except ValueError:raise HTTPException(400,'La fecha de cierre del vínculo es inválida.')
                if link_to.isoformat()<closing['valid_from']:
                    raise HTTPException(400,'La fecha de cierre precede al inicio del vínculo.')
                cx.execute('UPDATE biometric_identity_links SET valid_to=? WHERE id=?',
                           (link_to.isoformat(),closing['id']))
                note='Vínculo cerrado por revisión de RR. HH.'
        cx.execute('''UPDATE biometric_persons SET linked_user_id=?,link_status=?,reviewed_by=?,reviewed_at=?,updated_at=?
            WHERE person_id=?''',(user_id,status,actor['id'],nowstr(),nowstr(),person_id))
        audit(cx,actor['id'],'VINCULACION_BIOMETRICA','identidad',person_id,
              f"{identity['display_name']}; user_id={user_id}; estado={status}; vigencia={link_from}..{link_to}; respaldo={note}")
        latest=latest_import_summary(cx)
        first,last=_reconciliation_range(str(form.get('desde','')),str(form.get('hasta','')),latest)
        # Dated link triggers invalidate only the old/new employee's affected journeys.
        flash(request,'Vinculación guardada. Los eventos originales siguen intactos.')
    return redirect(f'/conciliacion?desde={first}&hasta={last}')


@app.post('/conciliacion/regimen')
async def reconciliation_regime_save(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            user_id=int(str(form.get('user_id','0')))
            user=cx.execute("SELECT id FROM users WHERE id=? AND active=1 AND role<>'admin'",(user_id,)).fetchone()
            if not user:raise ValueError('Selecciona un trabajador activo.')
            start=date.fromisoformat(str(form.get('valid_from','')))
            end=date.fromisoformat(str(form.get('valid_to',''))) if str(form.get('valid_to','')).strip() else None
            employment_start=date.fromisoformat(str(form.get('employment_from','')))
            employment_end=date.fromisoformat(str(form.get('employment_to',''))) if str(form.get('employment_to','')).strip() else None
            if end and end<start:raise ValueError('Vigencia del régimen inválida.')
            if employment_end and employment_end<employment_start:raise ValueError('Vigencia laboral inválida.')
            regime=str(form.get('regime_code',''))
            allowed=('BRANCH_STANDARD','KIMSTAR_ROTATING','MOBILE_NO_FIXED_HOURS','CUSTOM')
            if regime not in allowed:raise ValueError('Régimen no permitido.')
            sub=str(form.get('submodality',''))
            if regime=='MOBILE_NO_FIXED_HOURS' and sub not in ('DAYS','ASSIGNMENTS'):
                raise ValueError('Selecciona la modalidad móvil por días o por asignaciones.')
            if regime=='KIMSTAR_ROTATING':sub='ROTATING'
            weekdays=sorted({int(x) for x in form.getlist('weekday') if str(x).isdigit() and 0<=int(x)<=6})
            if regime=='BRANCH_STANDARD' and not weekdays:weekdays=[0,1,2,3,4,5]
            if (regime=='MOBILE_NO_FIXED_HOURS' and sub=='DAYS' or regime=='CUSTOM') and not weekdays:
                raise ValueError('Selecciona los días de trabajo confirmados.')
            note=str(form.get('note','')).strip()
            if len(note)<8:raise ValueError('Registra quién confirmó la condición y su respaldo (mínimo 8 caracteres).')
            confirm_employment=str(form.get('confirm_employment',''))=='1'
            for table, new_start, new_end in (
                ('attendance_employment_periods',employment_start.isoformat(),employment_end.isoformat() if employment_end else None),
                ('attendance_regime_assignments',start.isoformat(),end.isoformat() if end else None)):
                overlap=cx.execute(f'''SELECT id,valid_from,valid_to FROM {table} WHERE user_id=?
                    AND valid_from<=COALESCE(?, '9999-12-31') AND COALESCE(valid_to,'9999-12-31')>=?''',
                    (user_id,new_end,new_start)).fetchone()
                if overlap:
                    if overlap['valid_from']<new_start and (not new_end or overlap['valid_from']<=new_end):
                        cx.execute(f'UPDATE {table} SET valid_to=? WHERE id=?',( (date.fromisoformat(new_start)-timedelta(days=1)).isoformat(),overlap['id']))
                    else:
                        raise ValueError('El trabajador ya tiene una vigencia superpuesta; corrige el período antes de guardar.')
            cx.execute('''INSERT INTO attendance_employment_periods
                (user_id,valid_from,valid_to,confirmed,note,assigned_by,created_at) VALUES(?,?,?,?,?,?,?)''',
                (user_id,employment_start.isoformat(),employment_end.isoformat() if employment_end else None,
                 int(confirm_employment),note,actor['id'],nowstr()))
            cx.execute('''INSERT INTO attendance_regime_assignments
                (user_id,regime_code,submodality,weekdays_json,shift_start,shift_end,valid_from,valid_to,
                 confirmed,note,assigned_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (user_id,regime,sub,json.dumps(weekdays),str(form.get('shift_start','')).strip() or None,
                 str(form.get('shift_end','')).strip() or None,start.isoformat(),end.isoformat() if end else None,
                 1,note,actor['id'],nowstr()))
            audit(cx,actor['id'],'REGIMEN_ASISTENCIA','usuario',user_id,
                  f'{regime}/{sub}; vigencia={start}..{end}; relación laboral confirmada={confirm_employment}; {note}')
            latest=latest_import_summary(cx)
            recalc_to=end or max(start,date.fromisoformat(latest['coverage_end'])) if latest and latest.get('coverage_end') else end or start
            # Effective-date triggers enqueue the affected employee, without rebuilding every employee.
            flash(request,'Vigencias guardadas. La relación laboral solo se confirmó si marcaste la casilla correspondiente.')
        except (ValueError,TypeError) as exc:
            flash(request,str(exc),False)
            return redirect('/conciliacion')
    return redirect(f'/conciliacion?desde={start}&hasta={end or start}')


@app.post('/conciliacion/asignacion')
async def reconciliation_work_assignment(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            user_id=int(str(form.get('user_id','0')));work_date=date.fromisoformat(str(form.get('work_date','')))
            description=str(form.get('description','')).strip();location=str(form.get('location','')).strip()
            if len(description)<8:raise ValueError('Describe la instrucción o servicio confirmado (mínimo 8 caracteres).')
            if not cx.execute("SELECT 1 FROM users WHERE id=? AND active=1 AND role<>'admin'",(user_id,)).fetchone():
                raise ValueError('Trabajador inválido.')
            employment=cx.execute('''SELECT 1 FROM attendance_employment_periods WHERE user_id=? AND confirmed=1
                AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',(user_id,work_date.isoformat(),work_date.isoformat())).fetchone()
            regime=cx.execute('''SELECT 1 FROM attendance_regime_assignments WHERE user_id=? AND confirmed=1
                AND regime_code='MOBILE_NO_FIXED_HOURS' AND submodality='ASSIGNMENTS'
                AND valid_from<=? AND (valid_to IS NULL OR valid_to>=?)''',(user_id,work_date.isoformat(),work_date.isoformat())).fetchone()
            if not employment or not regime:raise ValueError('Confirma primero la relación laboral y la modalidad móvil B del trabajador.')
            cx.execute('''INSERT OR IGNORE INTO attendance_work_assignments
                (user_id,work_date,description,location,created_by,created_at) VALUES(?,?,?,?,?,?)''',
                (user_id,work_date.isoformat(),description,location,actor['id'],nowstr()))
            audit(cx,actor['id'],'ASIGNACION_JORNADA','usuario',user_id,
                  f'{work_date}; {description}; lugar={location}')
            # The source trigger queues this employee/date automatically.
            flash(request,'Asignación documentada y fecha recalculada.')
        except (ValueError,TypeError) as exc:
            flash(request,str(exc),False)
    return redirect('/conciliacion')


@app.post('/conciliacion/calendario')
async def reconciliation_calendar_event(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            title=str(form.get('title','')).strip();kind=str(form.get('event_type',''))
            first=date.fromisoformat(str(form.get('date_from','')));last=date.fromisoformat(str(form.get('date_to','')))
            user_raw=str(form.get('user_id','')).strip();branch_raw=str(form.get('branch_id','')).strip()
            user_id=int(user_raw) if user_raw else None;branch_id=int(branch_raw) if branch_raw else None
            allowed=('FERIADO','ASUETO','CUMPLEANOS','DIA_MUJER','DESCANSO_ESPECIAL','OTRO')
            if kind not in allowed or len(title)<3 or last<first:raise ValueError('Completa un evento autorizado y un rango válido.')
            if user_id and branch_id:raise ValueError('El evento aplica a persona o sucursal, no a ambos.')
            if kind in ('CUMPLEANOS','DIA_MUJER') and not (user_id or str(form.get('eligibility','')).strip()):
                raise ValueError('Define por escrito la elegibilidad; no se infiere por nombre ni apariencia.')
            rule_note=str(form.get('rule_note','')).strip()
            if len(rule_note)<8:raise ValueError('Registra la autorización y la regla aplicable (mínimo 8 caracteres).')
            if user_id and not cx.execute('SELECT 1 FROM users WHERE id=? AND active=1',(user_id,)).fetchone():
                raise ValueError('Trabajador inválido.')
            if branch_id and not cx.execute('SELECT 1 FROM branches WHERE id=? AND active=1',(branch_id,)).fetchone():
                raise ValueError('Sucursal inválida.')
            cur=cx.execute('''INSERT INTO attendance_calendar_events
                (title,event_type,date_from,date_to,branch_id,user_id,eligibility,approved_by,rule_note,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(title,kind,first.isoformat(),last.isoformat(),branch_id,user_id,
                str(form.get('eligibility','')).strip(),actor['id'],rule_note,actor['id'],nowstr()))
            audit(cx,actor['id'],'CALENDARIO_CONCILIACION','evento',cur.lastrowid,
                  f'{kind} {first}..{last}; sucursal={branch_id}; usuario={user_id}; regla={rule_note}')
            # The source trigger respects event dates and employee/branch scope.
            flash(request,'Evento autorizado guardado y fechas recalculadas. No se trasladó a otra fecha.')
        except (ValueError,TypeError) as exc:
            flash(request,str(exc),False)
    return redirect('/conciliacion')


@app.get('/conciliacion/detalle',response_class=HTMLResponse)
async def reconciliation_detail(request:Request,user_id:int,fecha:str,run_id:int|None=None):
    with db() as cx:
        actor=require_actor(cx,request)
        if actor['role'] not in ('manager','hr','admin'):raise HTTPException(403,'No tienes acceso a incidencias.')
        try:work_date=date.fromisoformat(fecha)
        except ValueError:raise HTTPException(400,'Fecha inválida.')
        if run_id:
            run=cx.execute('SELECT * FROM attendance_reconciliation_runs WHERE id=?',(run_id,)).fetchone()
        else:
            run=cx.execute('''SELECT r.* FROM attendance_daily_current c JOIN attendance_daily_versions v ON v.id=c.version_id
                JOIN attendance_reconciliation_days d ON d.id=v.evaluation_id JOIN attendance_reconciliation_runs r ON r.id=d.run_id
                WHERE c.user_id=? AND c.work_date=?''',(user_id,fecha)).fetchone()
            if not run:
                run=cx.execute('''SELECT * FROM attendance_reconciliation_runs WHERE date_from<=? AND date_to>=?
                    ORDER BY id DESC LIMIT 1''',(fecha,fecha)).fetchone()
        if not run:raise HTTPException(404,'No existe una conciliación guardada para esa fecha.')
        row=cx.execute('''SELECT d.*,u.name AS employee_name,u.employee_code,u.position,
            json_extract(d.detail_json,'$.branch_name') AS branch_name,
            json_extract(d.detail_json,'$.area_name') AS area_name FROM attendance_reconciliation_days d
            JOIN users u ON u.id=d.user_id WHERE d.run_id=? AND d.user_id=? AND d.work_date=?''',
            (run['id'],user_id,fecha)).fetchone()
        if not row:raise HTTPException(404,'Incidencia no encontrada.')
        if actor['role']=='manager':
            visible=_manager_has_scope(cx,actor['id'],user_id,fecha)
            if not visible:raise HTTPException(403,'Esta persona no pertenece a tu ámbito autorizado para esa fecha.')
        detail=json.loads(row['detail_json'])
        request_ids=sorted({int(item['id']) for key in ('approved_requests','pending_requests','rejected_requests','correction_requests')
                            for item in detail.get(key,[])})
        detail['approval_steps']=[]
        if request_ids:
            marks=','.join('?' for _ in request_ids)
            detail['approval_steps']=[dict(item) for item in cx.execute(f'''SELECT s.*,COALESCE(NULLIF(s.assignee_name_snapshot,''),u.name,'') AS reviewer_name
                FROM request_approval_steps s LEFT JOIN users u ON u.id=s.assignee_id
                WHERE s.request_id IN ({marks}) ORDER BY s.request_id,s.step_order''',request_ids)]
        identities=cx.execute('''SELECT DISTINCT p.person_id,p.display_name,p.department FROM biometric_persons p
            JOIN biometric_identity_links l ON l.person_id=p.person_id WHERE l.user_id=?
            AND l.valid_from<=? AND (l.valid_to IS NULL OR l.valid_to>=?) ORDER BY p.person_id''',
            (user_id,fecha,fecha)).fetchall()
        return page(request,'reconciliation_detail.html',actor,row=dict(row),detail=detail,run=dict(run),
                    identities=identities,can_edit=actor['role']=='hr')


@app.get('/conciliacion/identidad/{person_id}',response_class=HTMLResponse)
async def reconciliation_biometric_identity(request:Request,person_id:str,desde:str|None=None,hasta:str|None=None):
    with db() as cx:
        actor=require_actor(cx,request)
        if actor['role'] not in ('manager','hr','admin'):
            raise HTTPException(403,'No tienes acceso a identidades biométricas.')
        identity=cx.execute('''SELECT p.*,u.name AS linked_name,u.employee_code FROM biometric_persons p
            LEFT JOIN users u ON u.id=p.linked_user_id WHERE p.person_id=?''',(person_id,)).fetchone()
        if not identity:raise HTTPException(404,'Identidad biométrica no encontrada.')
        date_filter='';params=[person_id]
        first=last=None
        if desde or hasta:
            try:
                first=date.fromisoformat(desde) if desde else date(1900,1,1)
                last=date.fromisoformat(hasta) if hasta else date(9999,12,31)
            except ValueError:raise HTTPException(400,'Rango de fechas inválido.')
            if last<first:raise HTTPException(400,'Rango de fechas inválido.')
            date_filter=' AND e.occurred_at>=? AND e.occurred_at<=?'
            params.extend([first.isoformat()+' 00:00:00',last.isoformat()+' 23:59:59'])
        events=[dict(row) for row in cx.execute('''SELECT e.*,b.filename AS filename,
            l.user_id AS linked_user_id,l.valid_from AS identity_valid_from,l.valid_to AS identity_valid_to,
            u.name AS linked_employee_name FROM biometric_events e
            JOIN import_batches b ON b.id=e.import_batch_id
            LEFT JOIN biometric_identity_links l ON l.person_id=e.person_id
              AND l.valid_from<=substr(e.occurred_at,1,10)
              AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
            LEFT JOIN users u ON u.id=l.user_id WHERE e.person_id=?'''+date_filter+
            ' ORDER BY e.occurred_at,e.id',params)]
        if actor['role']=='manager':
            events=[row for row in events if row['linked_user_id'] and
                    _manager_has_scope(cx,actor['id'],row['linked_user_id'],row['occurred_at'][:10])]
            if not events:raise HTTPException(403,'La identidad no tiene marcaciones vinculadas a tu equipo en ese período.')
        summary={'events':len(events),'days':len({row['occurred_at'][:10] for row in events}),
                 'devices':len({row['device_sn'] for row in events if row['device_sn']}),
                 'sources':dict(Counter(row['source'] for row in events))}
        dates=sorted({row['occurred_at'][:10] for row in events})
        staff=_reconciliation_users(cx) if actor['role']=='hr' else []
        roster_context={}
        if actor['role']=='hr':
            roster_context={
                'roster_staff':[dict(r) for r in cx.execute("SELECT id,name,employee_code,document_id,active FROM users WHERE role<>'admin' ORDER BY name")],
                'roster_branches':[dict(r) for r in cx.execute("SELECT b.id,b.name FROM branches b JOIN companies c ON c.id=b.company_id WHERE b.active=1 AND b.classification_status='CLASIFICADA' AND c.name='KIMTEK'")],
                'roster_areas':[dict(r) for r in cx.execute('SELECT id,name,branch_id FROM areas WHERE active=1')],
                'roster_managers':[dict(r) for r in cx.execute("SELECT id,name,employee_code FROM users WHERE role='manager' ORDER BY name")],
                'roster_responsibilities':[dict(r) for r in cx.execute('SELECT user_id,area_id,valid_from,valid_to FROM area_responsibles')],
                'roster_links':[dict(r) for r in cx.execute('''SELECT l.*,u.name FROM biometric_identity_links l JOIN users u ON u.id=l.user_id WHERE l.person_id=? ORDER BY l.valid_from''',(person_id,))],
                'roster_reviews':[dict(r) for r in cx.execute('''SELECT r.*,u.name AS reviewer FROM biometric_roster_reviews r JOIN users u ON u.id=r.reviewed_by WHERE person_id=? ORDER BY r.id DESC''',(person_id,))],
            }
        return page(request,'reconciliation_identity.html',actor,identity=dict(identity),events=events,
                    summary=summary,active_dates=dates,staff=staff,can_edit=actor['role']=='hr',
                    date_from=desde or '',date_to=hasta or '',**roster_context)


@app.get('/conciliacion/padron',response_class=HTMLResponse)
async def biometric_roster_page(request:Request):
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        rows=[dict(r) for r in cx.execute('''SELECT p.*,COUNT(e.id) AS events,
            COUNT(DISTINCT substr(e.occurred_at,1,10)) AS days,
            (SELECT classification FROM biometric_roster_reviews r WHERE r.person_id=p.person_id ORDER BY r.id DESC LIMIT 1) AS classification,
            (SELECT u.name FROM users u WHERE u.id=p.linked_user_id) AS employee_name
            FROM biometric_persons p LEFT JOIN biometric_events e ON e.person_id=p.person_id
            GROUP BY p.person_id ORDER BY p.display_name''')]
        return page(request,'biometric_roster.html',actor,rows=rows)


@app.get('/conciliacion/referencia-rrhh',response_class=HTMLResponse)
async def hr_reference_page(request:Request,ci:str='',pagina:int=1):
    from hr_reference import compare_reference
    with db() as cx:
        actor=require_actor(cx,request);require_hr(actor)
        reference=cx.execute('SELECT * FROM hr_reference_imports ORDER BY id DESC LIMIT 1').fetchone()
        report=compare_reference(cx,reference['id']) if reference else None
        proposals=[dict(r) for r in cx.execute('SELECT * FROM hr_roster_proposals WHERE import_id=? ORDER BY original_name',
            (reference['id'],))] if reference else []
        rows=report['rows'] if report else []
        if ci:rows=[r for r in rows if ci.lower() in r['ci'].lower() or ci.lower() in r['name'].lower()]
        total=len(rows);pagina=max(1,pagina)
        return page(request,'hr_reference.html',actor,reference=reference,
            stats=json.loads(reference['stats_json']) if reference else {},report=report,
            proposals=proposals,rows=rows[(pagina-1)*50:pagina*50],ci=ci,pagina=pagina,
            pages=max(1,(total+49)//50),staff=cx.execute("SELECT id,name,employee_code,document_id FROM users WHERE role<>'admin' ORDER BY name").fetchall())


@app.post('/conciliacion/referencia-rrhh/importar')
async def hr_reference_import(request:Request):
    from hr_reference import import_reference
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        a=form.get('detailed');b=form.get('summary')
        if not isinstance(a,UploadFile) or not isinstance(b,UploadFile):raise HTTPException(400,'Selecciona ambos reportes XLSX.')
        raw_a=await a.read(20*1024*1024+1);raw_b=await b.read(20*1024*1024+1)
        import_reference(cx,raw_a,raw_b,a.filename or 'Reporte RRHH',actor['id'])
        flash(request,'Referencias y propuestas cargadas. No se crearon cuentas ni autorizaciones.')
    return redirect('/conciliacion/referencia-rrhh')


@app.post('/conciliacion/referencia-rrhh/{proposal_id}/confirmar')
async def hr_reference_confirm(request:Request,proposal_id:int):
    from hr_reference import confirm_proposal
    form=await request.form();check_csrf(request,form)
    try:
        with db() as cx:
            actor=require_actor(cx,request);require_personnel_hr(actor)
            uid=int(form.get('user_id')) if form.get('user_id') else None
            confirm_proposal(cx,proposal_id,actor['id'],uid,str(form.get('note','')),
                str(form.get('valid_from','')).strip() or None,str(form.get('valid_to','')).strip() or None)
            if uid:
                from reconciliation import reconcile
                reconcile(cx,date(2026,10,1),date(2026,10,31),actor['id'],user_ids=[uid],reason='Confirmación de correspondencia histórica')
        flash(request,'Propuesta verificada. Fechas laborales, encargado y calendario siguen pendientes donde no exista respaldo.')
    except (ValueError,sqlite3.IntegrityError) as exc:flash(request,'No se guardaron cambios: '+str(exc),False)
    return redirect('/conciliacion/referencia-rrhh')


@app.post('/conciliacion/identidad/{person_id}/padron')
async def biometric_roster_confirm(request:Request,person_id:str):
    form=await request.form();check_csrf(request,form)
    try:
        with db() as cx:
            actor=require_actor(cx,request);require_personnel_hr(actor)
            confirm_roster_identity(cx,actor,person_id,form)
        flash(request,'Padrón guardado con respaldo. Las jornadas afectadas fueron recalculadas como análisis provisional.')
    except (ValueError,TypeError,sqlite3.IntegrityError) as exc:
        flash(request,'No se guardaron cambios: '+str(exc),False)
    return redirect('/conciliacion/identidad/'+person_id)


@app.post('/conciliacion/resolver')
async def reconciliation_resolve(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        actor=require_actor(cx,request);require_personnel_hr(actor)
        try:
            user_id=int(str(form.get('user_id','0')));work_date=date.fromisoformat(str(form.get('work_date','')))
            kind=str(form.get('resolution_type',''));reason=str(form.get('reason','')).strip();evidence=str(form.get('evidence','')).strip()
            if kind not in ('JUSTIFICACION','FALTA_CONFIRMADA') or len(reason)<10:
                raise ValueError('Indica el tipo y el motivo de la decisión (mínimo 10 caracteres).')
            if len(evidence)<5:raise ValueError('Registra la evidencia consultada.')
            if kind=='FALTA_CONFIRMADA':
                row=cx.execute('''SELECT d.*,json_extract(d.detail_json,'$.employment_confirmed') AS employment_confirmed,
                    json_extract(d.detail_json,'$.coverage_reviewed') AS coverage_reviewed
                    FROM attendance_reconciliation_days d WHERE d.user_id=? AND d.work_date=?
                    ORDER BY d.id DESC LIMIT 1''',(user_id,work_date.isoformat())).fetchone()
                if not row or not row['employment_confirmed'] or not row['coverage_reviewed']:
                    raise ValueError('No se puede confirmar: relación laboral y cobertura completa deben estar verificadas.')
                if json.loads(row['detail_json']).get('journey',{}).get('state')!='JORNADA FINALIZADA':
                    raise ValueError('No se puede confirmar: la jornada no finalizó con horario y sincronización verificados.')
                if row['work_obligation'] not in ('TRABAJO PROGRAMADO','ASIGNACIÓN CONFIRMADA','TURNO/ASIGNACIÓN CONFIRMADA'):
                    raise ValueError('No se puede confirmar: falta una jornada o asignación laboral confirmada para esta fecha.')
                if row['punch_count'] or row['justification_status'] in ('PERMISO APROBADO','VACACIONES APROBADAS','LICENCIA APROBADA','ASUETO ESPECIAL','FERIADO','DESCANSO','REGULARIZACIÓN APROBADA'):
                    raise ValueError('La evidencia muestra actividad o una justificación aplicable; revisa la incidencia antes de confirmar.')
                if row['justification_status']=='SOLICITUD PENDIENTE':
                    raise ValueError('Hay una solicitud pendiente; no se puede confirmar una falta mientras siga abierta.')
                if row['operational_result'] not in ('POSIBLE INASISTENCIA','INFORMACIÓN INSUFICIENTE'):
                    raise ValueError('El resultado actual no es confirmable como falta.')
            cx.execute('UPDATE attendance_resolutions SET active=0 WHERE user_id=? AND work_date=? AND active=1',
                       (user_id,work_date.isoformat()))
            cx.execute('''INSERT INTO attendance_resolutions
                (user_id,work_date,resolution_type,reason,evidence,created_by,created_at)
                VALUES(?,?,?,?,?,?,?)''',(user_id,work_date.isoformat(),kind,reason,evidence,actor['id'],nowstr()))
            result=reconcile(cx,work_date,work_date,actor['id'],user_ids=[user_id])
            audit(cx,actor['id'],'RESOLUCION_ASISTENCIA','usuario',user_id,
                  f'{work_date}; {kind}; motivo={reason}; evidencia={evidence}; run={result["run"]["id"]}')
            audit(cx,actor['id'],'RECONCILIACION','rango',result['run']['id'],f'Resolución {kind}; {work_date}; user={user_id}')
            flash(request,'Decisión de Recursos Humanos registrada con evidencia y trazabilidad.')
        except (ValueError,TypeError) as exc:
            flash(request,str(exc),False)
            return redirect('/conciliacion')
    return redirect(f'/conciliacion/detalle?user_id={user_id}&fecha={work_date}')

@app.get('/reportes',response_class=HTMLResponse)
async def reports_page(request:Request,fin:str|None=None):
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        if fin:
            try:end=date.fromisoformat(fin)
            except ValueError:raise HTTPException(400,'Fecha invalida.')
            if end.day!=25:raise HTTPException(400,'Fecha de cierre debe ser 25.')
        else:end=month_end_for_day(now().date())
        payload,closed=get_report(cx,end)
        consolidation=payload.get('consolidation',{})
        close_ready=(not consolidation.get('required') or consolidation.get('ready',False))
        closure=cx.execute('SELECT * FROM monthly_closures WHERE period_end=?',(end.isoformat(),)).fetchone()
        postclosure_events=0
        postclosure_revisions=0
        if closed and closure:
            postclosure_revisions=cx.execute('''SELECT COUNT(*) FROM attendance_daily_current c
                JOIN attendance_daily_versions v ON v.id=c.version_id
                WHERE c.work_date BETWEEN ? AND ? AND v.closed_period_analysis=1 AND v.created_at>=?''',
                (period_start(end).isoformat(),end.isoformat(),closure['closed_at'])).fetchone()[0]
            postclosure_events=cx.execute('''SELECT COUNT(*) FROM biometric_events e JOIN import_batches b
                ON b.id=e.import_batch_id WHERE e.occurred_at>=? AND e.occurred_at<=? AND e.imported_at>?''',
                (period_start(end).isoformat()+' 00:00:00',end.isoformat()+' 23:59:59',closure['closed_at'])).fetchone()[0]
            if postclosure_events:
                payload=dict(payload);payload['postclosure_zkbio_events']=postclosure_events
        versions=[]
        if closure:
            versions=cx.execute('''SELECT v.*,u.name AS actor_name
                FROM monthly_closure_versions v LEFT JOIN users u ON u.id=v.actor_id
                WHERE v.closure_id=? ORDER BY v.version DESC''',(closure['id'],)).fetchall()
        return page(request,'reports.html',a,period=payload,closed=closed,closure=closure,
                    fin=end.isoformat(),period_label=period_label(period_start(end),end),
                    can_close=(now().date()>end or (now().date()==end and (now().hour,now().minute)>=(23,59))) and close_ready,
                    consolidation=consolidation,
                    postclosure_events=postclosure_events,
                    postclosure_revisions=postclosure_revisions,
                    closure_versions=versions)

@app.post('/reportes/cerrar')
async def close_post(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        try:
            end=date.fromisoformat(str(form.get('fin','')))
            changed=close_period(cx,end,a['id'])
            flash(request,'Cierre guardado y congelado.' if changed else 'Periodo ya cerrado.')
        except ValueError as exc:
            flash(request,str(exc),False)
            return redirect('/reportes')
    return redirect(f'/reportes?fin={end.isoformat()}')

@app.post('/reportes/reabrir')
async def reopen_post(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        try:
            end=date.fromisoformat(str(form.get('fin','')))
            reopen_period(cx,end,a['id'],str(form.get('reason','')))
            flash(request,'Periodo reabierto; la operación quedó auditada.')
        except ValueError as exc:
            flash(request,str(exc),False);return redirect('/reportes')
    return redirect(f'/reportes?fin={end.isoformat()}')

@app.get('/reportes/descargar.xlsx')
async def report_download(request:Request,fin:str):
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        try:end=date.fromisoformat(fin)
        except ValueError:raise HTTPException(400,'Fecha invalida.')
        payload,closed=get_report(cx,end)
        closure=cx.execute('SELECT closed_at FROM monthly_closures WHERE period_end=? AND state=\'CERRADO\'',
                           (end.isoformat(),)).fetchone()
        if closed and closure:
            count=cx.execute('''SELECT COUNT(*) FROM biometric_events e WHERE e.occurred_at>=? AND e.occurred_at<=?
                AND e.imported_at>?''',(period_start(end).isoformat()+' 00:00:00',end.isoformat()+' 23:59:59',
                closure['closed_at'])).fetchone()[0]
            if count:
                payload=dict(payload);payload['postclosure_zkbio_events']=count
        excel=excel_report(cx,payload,closed)
        audit(cx,a['id'],'DESCARGA_EXCEL','periodo',fin,'CERRADO' if closed else 'BORRADOR')
        return StreamingResponse(io.BytesIO(excel),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition':f'attachment; filename="Asistencia_{fin}.xlsx"'})

@app.get('/auditoria',response_class=HTMLResponse)
async def audit_page(request:Request,pagina:int=1,q:str=''):
    with db() as cx:
        a=require_actor(cx,request);require_hr(a)
        query='%'+q.strip()[:150]+'%'
        total=cx.execute('SELECT COUNT(*) FROM audit_log WHERE details LIKE ? OR action LIKE ? OR entity LIKE ?',(query,query,query)).fetchone()[0]
        pages=max(1,(total+49)//50);pagina=max(1,min(pagina,pages))
        rows=cx.execute('''SELECT l.*,u.name AS actor_name FROM audit_log l LEFT JOIN users u ON u.id=l.actor_id
            WHERE l.details LIKE ? OR l.action LIKE ? OR l.entity LIKE ? ORDER BY l.id DESC LIMIT 50 OFFSET ?''',
            (query,query,query,(pagina-1)*50)).fetchall()
        return page(request,'audit.html',a,rows=rows,total=total,page_no=pagina,pages=pages,search=q)

@app.get('/notificaciones',response_class=HTMLResponse)
async def notifications_page(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        rows=cx.execute('''SELECT * FROM internal_notifications
                           WHERE user_id=? ORDER BY id DESC LIMIT 100''',(a['id'],)).fetchall()
        return page(request,'notifications.html',a,rows=rows)

@app.post('/notificaciones/{notification_id}/leer')
async def mark_notification_read(request:Request,notification_id:int):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request)
        cx.execute('UPDATE internal_notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL',(nowstr(),notification_id,a['id']))
    return redirect('/notificaciones')

@app.post('/notificaciones/leer')
async def mark_notifications_read(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request)
        cx.execute('''UPDATE internal_notifications SET read_at=?
                      WHERE user_id=? AND read_at IS NULL''',(nowstr(),a['id']))
    return redirect('/notificaciones')

@app.get('/mi-perfil',response_class=HTMLResponse)
async def profile(request:Request):
    with db() as cx:
        a=require_actor(cx,request)
        return page(request,'profile.html',a)

@app.post('/mi-perfil/contrasena')
async def change_password(request:Request):
    form=await request.form();check_csrf(request,form)
    with db() as cx:
        a=require_actor(cx,request)
        current=str(form.get('current',''))
        password=str(form.get('new',''))
        confirm=str(form.get('confirm',''))
        if not verify_password(current,a['password_hash']):
            flash(request,'Contraseña actual incorrecta.',False)
        elif password!=confirm:
            flash(request,'Las nuevas contraseñas no coinciden.',False)
        else:
            try:
                newhash=hash_password(password)
                cx.execute('UPDATE users SET password_hash=? WHERE id=?',(newhash,a['id']))
                audit(cx,a['id'],'CAMBIO_PASSWORD','usuario',a['id'],'Cambio voluntario')
                flash(request,'Contraseña actualizada.')
            except ValueError as exc:flash(request,str(exc),False)
    return redirect('/mi-perfil')

if __name__ == '__main__':
    if len(sys.argv)>1 and sys.argv[1]=='cierre-automatico':
        initialize()
        print('Cierre nuevo:',auto_close_if_due())
    elif len(sys.argv)>1 and sys.argv[1]=='iniciar-admin':
        import getpass
        initialize()
        with db() as cx:
            if cx.execute('SELECT COUNT(*) n FROM users').fetchone()['n']:
                print('Ya existe personal; inicializacion no permitida.');sys.exit(1)
            company_area=input('Nombre del área inicial: ').strip() or 'Administración'
            email=input('Correo del administrador: ').strip().lower()
            name=input('Nombre del administrador: ').strip()
            password=getpass.getpass('Contraseña (minimo 10 caracteres): ')
            if len(name)<3 or '@' not in email:
                print('Nombre o correo inválido.');sys.exit(1)
            h=hash_password(password)
            cur=cx.execute('INSERT INTO areas(name) VALUES(?)',(company_area,))
            cx.execute('''INSERT INTO users(employee_code,name,area_id,email,role,password_hash,position,created_at)
                       VALUES(?,?,?,?,?,?,?,?)''',('ADMIN',name,cur.lastrowid,email,'admin',h,'Administrador',nowstr()))
        print('Administrador creado correctamente.')
    else:
        print('Comandos: python app.py iniciar-admin | cierre-automatico')
        print('Para iniciar la web: uvicorn app:app --host 127.0.0.1 --port 8000')
