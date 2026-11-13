"""Documentos descargables: boletas PDF y resumen Excel."""
from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from core import LEAVE_TYPES, STATUS_LABELS, nowstr

NAVY=colors.HexColor('#10253D')
TEAL=colors.HexColor('#087E88')
LIGHT=colors.HexColor('#E8F3F3')
GRAY=colors.HexColor('#667588')
BORDER=colors.HexColor('#CCD8E2')

try:
    pdfmetrics.registerFont(TTFont('Documento', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    pdfmetrics.registerFont(TTFont('DocumentoBold', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'))
    FONT='Documento';BOLD='DocumentoBold'
except Exception:
    FONT='Helvetica';BOLD='Helvetica-Bold'

    BOLETA_LOGO=Path(__file__).resolve().parent/'static'/'mercatech-bolivia.png'


def safe(value):
    return escape(str(value or '-')).replace('\n','<br/>')


def fmt(dt):
    try:
        return datetime.fromisoformat(dt).strftime('%d/%m/%Y %H:%M')
    except (ValueError,TypeError):
        return dt or '-'


def _excel_datetime(value):
    if isinstance(value,(date,datetime)):
        return value
    try:
        raw=str(value)
        if len(raw)==10:
            return date.fromisoformat(raw)
        return datetime.fromisoformat(raw)
    except (ValueError,TypeError):
        return value


def _wrap_canvas(c, text, x, y_top, width, *, font=FONT, size=7.2, leading=8.4, max_lines=2):
    text=' '.join(str(text or '').replace('\r','').split()) or '—'
    words=text.split(' '); lines=[]; current=''
    for word in words:
        candidate=(current+' '+word).strip()
        if pdfmetrics.stringWidth(candidate,font,size)<=width or not current:
            current=candidate
        else:
            lines.append(current); current=word
    if current:lines.append(current)
    if len(lines)>max_lines:
        raise ValueError('El contenido supera el espacio físico de la boleta. Corrige o resume las observaciones antes de emitir el documento.')
    c.setFont(font,size)
    for ix,line in enumerate(lines):c.drawString(x,y_top-(ix+1)*leading,line)
    return len(lines)*leading


def _field(c,x,y,w,h,label,value,*,size=7.2,max_lines=2):
    c.setLineWidth(.55);c.setStrokeColor(colors.black);c.rect(x,y,w,h,stroke=1,fill=0)
    c.setFont(BOLD,6.2);c.drawString(x+2,y+h-8,label.upper())
    _wrap_canvas(c,value,x+2,y+h-9,w-4,font=FONT,size=size,leading=size+1,max_lines=max_lines)


def _draw_boleta(c,item,slot=0):
    from boleta_layout import draw
    draw(c,item,slot)


def pdf_boletas(items):
    if not items:raise ValueError('Selecciona al menos una boleta autorizada.')
    request_ids=[item['request'].get('id') for item in items]
    folios=[item['request'].get('folio') for item in items]
    if len(request_ids)!=len(set(request_ids)) or len(folios)!=len(set(folios)):
        raise ValueError('La selección contiene una misma solicitud más de una vez.')
    from boleta_layout import PAGE_SIZE
    buff=io.BytesIO();c=canvas.Canvas(buff,pagesize=PAGE_SIZE,pageCompression=0)
    c.setTitle('Boletas de Permiso KIMTEK');c.setAuthor('Sistema interno KIMTEK')
    for ix,item in enumerate(items):
        _draw_boleta(c,item,ix%2)
        if ix%2==1 or ix==len(items)-1:c.showPage()
    c.save();return buff.getvalue()


def pdf_boleta(request_row,employee,approver,company='KIMTEK',approvals=()):
    if request_row['status']!='APROBADA' or request_row['kind']!='PERMISO':
        raise ValueError('Solo se emiten boletas de permisos aprobados definitivamente.')
    return pdf_boletas([{'request':request_row,'employee':employee,'approver':approver,'approvals':approvals}])

def _value(v):
    if v is None:return ''
    if isinstance(v,str) and v.startswith(('=','+','-','@')):
        return "'"+v # Evita formula injection con datos importados / libres
    return v


def _sheet(wb,name,headers,rows,widths,accent='087E88', top_rows=1):
    ws=wb.create_sheet(name)
    ws.sheet_view.showGridLines=False
    for ix,h in enumerate(headers,1):
        c=ws.cell(1,ix,h);c.fill=PatternFill('solid',fgColor=accent)
        c.font=Font(name='Aptos',size=10,bold=True,color='FFFFFF')
        c.alignment=Alignment(vertical='center',wrap_text=True)
    ws.row_dimensions[1].height=31
    for ix,w in enumerate(widths,1):ws.column_dimensions[get_column_letter(ix)].width=w
    for rownum,data in enumerate(rows,2):
        for colnum, v in enumerate(data,1):
            cell=ws.cell(rownum,colnum,_value(v));cell.alignment=Alignment(vertical='center',wrap_text=(colnum==len(headers)))
            if isinstance(v,datetime):
                cell.number_format='dd/mm/yyyy hh:mm'
            elif isinstance(v,date):
                cell.number_format='dd/mm/yyyy'
            cell.font=Font(name='Aptos',size=10,color='167A46' if 'Original' in name else '23364B')
            if rownum%2==0:cell.fill=PatternFill('solid',fgColor='F2F6F9')
        ws.row_dimensions[rownum].height=21
    ws.freeze_panes='C2' if len(headers)>4 else 'A2'
    ws.auto_filter.ref=f'A1:{get_column_letter(len(headers))}{max(2,ws.max_row)}'
    ws.sheet_properties.pageSetUpPr.fitToPage=True
    ws.page_setup.fitToWidth=1
    return ws


def excel_report(cx, payload, closed):
    wb=Workbook();cover=wb.active;cover.title='Panel'
    cover.sheet_view.showGridLines=False
    cover.merge_cells('A1:F2');cover['A1']='CONTROL DE ASISTENCIA | CIERRE MENSUAL'
    cover['A1'].font=Font(name='Aptos Display',bold=True,size=21,color='FFFFFF')
    cover['A1'].fill=PatternFill('solid',fgColor='10253D')
    cover['A1'].alignment=Alignment(vertical='center',indent=1)
    cover.row_dimensions[1].height=27;cover.row_dimensions[2].height=26
    cover['A4']='Desde';cover['B4']=_excel_datetime(payload['desde'])
    cover['C4']='Hasta';cover['D4']=_excel_datetime(payload['hasta'])
    cover['A5']='Estado';cover['B5']='CERRADO Y CONGELADO' if closed else 'BORRADOR / SIN CIERRE'
    cover['A6']='Generado';cover['B6']=_excel_datetime(payload['generado'])
    cover['A7']='Trabajadores';cover['B7']=payload['empleados']
    cover['A9']='Alcance';cover['B9']='Marcaciones originales conservadas; permisos y correcciones aprobadas se aplican como incidencias separadas.'
    cover['A10']='Nota';cover['B10']=(f"Cierre histórico: {payload['postclosure_zkbio_events']} eventos ZKBio importados después del cierre quedan fuera de esta copia."
        if payload.get('postclosure_zkbio_events') else
        'Reporte consolidado desde conciliación completa; no se calculan atrasos.'
        if payload.get('report_mode')=='CONCILIACION' else
        'Borrador: eventos ZKBio pendientes de consolidar; no utilizar para cierre oficial.'
        if payload.get('report_mode')=='PROVISIONAL' else
        'El reporte conserva el cálculo del cierre; las fuentes requieren revisión administrativa.')
    for col,w in [('A',23),('B',18),('C',20),('D',19),('E',17),('F',17)]:cover.column_dimensions[col].width=w
    for r in (4,5,6,7,9,10):
        cover[f'A{r}'].font=Font(name='Aptos',size=11,color='667588',bold=True)
        cover[f'B{r}'].font=Font(name='Aptos',size=11,color='10253D')
    cover['B9'].alignment=Alignment(wrap_text=True)
    cover['B10'].alignment=Alignment(wrap_text=True)
    cover['B4'].number_format='dd/mm/yyyy'
    cover['D4'].number_format='dd/mm/yyyy'
    cover['B6'].number_format='dd/mm/yyyy hh:mm'
    cover.row_dimensions[9].height=35;cover.row_dimensions[10].height=35

    summary_rows=[]
    if payload.get('report_mode')=='CONCILIACION' or payload.get('administrative_source')=='CONCILIACION':
        keys=['codigo','empleado','area','conciliacion_presencia','conciliacion_ausencia_justificada',
              'conciliacion_cobertura_parcial','conciliacion_posible_inasistencia',
              'conciliacion_falta_confirmada','conciliacion_no_laborable','conciliacion_sin_resolver']
        headers=['Codigo','Trabajador','Area','Presencia registrada','Ausencia justificada','Cobertura parcial',
                 'Posible inasistencia','Falta confirmada','No laborable','Sin resolver']
        widths=[15,26,24,20,22,19,23,20,17,17]
    else:
        keys=['codigo','empleado','area','puntual','atrasos','salidas_anticipadas','faltas','incompletas','justificados','parciales','en_curso']
        headers=['Codigo','Trabajador','Area','Puntuales','Atrasos','Salidas anticipadas','Faltas','Incompletas','Justificados','Parciales por revisar','Jornadas en curso']
        widths=[15,26,24,13,13,20,13,17,17,21,22]
    for r in payload['resumen']:
        summary_rows.append([r.get(k,0) for k in keys])
    _sheet(wb,'Resumen',headers,summary_rows,widths)
    details=[]
    for r in payload['detalle']:
        if payload.get('report_mode')=='CONCILIACION' or payload.get('administrative_source')=='CONCILIACION':
            details.append([_excel_datetime(r['fecha']),r['codigo'],r['empleado'],r['area'],
                r.get('reconciliation_result',''),r.get('reconciliation_justification',''),
                r.get('reconciliation_obligation',''),r.get('reconciliation_punch_count',0),
                r.get('reconciliation_sources',''),r.get('reconciliation_reason','')])
        else:
            details.append([_excel_datetime(r['fecha']),r['codigo'],r['empleado'],r['area'],r['entrada'],r['salida'],
                            r['estado_texto'],r['originales'],r['correcciones'],'Si' if r['permiso_aprobado'] else 'No',r['observacion']])
    if payload.get('report_mode')=='CONCILIACION' or payload.get('administrative_source')=='CONCILIACION':
        _sheet(wb,'Detalle diario',['Fecha','Codigo','Trabajador','Area','Resultado','Justificacion','Obligacion','Cantidad de marcas','Origen(es)','Motivo'],
              details,[15,16,27,24,31,28,28,18,36,55])
    else:
        _sheet(wb,'Detalle diario',['Fecha','Codigo','Trabajador','Area','Entrada','Salida','Estado','Marcaciones originales','Correcciones autorizadas','Permiso aprobado','Observacion'],
              details,[15,16,27,24,13,13,31,23,26,19,55])
    punches=cx.execute('''SELECT p.occurred_at,u.employee_code,u.name,p.kind,p.source,p.created_at
      FROM punches p JOIN users u ON u.id=p.user_id WHERE p.occurred_at>=? AND p.occurred_at<=?
      ORDER BY p.occurred_at,u.name''',(f'{payload["desde"]} 00:00:00', f'{payload["hasta"]} 23:59:59')).fetchall()
    original_punch_rows=[[_excel_datetime(v['occurred_at']),v['employee_code'],v['name'],v['kind'],v['source'],_excel_datetime(v['created_at'])]
                         for v in punches]
    if payload.get('report_mode')=='CONCILIACION' or payload.get('administrative_source')=='CONCILIACION':
        zkbio=cx.execute('''SELECT e.occurred_at,u.employee_code,u.name,e.event_type,e.source,e.imported_at
            FROM biometric_events e JOIN biometric_identity_links l ON l.person_id=e.person_id
              AND l.valid_from<=substr(e.occurred_at,1,10)
              AND (l.valid_to IS NULL OR l.valid_to>=substr(e.occurred_at,1,10))
            JOIN users u ON u.id=l.user_id WHERE e.occurred_at>=? AND e.occurred_at<=?
            ORDER BY e.occurred_at,u.name,e.id''',
            (f'{payload["desde"]} 00:00:00',f'{payload["hasta"]} 23:59:59')).fetchall()
        original_punch_rows.extend([[_excel_datetime(v['occurred_at']),v['employee_code'],v['name'],
            v['event_type'],v['source']+' - ZKBio',_excel_datetime(v['imported_at'])] for v in zkbio])
    _sheet(wb,'Marcaciones originales',['Fecha y hora','Codigo','Trabajador','Tipo de registro','Fuente','Importado el'],
      original_punch_rows,[24,16,29,21,32,24],accent='167A46')
    req=cx.execute('''SELECT r.*,u.employee_code,u.name,a.name AS approver_name
      FROM requests r JOIN users u ON u.id=r.user_id JOIN users a ON a.id=r.approver_id
      WHERE (r.kind='PERMISO' AND r.starts_at<=? AND r.ends_at>=?) OR
            (r.kind='CORRECCION' AND r.correction_at BETWEEN ? AND ?)
      ORDER BY r.submitted_at''',(f'{payload["hasta"]} 23:59:59',f'{payload["desde"]} 00:00:00',
             f'{payload["desde"]} 00:00:00',f'{payload["hasta"]} 23:59:59')).fetchall()
    req_rows=[]
    for v in req:
        req_rows.append([v['folio'],v['employee_code'],v['name'],v['kind'],LEAVE_TYPES.get(v['leave_type'],v['leave_type']),
         _excel_datetime(v['starts_at'] if v['kind']=='PERMISO' else v['correction_at']),
         _excel_datetime(v['ends_at']) if v['ends_at'] else '',v['status'],
         v['approver_name'],_excel_datetime(v['decided_at']) if v['decided_at'] else '',v['justification']])
    _sheet(wb,'Solicitudes',['Folio','Código','Solicitante','Clase','Motivo','Inicio o marcación','Fin','Estado','Responsable','Decisión','Justificación'],
       req_rows,[20,16,27,17,29,26,24,18,27,24,50],accent='6757A8')
    _sheet(wb,'Reglas y notas',['Concepto','Valor'],[
      ['Período','Del día 26 del mes previo al 25 del mes actual, inclusive'],
      ['Zona horaria','America/La_Paz (Bolivia)'],
      ['Puntualidad','Depende del horario individual y la tolerancia configurada'],
      ['Corrección','No edita las marcaciones originales; agrega un registro autorizado'],
      ['Justificación','Se aplica tras aprobación y dentro de su intervalo de vigencia'],
      ['Jornada incompleta','No se inventan ni infieren horas faltantes'],
      ['Cierre','El día 25 a las 23:59, con snapshot de resultados'],
      ['Reapertura','Solo RH/Administrador con motivo; queda en auditoría'],
      ['Fuente','ZKBio Zlink solo mediante exportación CSV/XLSX en esta versión; sin API verificada']
    ],[26,90],accent='10253D')
    out=io.BytesIO();wb.save(out);return out.getvalue()
