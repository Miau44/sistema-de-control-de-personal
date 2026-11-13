"""Explicit, reproducible preparation of this workstation. Never runs on app startup."""
from contextlib import closing
from collections import Counter,defaultdict
from datetime import date
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import secrets
import sqlite3
import tempfile

BASE=Path(__file__).resolve().parent
TARGET=BASE/'instance'/'kimtek.sqlite3'
SEED=20261031


def read_sources(biometric,detailed,summary):
    from reconciliation import parse_zlink_xlsx,normalize
    from openpyxl import load_workbook
    files=[Path(x).resolve(strict=True) for x in (biometric,detailed,summary)]
    content=[p.read_bytes() for p in files]
    parsed=parse_zlink_xlsx(content[0]);identities={}
    for r in parsed['rows']:
        if r.get('parse_error'):raise ValueError('El biométrico contiene errores de interpretación.')
        prior=identities.get(r['person_id'])
        if prior and normalize(prior['person_name'])!=normalize(r['person_name']):raise ValueError('ID ambiguo: '+r['person_id'])
        identities[r['person_id']]=r
    wb=load_workbook(files[1],read_only=True,data_only=True)
    try:
        rows=list(wb.active.values)
        if tuple(rows[4][:6])!=('Nro','Sucursal','Departamento','Cargo','CI','Empleado'):raise ValueError('Reporte detallado no reconocido.')
        references={}
        for r in rows[5:]:
            if r[0] is None:continue
            code=str(r[4]).strip().removesuffix('.0')
            if code in references:raise ValueError('ID repetido en padrón.')
            references[code]=r
    finally:wb.close()
    if len(identities)!=50 or len(parsed['rows'])!=3567 or len(references)!=51:raise ValueError('Los archivos no corresponden al padrón esperado de octubre.')
    for code,item in identities.items():
        if code not in references or normalize(references[code][5])!=normalize(item['person_name']):
            raise ValueError('La correspondencia exacta ID/nombre necesita revisión: '+code)
    return files,content,parsed,identities,references


