"""Historical HR evidence and verifiable roster proposals, never authorization imports."""
from collections import Counter,defaultdict
from datetime import date,datetime
import hashlib
from io import BytesIO
import json
import re
from openpyxl import load_workbook
from core import nowstr,audit


def _code(value):return str(value).strip().removesuffix('.0')


def _lines(text):
    out=[]
    for line in text.splitlines():
        match=re.search(r'\b(\d{2}:\d{2})\b',line)
        if match:
            out.append({'time':match[1],'text':line,'provenance':
                'E_HR_UNVERIFIED_ANNOTATION' if 'Permiso:' in line else
                'B_ZLINK_MANUAL' if 'Origen:' in line else 'A_ZLINK_PHYSICAL'})
    return out


def import_reference(cx,detailed,summary,filename,actor_id):
    from reconciliation import normalize
    if len(detailed)>20*1024*1024 or len(summary)>20*1024*1024:raise ValueError('Máximo 20 MB por archivo.')
    dh=hashlib.sha256(detailed).hexdigest();sh=hashlib.sha256(summary).hexdigest()
    previous=cx.execute('SELECT id FROM hr_reference_imports WHERE detailed_hash=? AND summary_hash=?',(dh,sh)).fetchone()
    if previous:return previous['id']
    def read(content):
        w=load_workbook(BytesIO(content),read_only=True,data_only=True)
        try:return list(w.active.values)
        finally:w.close()
    d=read(detailed);s=read(summary)
    if len(d)<6 or len(s)<6 or tuple(d[4][:6])!=('Nro','Sucursal','Departamento','Cargo','CI','Empleado'):
        raise ValueError('No se reconoce la estructura del reporte detallado.')
    if tuple(s[4][:4])!=('Nro','Sucursal','Cargo','Empleado'):raise ValueError('No se reconoce el reporte resumido.')
    if '2026-10-01 al 2026-10-31' not in str(d[1][0]) or d[1][0]!=s[1][0]:
        raise ValueError('Esta comparación histórica admite exclusivamente los dos reportes de octubre de 2026.')
    for n in range(1,32):
        if f'{n:02d}/10' not in str(d[4][5+n]) or f'{n:02d}/10' not in str(s[4][3+n]):
            raise ValueError('Las columnas diarias no coinciden con octubre.')
    summary_rows={(normalize(r[1]),int(r[0])):r for r in s[5:] if r[0] is not None}
    rows=[r for r in d[5:] if r[0] is not None]
    if len({_code(r[4]) for r in rows})!=len(rows):raise ValueError('El padrón contiene CI duplicados; no se importó.')
    stats=Counter();stats['workers']=len(rows)
    source_id=cx.execute('''INSERT INTO hr_reference_imports(detailed_hash,summary_hash,filename,imported_by,imported_at,stats_json)
        VALUES(?,?,?,?,?,'{}')''',(dh,sh,filename[:200],actor_id,nowstr())).lastrowid
    branches={normalize(r['name']):r['id'] for r in cx.execute("SELECT b.* FROM branches b JOIN companies c ON c.id=b.company_id WHERE c.name='KIMTEK'")}
    aliases={'central kimtek':'sede central','mercatech murillo':'mercatech la paz','mercatech riosinho':'mercatech riosinio'}
    for row in rows:
        ref=summary_rows.get((normalize(row[1]),int(row[0])))
        if not ref or normalize(ref[3])!=normalize(row[5]):raise ValueError('Los trabajadores de ambos reportes no coinciden.')
        ci=_code(row[4]);label=normalize(row[1]).replace('-',' ');label=' '.join(label.split())
        branch_id=branches.get(aliases.get(label,label)) if label!='tekne' else None
        biometric=cx.execute('SELECT person_id FROM biometric_persons WHERE person_id=?',(ci,)).fetchone()
        users=cx.execute("SELECT id FROM users WHERE document_id=? OR employee_code=?",(ci,ci)).fetchall()
        pid=cx.execute('''INSERT INTO hr_roster_proposals(import_id,identity_code,original_name,branch_label,department,
            position,branch_id,biometric_person_id,proposed_user_id) VALUES(?,?,?,?,?,?,?,?,?)''',
            (source_id,ci,str(row[5]),str(row[1]),str(row[2] or ''),str(row[3] or ''),branch_id,
             biometric['person_id'] if biometric else None,users[0]['id'] if len(users)==1 else None)).lastrowid
        stats['with_biometric_identity' if biometric else 'without_biometric_identity']+=1
        for n in range(1,32):
            text=str(row[5+n] or '');simple=str(ref[3+n] or '')
            category='BLANK' if row[5+n] is None else 'DASH' if text=='-' else 'SM' if text.startswith('S/M') else 'HOURS'
            lines=_lines(text);physical=[e for e in lines if e['provenance']=='A_ZLINK_PHYSICAL']
            manual=[e for e in lines if e['provenance']=='B_ZLINK_MANUAL']
            admin=[e for e in lines if e['provenance']=='E_HR_UNVERIFIED_ANNOTATION']
            late=re.search(r'Atraso: (\d+) min',text)
            stats[category]+=1;stats['physical_lines']+=len(physical);stats['manual_lines']+=len(manual)
            stats['administrative_lines']+=len(admin);stats['permission_days']+=bool(admin)
            stats['late_incidents']+=bool(late);stats['late_minutes']+=int(late[1]) if late else 0
            cx.execute('''INSERT INTO hr_reference_days(proposal_id,work_date,category,detailed_text,summary_text,
                physical_json,manual_json,administrative_json,late_minutes) VALUES(?,?,?,?,?,?,?,?,?)''',
                (pid,date(2026,10,n).isoformat(),category,text,simple,json.dumps(physical,ensure_ascii=False),
                 json.dumps(manual,ensure_ascii=False),json.dumps(admin,ensure_ascii=False),int(late[1]) if late else None))
    stats['cells']=len(rows)*31
    cx.execute('UPDATE hr_reference_imports SET stats_json=? WHERE id=?',(json.dumps(stats,ensure_ascii=False),source_id))
    audit(cx,actor_id,'REFERENCIA_RRHH','referencia',source_id,'Padrón y anotaciones históricas pendientes; no crea usuarios, permisos ni marcaciones')
    return source_id


