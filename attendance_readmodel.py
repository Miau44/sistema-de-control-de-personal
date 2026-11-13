"""Shared administrative query with frozen official snapshots taking precedence."""
import json


def daily_result(cx,employee,day,as_of=None):
    from core import month_end_for_day,legacy_daily_status,STATUS_LABELS
    STATUS_LABELS.setdefault('INFORMACIÓN INSUFICIENTE','INFORMACIÓN INSUFICIENTE')
    operational=cx.execute("SELECT 1 FROM local_staff_profiles WHERE user_id=? AND account_kind='OPERATIVA'",(employee['id'],)).fetchone()
    if operational:
        STATUS_LABELS.setdefault('NO APLICA','NO APLICA')
        return {'fecha':day.isoformat(),'user_id':employee['id'],'codigo':employee['employee_code'],'empleado':employee['name'],
            'entrada':'','salida':'','estado':'NO APLICA','estado_texto':'Cuenta operativa sin control biométrico',
            'originales':0,'correcciones':0,'permiso_aprobado':False,'observacion':'Cuenta auxiliar excluida de asistencia y reportes de personal.',
            'administrative_source':'CUENTA OPERATIVA','provisional':False}
    closure=cx.execute("SELECT snapshot_json FROM monthly_closures WHERE period_end=? AND state='CERRADO'",(month_end_for_day(day).isoformat(),)).fetchone()
    if closure:
        data=json.loads(closure['snapshot_json'])
        for row in data.get('detalle',[]):
            if row.get('fecha')==day.isoformat() and (row.get('user_id')==employee['id'] or row.get('codigo')==employee['employee_code']):
                STATUS_LABELS.setdefault(row['estado'],row.get('estado_texto',row['estado']))
                return {**row,'administrative_source':'CIERRE OFICIAL','provisional':False}
        return {'fecha':day.isoformat(),'codigo':employee['employee_code'],'empleado':employee['name'],
            'entrada':'','salida':'','estado':'INFORMACIÓN INSUFICIENTE','estado_texto':'INFORMACIÓN INSUFICIENTE',
            'originales':0,'correcciones':0,'permiso_aprobado':False,'observacion':'El snapshot oficial no contiene esta jornada; no se reconstruye como oficial.',
            'administrative_source':'CIERRE OFICIAL SIN DETALLE','provisional':True}
    central=cx.execute('SELECT 1 FROM biometric_events LIMIT 1').fetchone() or cx.execute('SELECT 1 FROM attendance_employment_periods WHERE user_id=?',(employee['id'],)).fetchone()
    if not central:
        row=legacy_daily_status(cx,employee,day,as_of)
        row.update(administrative_source='LEGADO',provisional=True)
        row['observacion']='Fuente legado; no certifica identidad/calendario Zlink. '+row['observacion']
        return row
    from attendance_continuous import current_evaluation
    from attendance_engine import evaluate
    saved=current_evaluation(cx,employee['id'],day)
    r=saved if saved and not saved['stale'] else evaluate(cx,employee,day)
    evidence=json.loads(r['detail_json']);state=r['operational_result']
    STATUS_LABELS.setdefault(state,state)
    return {'fecha':day.isoformat(),'user_id':employee['id'],'codigo':employee['employee_code'],'empleado':employee['name'],
        'area':evidence['area_name'],'entrada':'','salida':'','estado':state,'estado_texto':state,
        'originales':evidence['original_punch_count'],'correcciones':sum(x['authorization']['valid'] for x in evidence['correction_requests']),
        'permiso_aprobado':bool(evidence['approved_requests']),'observacion':r['reason'],
        'administrative_source':'CONCILIACION','provisional':True}