def build_database(path,biometric,detailed,summary):
    """Build a new staging file only. No cleaning or replacement inside this function."""
    import core
    from reconciliation import preview_zlink,import_zlink,reconcile,normalize
    from hr_reference import import_reference
    files,content,parsed,identities,references=read_sources(biometric,detailed,summary)
    path=Path(path).resolve()
    if path.exists():raise ValueError('La base de construcción debe ser nueva.')
    old=core.DB_PATH;core.DB_PATH=path
    try:core.initialize()
    finally:core.DB_PATH=old
    hashes={p.name:hashlib.sha256(data).hexdigest() for p,data in zip(files,content)}
    aliases={'central kimtek':'sede central','mercatech murillo':'mercatech la paz','mercatech riosinho':'mercatech riosinio'}
    with closing(sqlite3.connect(path)) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
        stamp=core.nowstr();first=min(r['occurred_at'][:10] for r in parsed['rows'])
        branches={normalize(r['name']):dict(r) for r in c.execute('SELECT * FROM branches')}
        central=branches['sede central'];areas={}
        def area(branch,name):
            key=(branch['id'],name)
            if key not in areas:
                areas[key]=c.execute('INSERT INTO areas(name,branch_id,active) VALUES(?,?,1)',key[::-1]).lastrowid
            return areas[key]
        techarea=area(central,'Administración de cuentas')
        def user(code,name,aid,role,position='',document=''):
            # The generated secret is discarded. Access requires the local interactive password tool.
            return c.execute('''INSERT INTO users(employee_code,name,document_id,position,area_id,email,role,password_hash,active,created_at,start_time,end_time)
                VALUES(?,?,?,?,?,?,?,?,1,?,'','')''',(code,name,document,position,aid,code.lower()+'@kimtek.local.invalid',role,
                core.hash_password(secrets.token_urlsafe(32)),stamp)).lastrowid
        ops={}
        for code,name,role,authority in [('ADMIN_TEC','Administración técnica','admin','TECNICA'),
             ('ADM_OPER','Autoridad de Administración','hr','ADMINISTRACION_OPERATIVA'),
             ('RESP_RRHH','Responsable de Recursos Humanos','hr','RESPONSABLE_RRHH'),
             ('OP_RRHH','Operación de Recursos Humanos','hr','OPERADOR_RRHH')]:
            uid=user(code,name,techarea,role);ops[code]=uid
            c.execute('''INSERT INTO local_staff_profiles(user_id,account_kind,operational_authority,source_json)
                VALUES(?,'OPERATIVA',?,?)''',(uid,authority,json.dumps({'source':'Configuración funcional local autorizada','not_biometric_employee':True})))
            if role=='hr':
                c.execute('''INSERT INTO employee_placements(user_id,company_id,branch_id,area_id,valid_from,classification_status,changed_by,created_at)
                    VALUES(?,?,?,?,?,'PENDIENTE',?,?)''',(uid,central['company_id'],central['id'],techarea,first,ops['ADMIN_TEC'],stamp))
        c.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(ops['ADM_OPER'],ops['RESP_RRHH']))
        c.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(ops['RESP_RRHH'],ops['OP_RRHH']))
        actor=ops['OP_RRHH'];preview=preview_zlink(c,content[0]);imported=import_zlink(c,preview,content[0],actor,files[0].name)
        import_reference(c,content[1],content[2],files[1].name+' + '+files[2].name,actor)
        members=[];groups=defaultdict(list)
        for code,item in sorted(identities.items()):
            ref=references[code];label=normalize(ref[1]);unknown=label=='tekne'
            branch=central if unknown else branches.get(aliases.get(label,label))
            if not branch:raise ValueError('Sucursal no reconocida: '+str(ref[1]))
            department=str(ref[2] or item.get('department') or '').strip()
            if not department:department='Clasificación pendiente'
            if normalize(branch['name']).startswith('mercatech'):department='Equipo Mercatech'
            aid=area(branch,department)
            uid=user(code,item['person_name'],aid,'employee',str(ref[3] or ''),code)
            evidence={'source_files':hashes,'original_person_name':item['person_name'],'original_department':item.get('department'),
                'hr_branch':str(ref[1]),'hr_area':str(ref[2] or ''),'hr_position':str(ref[3] or ''),
                'link_type':'EXACT_TECHNICAL_ID_AND_NAME','administrative_confirmation':False,
                'branch_confirmation':False,'branch_fallback':'Sede Central, clasificación local pendiente Tekne' if unknown else '',
                'placement_validity':'Ámbito técnico de análisis desde 01/10/2026; no es fecha de ingreso',
                'employment_dates_confirmed':False,'supervision':'PROVISIONAL','seed':SEED}
            c.execute('''INSERT INTO local_staff_profiles(user_id,person_id,account_kind,source_json)
                VALUES(?,?,'LABORAL',?)''',(uid,code,json.dumps(evidence,ensure_ascii=False)))
            c.execute('''INSERT INTO biometric_identity_links(person_id,user_id,valid_from,confirmed_by,confirmed_at,note)
                VALUES(?,?,?,NULL,?,?)''',(code,uid,first,stamp,'Vínculo técnico exacto autorizado para uso local. No constituye validación administrativa ni fecha de ingreso.'))
            c.execute("UPDATE biometric_persons SET linked_user_id=?,link_status='CONFIRMED',updated_at=? WHERE person_id=?",(uid,stamp,code))
            c.execute('''INSERT INTO employee_placements(user_id,company_id,branch_id,area_id,valid_from,classification_status,changed_by,created_at)
                VALUES(?,?,?,?,?,'PENDIENTE',?,?)''',(uid,branch['company_id'],branch['id'],aid,first,actor,stamp))
            regime='MOBILE_NO_FIXED_HOURS' if code=='8282765' else 'KIMSTAR_ROTATING' if normalize(branch['name'])=='kimstar' else 'BRANCH_STANDARD'
            c.execute('''INSERT INTO attendance_regime_assignments(user_id,regime_code,weekdays_json,valid_from,confirmed,note,assigned_by,created_at)
                VALUES(?,?,?,?,0,?,?,?)''',(uid,regime,'[0,1,2,3,4,5]' if regime=='BRANCH_STANDARD' else '[]',first,
                'Referencia local pendiente de validación; sin horas ni vigencia laboral confirmada.',actor,stamp))
            c.execute('UPDATE hr_roster_proposals SET proposed_user_id=? WHERE identity_code=?',(uid,code))
            entry={'id':uid,'code':code,'branch_id':branch['id'],'branch':branch['name'],'area_id':aid,'area':department}
            members.append(entry);groups[(branch['id'],aid)].append(entry)
        rng=random.Random(SEED);managers={};bybranch=defaultdict(list)
        for key,people in sorted(groups.items()):
            if len(people)>1:
                selected=rng.choice(sorted(people,key=lambda p:p['code']));managers[key]=selected;bybranch[key[0]].append(selected)
                c.execute("UPDATE users SET role='manager' WHERE id=?",(selected['id'],))
                c.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(actor,selected['id']))
        for key,people in sorted(groups.items()):
            manager=managers.get(key) or (sorted(bybranch[key[0]],key=lambda p:p['code'])[0] if bybranch[key[0]] else None)
            if manager:
                c.execute('''INSERT INTO area_responsibles(area_id,user_id,role,valid_from,assigned_by,created_at)
                    VALUES(?,?,'PRINCIPAL',?,?,?)''',(key[1],manager['id'],first,actor,stamp))
                for person in people:
                    if person['id']==manager['id']:continue
                    c.execute('UPDATE employee_placements SET supervisor_id=? WHERE user_id=? AND valid_to IS NULL',(manager['id'],person['id']))
                    c.execute('UPDATE local_staff_profiles SET superior_id=? WHERE user_id=?',(manager['id'],person['id']))
        run=reconcile(c,date(2026,10,1),date(2026,10,31),actor,reason='Preparación inicial local; sin autorizaciones ni horarios oficiales')
        c.execute('UPDATE attendance_source_changes SET processed_at=? WHERE processed_at IS NULL',(stamp,))
        c.execute('UPDATE attendance_recalc_queue SET processed_at=? WHERE processed_at IS NULL',(stamp,))
        allgroups=[g for r in run['rows'] for g in json.loads(r['detail_json'])['grouping']['groups']]
        result={'workers':len(members),'identities':len(identities),'events':imported['added'],'manual_events':run['stats']['manual_source_events'],
            'groups':len(allgroups),'repeated_groups':sum(g['original_count']>1 for g in allgroups),
            'extra_grouped_events':sum(g['original_count']-1 for g in allgroups),'branches':len(branches),'labor_areas':len(groups),
            'technical_areas':1,'provisional_managers':len(managers),'operational_accounts':len(ops),
            'branch_distribution':dict(Counter(p['branch'] for p in members)),
            'area_distribution':dict(Counter(p['branch']+' / '+p['area'] for p in members)),
            'manager_codes':sorted(m['code'] for m in managers.values()),'operational_codes':list(ops),
            'requests':c.execute('SELECT COUNT(*) FROM requests').fetchone()[0],
            'confirmed_employment_periods':c.execute('SELECT COUNT(*) FROM attendance_employment_periods WHERE confirmed=1').fetchone()[0],
            'daily_results':run['stats']['days_evaluated'],'operational_states':dict(Counter(r['operational_result'] for r in run['rows']))}
        assert (result['events'],result['groups'],result['repeated_groups'],result['extra_grouped_events'])==(3567,3049,483,518)
        assert not c.execute('SELECT 1 FROM employee_placements WHERE supervisor_id=user_id OR valid_to<valid_from').fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        from organization import register_profile
        for row in c.execute('SELECT user_id FROM local_staff_profiles').fetchall():
            register_profile(c,row['user_id'],'2026-10-01',None)
        c.execute('INSERT INTO local_preparation VALUES(1,?,?,?,?)',(json.dumps(hashes,sort_keys=True),SEED,stamp,json.dumps(result,ensure_ascii=False)))
        core.audit(c,actor,'PREPARACION_LOCAL','base',1,'50 correspondencias técnicas; responsabilidades provisionales; sin permisos ni cierres')
        c.commit();assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--biometric',required=True);p.add_argument('--detailed',required=True);p.add_argument('--summary',required=True)
    p.add_argument('--target',default=str(TARGET));p.add_argument('--dry-run',action='store_true');p.add_argument('--apply',action='store_true')
    p.add_argument('--confirm-target');p.add_argument('--rebuild',action='store_true');p.add_argument('--restore',type=Path)
    args=p.parse_args();target=Path(args.target).resolve()
    if target!=TARGET.resolve() or not target.is_relative_to(BASE.resolve()) or TARGET.is_symlink():p.error('El único destino autorizado es '+str(TARGET))
    files,content,*_=read_sources(args.biometric,args.detailed,args.summary)
    hashes={f.name:hashlib.sha256(data).hexdigest() for f,data in zip(files,content)}
    if args.dry_run or not args.apply:
        print(json.dumps({'dry_run':True,'target':str(target),'workers':50,'events':3567,'seed':SEED,'source_hashes':hashes},indent=2));return
    if args.confirm_target!=str(target):p.error('Debes confirmar la ruta absoluta exacta con --confirm-target.')
    if target.exists() and not args.rebuild and not args.restore:
        with closing(sqlite3.connect(target)) as c:
            r=c.execute('SELECT source_hashes_json,summary_json FROM local_preparation WHERE id=1').fetchone()
            if not r or json.loads(r[0])!=hashes:raise ValueError('Base existente distinta: no se sobrescribe; requiere --rebuild explícito.')
            print(json.dumps({'already_prepared':True,**json.loads(r[1])},ensure_ascii=True,indent=2));return
    from core import backup_before_migration
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',8000))==0:raise ValueError('Detén el servidor 8000 antes de reconstruir o restaurar la base.')
    target.parent.mkdir(parents=True,exist_ok=True)
    backup=backup_before_migration(target,'pre-local-rebuild') if target.exists() else None
    if args.restore:
        source=args.restore.resolve(strict=True)
        if not source.is_relative_to((BASE/'instance'/'backups').resolve()):raise ValueError('La restauración solo admite respaldos del proyecto.')
        with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro',uri=True)) as s:
            if s.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Respaldo inválido.')
            if not s.execute("SELECT 1 FROM sqlite_master WHERE name='local_preparation'").fetchone():
                raise ValueError('Respaldo anterior al entorno limpio: restaura el conjunto completo de código/base desde el archivo histórico, no como base normal actual.')
            with closing(sqlite3.connect(target)) as d:s.backup(d)
        print('Restaurado el respaldo confirmado; reinicia el servidor.');return
    fd,temp=tempfile.mkstemp(prefix='kimtek-build-',suffix='.sqlite3',dir=target.parent);os.close(fd);Path(temp).unlink()
    try:
        result=build_database(temp,args.biometric,args.detailed,args.summary)
        os.replace(temp,target)
        print(json.dumps({'target':str(target),'backup':str(backup) if backup else None,**result},ensure_ascii=True,indent=2))
    finally:
        if Path(temp).exists():Path(temp).unlink()


if __name__=='__main__':main()