def confirm_proposal(cx,proposal_id,actor_id,user_id,note,valid_from=None,valid_to=None):
    actor=cx.execute('SELECT role FROM users WHERE id=?',(actor_id,)).fetchone()
    if not actor or actor['role']!='hr':raise ValueError('Solo RR. HH. puede confirmar correspondencias.')
    if len(note.strip())<8:raise ValueError('Registra el respaldo revisado.')
    p=cx.execute('SELECT * FROM hr_roster_proposals WHERE id=?',(proposal_id,)).fetchone()
    if not p:raise ValueError('Propuesta inexistente.')
    if user_id and not cx.execute("SELECT id FROM users WHERE id=? AND role<>'admin'",(user_id,)).fetchone():raise ValueError('Persona no disponible.')
    if valid_to and not valid_from:raise ValueError('Indica inicio del vínculo.')
    if valid_from:
        first=date.fromisoformat(valid_from);last=date.fromisoformat(valid_to) if valid_to else None
        if last and last<first:raise ValueError('Vigencia inválida.')
        if not user_id or not p['biometric_person_id']:raise ValueError('Para un vínculo biométrico se necesitan usuario e identidad existentes.')
        overlap=cx.execute('''SELECT 1 FROM biometric_identity_links WHERE person_id=? AND valid_from<=COALESCE(?,'9999-12-31')
            AND COALESCE(valid_to,'9999-12-31')>=?''',(p['biometric_person_id'],valid_to,valid_from)).fetchone()
        if overlap:raise ValueError('Existe un vínculo superpuesto; revisa su historial.')
        cx.execute('''INSERT INTO biometric_identity_links(person_id,user_id,valid_from,valid_to,confirmed_by,confirmed_at,note)
            VALUES(?,?,?,?,?,?,?)''',(p['biometric_person_id'],user_id,valid_from,valid_to,actor_id,nowstr(),note))
        cx.execute("UPDATE biometric_persons SET linked_user_id=?,link_status='CONFIRMED',reviewed_by=?,reviewed_at=?,updated_at=? WHERE person_id=?",
            (user_id,actor_id,nowstr(),nowstr(),p['biometric_person_id']))
    cx.execute("UPDATE hr_roster_proposals SET state='CONFIRMED',user_id=?,confirmed_by=?,confirmed_at=?,note=? WHERE id=?",
        (user_id,actor_id,nowstr(),note,proposal_id))
    audit(cx,actor_id,'CONFIRMACION_PADRON_RRHH','propuesta',proposal_id,'Datos históricos verificados; sin inferir empleo ni horarios. '+note)


