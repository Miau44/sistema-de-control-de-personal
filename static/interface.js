/* Progressive enhancement only; all decisions and validations remain on the server. */
const menuButton=document.querySelector('.mobile-nav-toggle'),navigation=document.getElementById('primary-navigation');
if(menuButton&&navigation){const mobile=matchMedia('(max-width:900px)'),session=document.querySelector('.session-tools'),identity=document.querySelector('.sidebar-foot');function setOpen(open){navigation.hidden=!open;for(const el of [session,identity])if(el)el.hidden=!open;menuButton.setAttribute('aria-expanded',String(open));menuButton.textContent=open?'Cerrar menú':'Menú'}const reset=()=>setOpen(!mobile.matches);mobile.addEventListener('change',reset);reset();menuButton.addEventListener('click',()=>setOpen(navigation.hidden));document.addEventListener('keydown',e=>{if(e.key==='Escape'&&mobile.matches){setOpen(false);menuButton.focus()}})}

document.querySelectorAll('[data-query-page]').forEach(a=>{const u=new URL(location.href);u.searchParams.set('pagina',a.dataset.queryPage);a.href=u.pathname+u.search});
document.querySelectorAll('a[href^="/conciliacion/detalle?"]').forEach(a=>{a.addEventListener('click',()=>sessionStorage.setItem('attendance-return',location.pathname+location.search))});
if(location.pathname==='/conciliacion/detalle'){
 const prior=sessionStorage.getItem('attendance-return');
 if(prior&&prior.startsWith('/conciliacion?'))document.querySelectorAll('a[href="/conciliacion"]').forEach(a=>a.href=prior);
}
document.querySelectorAll('[data-date-filter]').forEach(form=>{
 const parse=s=>new Date(s+'T12:00:00Z'),iso=d=>d.toISOString().slice(0,10),today=parse(form.dataset.today);
 form.querySelector('[data-date-preset]').addEventListener('change',e=>{
  let from=new Date(today),to=new Date(today),mode=e.target.value;
  if(mode==='custom')return;
  if(mode==='yesterday'){from.setUTCDate(from.getUTCDate()-1);to=new Date(from)}
  if(mode==='week')from.setUTCDate(from.getUTCDate()-((from.getUTCDay()+6)%7));
  if(mode==='month'){from.setUTCDate(1);to=new Date(Date.UTC(today.getUTCFullYear(),today.getUTCMonth()+1,0,12))}
  if(mode==='period'){to=parse(form.dataset.period);from=new Date(Date.UTC(to.getUTCFullYear(),to.getUTCMonth()-1,26,12))}
  form.elements.desde.value=iso(from);form.elements.hasta.value=iso(to);
  for(const field of [form.elements.desde,form.elements.hasta])field.dispatchEvent(new Event('change'));
 });
});
document.querySelectorAll('[data-quick-range]').forEach(button=>button.addEventListener('click',()=>{
 const form=document.querySelector('[data-date-filter]'),preset=form.querySelector('[data-date-preset]');
 preset.value=button.dataset.quickRange;preset.dispatchEvent(new Event('change'));form.requestSubmit();
}));
document.querySelectorAll('.file-picker input[type=file]').forEach(input=>input.addEventListener('change',()=>{
 input.closest('form').querySelector('.selected-file').textContent=input.files[0]?.name||'Ningún archivo seleccionado';
}));
document.querySelectorAll('[data-open-tools]').forEach(a=>a.addEventListener('click',()=>document.getElementById('herramientas').open=true));
document.querySelectorAll('.table-scroll').forEach(container=>{container.tabIndex=0;container.setAttribute('role','region');container.setAttribute('aria-label','Tabla desplazable horizontalmente')});
// Long HTML tables remain complete; filtering and paging only affect presentation.
document.querySelectorAll('main table').forEach(table=>{
 const body=table.tBodies[0];if(!body||table.hasAttribute('data-server-paged'))return;
 const rows=[...body.rows];if(rows.length<=20||table.closest('#bulk-form'))return;
 const bar=document.createElement('div');bar.className='table-tools';
 const search=document.createElement('input');search.type='search';search.placeholder='Buscar en esta tabla';search.setAttribute('aria-label','Buscar en esta tabla');
 const prev=document.createElement('button'),next=document.createElement('button'),state=document.createElement('span');
 for(const b of [prev,next]){b.type='button';b.className='btn btn-outline'}prev.textContent='Anterior';next.textContent='Siguiente';state.setAttribute('aria-live','polite');
 const selectors=[];
 if(location.pathname==='/equipo'&&table.closest('section')?.querySelector('h2')?.textContent==='Directorio'){
  for(const [column,label] of [[3,'Sucursal'],[4,'Área']]){const select=document.createElement('select');select.setAttribute('aria-label',label);select.add(new Option('Todas: '+label,''));[...new Set(rows.map(r=>r.cells[column]?.textContent.trim()).filter(Boolean))].sort().forEach(value=>select.add(new Option(value,value)));selectors.push({select,column});bar.append(select);select.addEventListener('change',()=>{page=0;render()})}
 }
 bar.prepend(search);bar.append(prev,state,next);table.parentElement.before(bar);let page=0;
 function render(){const visible=rows.filter(r=>r.textContent.toLocaleLowerCase().includes(search.value.toLocaleLowerCase())&&selectors.every(({select,column})=>!select.value||r.cells[column]?.textContent.trim()===select.value));const pages=Math.max(1,Math.ceil(visible.length/20));page=Math.min(page,pages-1);rows.forEach(r=>r.hidden=true);visible.slice(page*20,page*20+20).forEach(r=>r.hidden=false);state.textContent=`${visible.length} resultados · ${page+1} / ${pages}`;prev.disabled=!page;next.disabled=page>=pages-1}
 search.addEventListener('input',()=>{page=0;render()});prev.addEventListener('click',()=>{page--;render()});next.addEventListener('click',()=>{page++;render()});render();
});
const leave=document.querySelector('[name=leave_type]');
if(leave){const help=document.createElement('p');help.className='inside-note';leave.closest('.form-grid').after(help);const update=()=>{help.textContent=leave.value==='VACACIONES'?'Vacaciones: indica las fechas y horas explícitas autorizadas. No se infiere una jornada completa ni se calcula saldo.':'';help.hidden=leave.value!=='VACACIONES'};leave.addEventListener('change',update);update()}
if(leave){document.querySelectorAll('[name=kind]').forEach(r=>r.addEventListener('change',()=>leave.dispatchEvent(new Event('change'))));
 const optional=document.createElement('details');optional.innerHTML='<summary>Información adicional (opcional)</summary>';
 for(const name of ['place','contact']){const field=document.querySelector(`[name=${name}]`);if(field)optional.append(field.closest('label'))}
 document.getElementById('leave-fields').append(optional);
}
