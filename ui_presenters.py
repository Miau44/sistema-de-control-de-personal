"""Presentation only: group existing results without deciding attendance or authorization."""
from collections import defaultdict


def attendance_overview(rows):
    groups=defaultdict(list);attention=[]
    for row in rows:
        if row['operational_result'] in ('INFORMACIÓN INSUFICIENTE','HORARIO NO CONFIGURADO'):
            groups[row['reason']].append(row)
        elif row['justification_status']=='SOLICITUD PENDIENTE' or row['operational_result'] not in (
            'PRESENCIA REGISTRADA','AUSENCIA JUSTIFICADA','NO LABORABLE','FUERA DE RELACIÓN LABORAL'):
            attention.append(row)
    attention.sort(key=lambda r:(r['justification_status']!='SOLICITUD PENDIENTE',r['work_date'],r['user_id']))
    return {'attention':attention,'groups':[{'reason':reason,'count':len(items),
        'people':len({r['user_id'] for r in items}),'sample':items[:20]} for reason,items in groups.items()],
        'pending_information':sum(len(items) for items in groups.values()),
        'people_evaluated':len({r['user_id'] for r in rows}),
        'activity_days':sum(bool(r.get('punch_count')) for r in rows),
        'pending_requests':sum(r['justification_status']=='SOLICITUD PENDIENTE' for r in rows)}
