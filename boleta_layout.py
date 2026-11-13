"""Two fixed half-letter slips per portrait sheet; an odd lower slot stays blank."""
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.lib.utils import ImageReader

PAGE_SIZE=LETTER


def draw(c,item,slot=0):
    from core import LEAVE_TYPES
    from documents import FONT,BOLD,fmt
    r=item['request'];e=item['employee'];steps=item['approvals']
    if r['kind']!='PERMISO' or r['status']!='APROBADA' or not steps:
        raise ValueError('Solo se imprimen permisos con historial de aprobación.')
    if any(s['decision']!='APROBADA' and not s['is_skipped'] for s in steps):
        raise ValueError('Falta una aprobación obligatoria.')
    W,H=PAGE_SIZE
    if slot not in (0,1):raise ValueError('Posición de boleta inválida.')
    top=H-slot*H/2-4.8*mm;left=5.2*mm;right=W-5.2*mm;inner=right-left
    c.saveState();c.setStrokeColorRGB(.26,.26,.26);c.setFillColorRGB(.1,.1,.1);c.setLineWidth(.65)

    def box(xx,yy,ww,hh):c.rect(xx,yy-hh,ww,hh)
    def text(value,xx,yy,ww,hh,size=9,bold=False,align=0,italic=False,underline=False):
        content=escape(str(value or '—')).replace('\n','<br/>')
        if underline:content='<u>'+content+'</u>'
        font=('Helvetica-BoldOblique' if bold else 'Helvetica-Oblique') if italic else BOLD if bold else FONT
        p=Paragraph(content,ParagraphStyle('slip',
            fontName=font,fontSize=size,leading=size*1.13,alignment=align,
            textColor='#1b1b1b',splitLongWords=True))
        _,height=p.wrap(ww,hh)
        if height>hh+.1:raise ValueError('El contenido supera el espacio de la boleta. Requiere revisión antes de emitir; no se ocultó información.')
        p.drawOn(c,xx,yy-height)

    y=top
    logo=Path(__file__).resolve().parent/'static/mercatech-bolivia.png'
    if logo.is_file():c.drawImage(ImageReader(str(logo)),left,y-10.5*mm,10.5*mm,10.5*mm,mask='auto',preserveAspectRatio=True)
    text('MERCATECH\nKIMTEK · GESTIÓN\nDE PERSONAL',left+12.2*mm,y-1*mm,31.8*mm,11*mm,7.6,True)
    text('BOLETA DE PERMISO',left+45.8*mm,y-2*mm,inner-86.6*mm,10*mm,16.8,True,1)
    box(right-39*mm,y-2*mm,39*mm,7*mm);text('FOLIO: '+r['folio'],right-38*mm,y-3.5*mm,37*mm,5*mm,7.1,False,1)
    y-=13.2*mm
    labels=['Apellidos y Nombres','Dependencia / Sucursal','Cargo']
    values=[e.get('name'),(e.get('branch_name') or 'KIMTEK')+' / '+(e.get('area_name') or ''),e.get('position')]
    value_x=left+35*mm;value_w=inner-35*mm
    for i,(label,value) in enumerate(zip(labels,values)):
        height=(6.3 if i<2 else 8)*mm
        text(label,left,y-1.2*mm,34*mm,5.5*mm,8,italic=True);box(value_x,y,value_w,height)
        if i<2:text(value,value_x+2*mm,y-.9*mm,value_w-4*mm,4.5*mm,8.2)
        else:
            widths=[value_w*1.35/3.01,value_w*.74/3.01,value_w*.92/3.01];xx=value_x
            for j,(head,val) in enumerate(zip(['','Código de Empleado','Folio'],[value,e.get('employee_code'),r['folio']])):
                if j:c.line(xx,y,xx,y-8*mm)
                if head:text(head,xx+1.4*mm,y-.9*mm,widths[j]-2.8*mm,2.8*mm,6.4,italic=True)
                text(val,xx+1.4*mm,y-(3.8 if head else 1.4)*mm,widths[j]-2.8*mm,(3.4 if head else 6)*mm,7.5,True)
                xx+=widths[j]
        y-=height+(.8*mm if i<2 else 0)
    y-=1*mm;text('MOTIVO',left,y,inner,4*mm,9.6,True);y-=4*mm
    half=inner/2;box(left,y,inner,27.3*mm);c.line(left+half,y,left+half,y-27.3*mm)
    for col in range(2):
        xx=left+col*half+1.5*mm
        text('CÓDIGO',xx,y-1*mm,30.5*mm,4.7*mm,7.7,True,italic=True,underline=True)
        text('DESCRIPCIÓN',xx+36.8*mm,y-1*mm,half-40*mm,4.7*mm,7.7,True,italic=True,underline=True)
        for j,(code,description) in enumerate(list(LEAVE_TYPES.items())[col*4:col*4+4]):
            yy=y-(6+j*5.15)*mm
            text(code,xx,yy,30.5*mm,4.5*mm,7.2,True)
            box(xx+31.6*mm,yy,3.5*mm,3.5*mm)
            if r['leave_type']==code:text('X',xx+31.6*mm,yy,3.5*mm,3.5*mm,8.5,True,1)
            text(description,xx+36.8*mm,yy,half-40*mm,5*mm,7.3)
    y-=28.3*mm
    begin=datetime.fromisoformat(r['starts_at']);end=datetime.fromisoformat(r['ends_at']);hours=(end-begin).total_seconds()/3600
    duration=(f'{(end.date()-begin.date()).days+1} día(s) calendario solicitados' if r['leave_type']=='VACACIONES' else f'{hours:g} horas' if hours<24 else f'{(end.date()-begin.date()).days+1} día(s) calendario')
    detail=r.get('justification','')+(' - Destino: '+r['place'] if r.get('place') else '')
    widths=[inner*.45,inner*.18,inner*.18,inner*.19];xx=left
    for head,val,ww in zip(['DETALLE MOTIVO','INICIO','TÉRMINO','HORAS / DURACIÓN'],[detail,fmt(r['starts_at']),fmt(r['ends_at']),duration],widths):
        box(xx,y,ww,14*mm);text(head,xx+1.25*mm,y-1*mm,ww-2.5*mm,4*mm,7.6,False,0 if xx==left else 1,italic=True,underline=True)
        text(val,xx+1.25*mm,y-5*mm,ww-2.5*mm,8*mm,7.5,False,0 if xx==left else 1);xx+=ww
    y-=15*mm;box(left,y,inner,11.2*mm);text('OBSERVACIONES',left+1.8*mm,y-1*mm,inner-3.6*mm,3.5*mm,7.9,True,italic=True,underline=True)
    notes=['Encargado: '+s['note'] if s['assignee_role']=='manager' else 'RR. HH.: '+s['note'] for s in steps if s.get('note')]
    text(' | '.join(notes) or 'Aprobaciones registradas en el sistema interno.',left+1.8*mm,y-4.5*mm,inner-3.6*mm,6*mm,7.7)
    y-=12.2*mm;text('Aprobación electrónica interna registrada; no equivale a firma digital certificada.',left,y,inner,3*mm,6.7);y-=4*mm
    sig_h=y-(H-(slot+1)*H/2+4.8*mm)
    gap=.6*mm;weights=[1,1.17,1.17,1,1];unit=(inner-4*gap)/sum(weights);xx=left
    for label,role,weight in zip(['SOLICITANTE','ENCARGADO DE ÁREA','RECURSOS HUMANOS','RECEPCIÓN','CONTROL PERSONAL'],[None,'manager','hr',None,None],weights):
        ww=unit*weight;box(xx,y,ww,sig_h);text(label,xx+1.3*mm,y-1*mm,ww-2.6*mm,4*mm,7,True,1)
        step=next((s for s in steps if s['assignee_role']==role and s['decision']=='APROBADA'),None)
        if step:text('Aprobación registrada: '+(step.get('approver_name') or step.get('assignee_name_snapshot') or 'Responsable registrado'),xx+1.3*mm,y-5.5*mm,ww-2.6*mm,sig_h-11*mm,6.35,italic=True)
        elif role:text('Etapa no ejecutada: '+next((s.get('skip_reason','') for s in steps if s['assignee_role']==role),'Sin decisión registrada'),xx+1.3*mm,y-5.5*mm,ww-2.6*mm,sig_h-11*mm,6.35,italic=True)
        c.line(xx+1.3*mm,y-sig_h+4.5*mm,xx+ww-1.3*mm,y-sig_h+4.5*mm)
        text(fmt(step.get('decided_at')) if step else 'Firma / sello',xx+1.3*mm,y-sig_h+3.7*mm,ww-2.6*mm,3*mm,6.4,False,1);xx+=ww+gap
    c.restoreState()
