"""New physical layout; dynamic fields, fixed geometry and explicit overflow."""
import pytest
import pymupdf
from documents import pdf_boletas
from boleta_layout import PAGE_SIZE


def item():
    return {'request':{'id':1,'folio':'BP-TEST-000001','kind':'PERMISO','status':'APROBADA','leave_type':'MEDICO',
        'starts_at':'2026-11-12 09:00:00','ends_at':'2026-11-12 12:00:00','justification':'Cita médica autorizada','place':'Centro médico'},
        'employee':{'name':'Persona de prueba','branch_name':'Sede Central','area_name':'Sistemas','position':'Responsable','employee_code':'TEST01'},
        'approvals':[{'assignee_role':role,'decision':'APROBADA','is_skipped':0,'approver_name':name,'decided_at':'2026-11-11 10:00:00','note':''}
            for role,name in [('manager','Encargado de prueba'),('hr','RR. HH. de prueba')]]}


def test_new_layout_dynamic_letter_two_halves():
    first=item();second=item();second['request']={**second['request'],'id':2,'folio':'BP-TEST-000002'}
    with pymupdf.open(stream=pdf_boletas([first,second]),filetype='pdf') as doc:
        assert len(doc)==1
        for page in doc:
            assert abs(page.rect.width-PAGE_SIZE[0])<.01 and abs(page.rect.height-PAGE_SIZE[1])<.01
            text=page.get_text()
            for value in ['BOLETA DE PERMISO','Persona de prueba','RECEPCIÓN','CONTROL PERSONAL','TEST01']:assert value in text
            assert 'DEMO' not in text
            for block in page.get_text('blocks'):
                assert page.rect.contains(pymupdf.Rect(block[:4]))
        assert 'BP-TEST-000001' in doc[0].get_text() and 'BP-TEST-000002' in doc[0].get_text()


def test_long_content_requires_review_instead_of_silent_truncation():
    data=item();data['request']['justification']='Información detallada '*200
    with pytest.raises(ValueError,match='supera el espacio'):pdf_boletas([data])


@pytest.mark.parametrize('count,pages',[(1,1),(2,1),(3,2),(4,2),(5,3),(10,5),(20,10)])
def test_letter_slots_page_counts_and_empty_odd_lower_half(count,pages):
    slips=[]
    for i in range(count):
        data=item();data['request']={**data['request'],'id':i+1,'folio':f'BP-TEST-{i+1:06d}'};slips.append(data)
    with pymupdf.open(stream=pdf_boletas(slips),filetype='pdf') as doc:
        assert len(doc)==pages
        for index,page in enumerate(doc):
            assert tuple(page.rect)==(0,0,612,792)
            assert page.get_text().count('BOLETA DE PERMISO')==min(2,count-index*2)
        if count%2:
            assert all(b[3]<396 for b in doc[-1].get_text('blocks'))
            assert all(d['rect'].y1<396 for d in doc[-1].get_drawings())
        # Same first slip geometry, whether printed alone or with others.
        with pymupdf.open(stream=pdf_boletas(slips[:1]),filetype='pdf') as single:
            top=[(w[4],round(w[0],2),round(w[1],2)) for w in doc[0].get_text('words') if w[3]<396]
            reference=[(w[4],round(w[0],2),round(w[1],2)) for w in single[0].get_text('words')]
            assert top==reference
