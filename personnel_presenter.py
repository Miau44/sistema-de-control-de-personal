"""Read-only labels and directory counts; never confirms organizational assignments."""
import json


def describe(rows,profiles,confirmed_links):
    result=[]
    for original in rows:
        row=dict(original);profile=profiles.get(row['id'],{})
        auxiliary=profile.get('account_kind')=='OPERATIVA' or row['role']=='admin'
        provisional=bool(profile.get('provisional')) and not auxiliary
        if auxiliary:state='No aplica'
        elif provisional:state='Asignación provisional'
        elif row.get('classification_status')!='CLASIFICADA':state='Pendiente de validación'
        elif profile.get('person_id') and row['id'] not in confirmed_links:state='Pendiente de validación'
        else:
            source=json.loads(profile.get('source_json') or '{}')
            state='Pendiente de validación' if source.get('administrative_confirmation') is False else 'Confirmado'
        role={'employee':'Trabajador','manager':'Encargado','hr':'Recursos Humanos','admin':'Administrador técnico'}[row['role']]
        role={'ADMINISTRACION_OPERATIVA':'Administración operativa','OPERADOR_RRHH':'Operador RR. HH.',
              'RESPONSABLE_RRHH':'Responsable RR. HH.'}.get(profile.get('operational_authority'),role)
        result.append({**row,'display_state':state,'display_role':role,'auxiliary':auxiliary,
                       'pending_validation':not auxiliary and state!='Confirmado','provisional':provisional})
    return result,{'accounts':len(result),'labor':sum(not r['auxiliary'] for r in result),
        'auxiliary':sum(r['auxiliary'] for r in result),'pending':sum(r['pending_validation'] for r in result),
        'provisional_managers':sum(r['provisional'] and r['role']=='manager' for r in result)}
