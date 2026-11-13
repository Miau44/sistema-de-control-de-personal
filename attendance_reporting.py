"""Adapt the central evaluation to the existing report contract without inferred lateness."""
import json
from datetime import timedelta


def report_from_reconciliation(cx,ending):
    from core import period_start,nowstr
    from reconciliation import reconciliation_employees,_evaluate_day,report_consolidation_status
    from attendance_continuous import capture_changes,refresh_expired
    from reconciliation import drain_recalculations
    capture_changes(cx);refresh_expired(cx);drain_recalculations(cx)
    start=period_start(ending);readiness=report_consolidation_status(cx,start,ending)
    details=[];summary=[]
    for person in reconciliation_employees(cx,start,ending):
        results={};counts={}
        for offset in range((ending-start).days+1):
            day=start+timedelta(days=offset)
            from attendance_continuous import current_evaluation
            saved=current_evaluation(cx,person['id'],day)
            evaluated=saved if saved and not saved['stale'] else _evaluate_day(cx,person,day)
            evidence=json.loads(evaluated['detail_json'])
            result=evaluated['operational_result'];counts[result]=counts.get(result,0)+1
            details.append({'fecha':day.isoformat(),'user_id':person['id'],'codigo':person['employee_code'],
                'empleado':person['name'],'area':evidence['area_name'],'entrada':'','salida':'',
                'estado':result,'estado_texto':result,'originales':evidence['original_punch_count'],
                'correcciones':sum(r['authorization']['valid'] for r in evidence['correction_requests']),
                'permiso_aprobado':bool(evidence['approved_requests']),'observacion':evaluated['reason'],
                'reconciliation_result':result,'reconciliation_justification':evaluated['justification_status'],
                'reconciliation_obligation':evaluated['work_obligation'],
                'reconciliation_punch_count':evaluated['punch_count'],'reconciliation_sources':evaluated['source_summary'],
                'reconciliation_reason':evaluated['reason'],'administrative_references':evidence['administrative_references']})
        area=cx.execute('SELECT name FROM areas WHERE id=?',(person['area_id'],)).fetchone()
        summary.append({'codigo':person['employee_code'],'empleado':person['name'],'area':area['name'] if area else '',
            'puntual':0,'atrasos':0,'salidas_anticipadas':0,'faltas':counts.get('FALTA CONFIRMADA POR RR. HH.',0),
            'incompletas':0,'justificados':counts.get('AUSENCIA JUSTIFICADA',0),'parciales':counts.get('COBERTURA PARCIAL',0),'en_curso':0,
            'conciliacion_presencia':counts.get('PRESENCIA REGISTRADA',0),
            'conciliacion_ausencia_justificada':counts.get('AUSENCIA JUSTIFICADA',0),
            'conciliacion_cobertura_parcial':counts.get('COBERTURA PARCIAL',0),
            'conciliacion_posible_inasistencia':counts.get('POSIBLE INASISTENCIA',0),
            'conciliacion_falta_confirmada':counts.get('FALTA CONFIRMADA POR RR. HH.',0),
            'conciliacion_no_laborable':counts.get('NO LABORABLE',0),
            'conciliacion_sin_resolver':sum(v for k,v in counts.items() if k in ('INFORMACIÓN INSUFICIENTE','INCIDENCIA TÉCNICA','HORARIO NO CONFIGURADO'))})
    return {'desde':start.isoformat(),'hasta':ending.isoformat(),'generado':nowstr(),
        'detalle':details,'resumen':summary,'empleados':len(summary),'consolidation':readiness,
        'reconciliation_run_id':readiness.get('run_id'),'administrative_source':'CONCILIACION',
        'reconciliation_version_ids':readiness.get('version_ids',[]),
        'report_mode':'CONCILIACION' if readiness['ready'] else 'PROVISIONAL'}