def compare_reference(cx,import_id):
    if cx.execute("SELECT 1 FROM sqlite_master WHERE name='executive_demo_meta'").fetchone():
        raise ValueError('La comparación real no admite bases con autorizaciones ficticias de la DEMO.')
    from reconciliation import _evaluate_day
    raw=defaultdict(list)
    for e in cx.execute("SELECT * FROM biometric_events WHERE occurred_at>='2026-10-01' AND occurred_at<'2026-11-01'"):
        raw[(e['person_id'],e['occurred_at'][:10])].append(dict(e))
    counts=Counter();categories=Counter();rows=[]
    for r in cx.execute('''SELECT d.*,p.identity_code,p.original_name,p.biometric_person_id,p.user_id,p.proposed_user_id,p.branch_id
        FROM hr_reference_days d JOIN hr_roster_proposals p ON p.id=d.proposal_id WHERE p.import_id=? ORDER BY p.original_name,d.work_date''',(import_id,)):
        counts['compared']+=1;original=raw.get((r['identity_code'],r['work_date']),[])
        physical=json.loads(r['physical_json']);manual=json.loads(r['manual_json']);admin=json.loads(r['administrative_json'])
        physical_source=Counter((e['occurred_at'][11:16],e['device_name'],e['verification_mode']) for e in original if 'manual' not in e['event_type'].lower() and 'administrador' not in e['source'].lower())
        expected_physical=Counter((e['time'],re.search(r'Lugar: (.*?)(?: \||$)',e['text'])[1],re.search(r'Verificaci.n: (.*?)(?: \||$)',e['text'])[1]) for e in physical)
        manual_source=Counter(e['occurred_at'][11:16] for e in original if 'manual' in e['event_type'].lower() or 'administrador' in e['source'].lower())
        evidence_match=physical_source==expected_physical and manual_source==Counter(e['time'] for e in manual)
        summary_hours=re.findall(r'\b\d{2}:\d{2}\b',r['summary_text'])
        all_reference_hours=[e['time'] for e in physical+manual+admin]
        expected_hours=([min(all_reference_hours)] if len(all_reference_hours)==1 else
                        [min(all_reference_hours),max(all_reference_hours)]) if all_reference_hours else []
        summary_consistent=summary_hours==expected_hours
        counts['summary_extrema_matches']+=summary_consistent
        if evidence_match:counts['source_cells_match']+=1
        counts['source_events_matched']+=len(original) if evidence_match else 0
        reasons=[];result=None;detail={};match=False
        user=cx.execute('SELECT * FROM users WHERE id=?',(r['user_id'],)).fetchone() if r['user_id'] else None
        if user:
            result=_evaluate_day(cx,user,date.fromisoformat(r['work_date']));detail=json.loads(result['detail_json'])
        else:reasons.append('DIFERENCIA_DE_PADRON')
        if not detail.get('employment_confirmed'):reasons.append('FECHA_DE_INGRESO_NO_CONFIRMADA')
        if not detail.get('work_intervals'):reasons.append('HORARIO_NO_CONFIRMADO')
        if not r['branch_id']:reasons.append('CLASIFICACION_ORGANIZACIONAL_PENDIENTE')
        if admin and not detail.get('approved_requests'):reasons.append('AUTORIZACION_HISTORICA_NO_DISPONIBLE')
        if manual:reasons.append('MARCACION_MANUAL_ZLINK')
        if r['category']=='DASH' and (not result or result['operational_result']!='NO LABORABLE'):reasons.append('INTERPRETACION_DE_DESCANSO_PENDIENTE')
        if result and result['operational_result']=='COBERTURA PARCIAL':reasons.append('COBERTURA_PARCIAL')
        if not evidence_match:reasons.append('DIFERENCIA_DE_FUENTES_VERIFICABLE')
        if not summary_consistent:reasons.append('DIFERENCIA_VERIFICABLE_ENTRE_REPORTES_RRHH')
        if result and result['operational_result']=='AUSENCIA JUSTIFICADA' and not detail.get('coverage_complete'):
            reasons.append('ERROR_REAL_DEL_MOTOR')
        if result and detail.get('employment_confirmed') and evidence_match:
            if r['category']=='BLANK':match=result['operational_result']=='FUERA DE RELACIÓN LABORAL'
            elif r['category']=='DASH':match=result['operational_result']=='NO LABORABLE'
            elif r['category']=='SM':match=result['operational_result']=='POSIBLE INASISTENCIA' and not original
            elif admin:
                ref_hours=sorted(e['time'] for e in admin)
                ours=sorted(e['time'][:5] for e in detail.get('administrative_references',[]))
                match=result['operational_result']=='AUSENCIA JUSTIFICADA' and ref_hours==ours and bool(ours)
                if detail.get('approved_requests') and not ours:reasons.append('POLITICA_DE_REPRESENTACION_NO_CONFIGURADA')
            else:match=bool(original) and result['operational_result']=='PRESENCIA REGISTRADA'
        if r['late_minutes'] is not None:counts['late_pending']+=1
        match=match and summary_consistent
        if match:counts['administrative_matches']+=1
        else:
            counts['differences']+=1
            if reasons:counts['explained']+=1
            counts['pending']+=1
        categories.update(set(reasons))
        rows.append({'proposal_id':r['proposal_id'],'ci':r['identity_code'],'name':r['original_name'],'date':r['work_date'],
            'hr_category':r['category'],'original_count':len(original),
            'hr_presented_hours':summary_hours,'summary_consistent':summary_consistent,
            'first_original':min((e['occurred_at'] for e in original),default=None),
            'last_original':max((e['occurred_at'] for e in original),default=None),
            'historical_administrative_lines':admin,'administrative_references':detail.get('administrative_references',[]),
            'approval_valid':bool(detail.get('approved_requests')),'permission_types':[q['leave_type'] for q in detail.get('approved_requests',[])],
            'confirmed_work_intervals':detail.get('work_intervals',[]),'result':result['operational_result'] if result else 'INFORMACIÓN INSUFICIENTE',
            'administrative_match':match,'source_match':evidence_match,'reasons':reasons,
            'late_policy_pending':r['late_minutes'] is not None})
    for key in ('administrative_matches','compared','differences','explained','pending','source_events_matched','source_cells_match'):
        counts.setdefault(key,0)
    return {'counts':dict(counts),'categories':dict(categories),'rows':rows}
