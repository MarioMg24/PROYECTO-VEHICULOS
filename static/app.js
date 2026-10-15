'use strict';
/* Gold Engine Fleet pilot. Business records are server-owned, not browser-authoritative.
   Inspections may be queued offline and synced idempotently by client_uuid. */
const $=(sel,parent=document)=>parent.querySelector(sel);
const $$=(sel,parent=document)=>Array.from(parent.querySelectorAll(sel));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const milli=n=>(Number(n||0)/1000).toLocaleString('es-EC',{maximumFractionDigits:3});
const nowDate=()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`};
const uid=()=>crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`;
const statusPill=(s)=>{let cls=['PENDIENTE','PARCIAL'].includes(s)?'warn':['ENTREGADA','APROBADA'].includes(s)?'ok':s==='MAL'?'bad':'gray';return `<span class="badge ${cls}">${esc(s)}</span>`};
let state={vehicles:[],parts:[],inspections:[],maintenance:[],maintenance_plans:null,requests:[],request_lines:[],movements:[],compatibility:[],template:[],online:true};
let active='inicio';let checkSection=0;let checkDraft=null;let photos=[];let _alertTimeout;
let checkSaveTimer=null;let checkSubmitting=false;let checkPhotoBusy=false;let checkPhotoVersion=0;let checkStorageError=false;
let maintenanceHistory={vehicle_id:'',date_from:'',date_to:'',maintenance_type:'',page:1,page_size:20,data:null,loading:false,error:'',version:0};
let maintenanceDetailVersion=0;
const CHECK_OBSERVATIONS_LIMIT=2500;
const menuNames={inicio:['Panel general','Control de flota, inspecciones y bodega vehicular.'],vehiculos:['Vehículos','Unidades livianas y pesadas registradas.'],inspecciones:['Inspecciones','Checklist del formulario liviano vigente.'],mantenimientos:['Mantenimientos','Servicios y próximos cambios por kilometraje.'],repuestos:['Bodega vehicular','Existencias, filtros, bandas, baterías y lubricantes.'],solicitudes:['Solicitudes de repuestos','Pedidos y entregas asociados a cada vehículo.'],historial:['Historial y exportaciones','Inspecciones, movimientos y respaldos.']};
function alertMsg(text,kind='ok'){const box=$('#alert');box.className=`alert ${kind==='bad'?'bad':''}`;box.textContent=text;clearTimeout(_alertTimeout);_alertTimeout=setTimeout(()=>box.classList.add('hidden'),6500)}
function setOnline(value){state.online=value;const b=$('#network-status');b.textContent=value?'● Conectado':'● Sin conexión';b.className=`status ${value?'ok':'off'}`}
async function api(path,options={}){
 let resp;
 try{resp=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}})}catch(e){setOnline(false);throw new Error('Sin conexión con el servidor local.')} 
 if(!resp.ok){let reason='La operación no pudo completarse.';try{const data=await resp.json();reason=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{}throw new Error(reason)}
 setOnline(true);return resp.json();
}
async function post(path,payload){return api(path,{method:'POST',body:JSON.stringify(payload)})}
async function patch(path,payload){return api(path,{method:'PATCH',body:JSON.stringify(payload)})}
async function load(){
 try{const fresh=await api('/api/bootstrap');state={...state,...fresh,online:true};localStorage.setItem('ge_fleet_snapshot',JSON.stringify(fresh))}
 catch(e){const cached=localStorage.getItem('ge_fleet_snapshot');if(cached){try{state={...state,...JSON.parse(cached),online:false}}catch{}}else alertMsg('No se pudo conectar. Abre primero la aplicación con el servidor iniciado.','bad')}
 navigate(active,false);
}
function vehicleName(id){let v=state.vehicles.find(x=>x.id===Number(id));return v?`${v.name} · ${v.plate}`:'Sin vehículo'}
function partName(id){let p=state.parts.find(x=>x.id===Number(id));return p?`${p.code} · ${p.name}`:'Sin repuesto'}
function vehicleOptions(kind=null){return `<option value="">Selecciona el vehículo</option>`+state.vehicles.filter(v=>!kind||v.vehicle_type===kind).map(v=>`<option value="${v.id}">${esc(v.plate)} · ${esc(v.name)}</option>`).join('')}
function partOptions(){return `<option value="">Selecciona el repuesto</option>`+state.parts.map(p=>`<option value="${p.id}">${esc(p.code)} · ${esc(p.name)} (${milli(p.stock_milli)} ${esc(p.unit)})</option>`).join('')}
function formatTime(str){if(!str)return '—';const v=str.replace('T',' ');return esc(v.slice(0,16))}
function summaryCard(label,count,foot,extra=''){return `<div class="panel kpi ${extra}"><div class="cap">${label}</div><div class="num">${count}</div><small>${foot}</small></div>`}
function empty(title,sub){return `<div class="empty"><div class="illustration">▤</div><h3>${title}</h3><p>${sub}</p></div>`}
function pane(title,contents,controls=''){return `<div class="panel"><div class="panel-pad"><div class="heading"><h2>${title}</h2>${controls}</div>${contents}</div></div>`}
function table(head,body){return `<div class="table-scroller"><table class="table"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`}
function navigate(page,scroll=true){
 if(!menuNames[page])return;if(active==='mantenimientos'&&page!=='mantenimientos')maintenanceHistory.version++;active=page;$$('[data-nav]').forEach(x=>x.classList.toggle('active',x.dataset.nav===page));
 $('#crumb').textContent=page.toUpperCase();$('#page-title').textContent=menuNames[page][0];$('#page-sub').textContent=menuNames[page][1];
 $('#screen').innerHTML=({inicio:renderHome,vehiculos:renderVehicles,inspecciones:renderInspections,mantenimientos:renderMaintenance,repuestos:renderParts,solicitudes:renderRequests,historial:renderHistory}[page])();
 if(page==='mantenimientos'){bindMaintenanceHistory();fetchMaintenanceHistory()}
 if(scroll)window.scrollTo({top:0,behavior:'smooth'});
}
function latestDue(){
 // New snapshots contain complete server-selected plans, independent of the history limit.
 // Old cached snapshots can only provide a partial chronological fallback until reconnected.
 const complete=Array.isArray(state.maintenance_plans),source=complete?state.maintenance_plans:state.maintenance;
 const validDate=value=>{const match=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value||'');if(!match)return false;const year=Number(match[1]),month=Number(match[2]),day=Number(match[3]),days=[31,year%4===0&&(year%100!==0||year%400===0)?29:28,31,30,31,30,31,31,30,31,30,31];return year>=1&&month>=1&&month<=12&&day>=1&&day<=days[month-1]};
 const byKey=new Map();
  [...source].filter(m=>m.maintenance_type!=='CORRECTIVO'&&Number.isSafeInteger(m.odometer)&&m.odometer>=0&&m.odometer<=10000000&&Number.isSafeInteger(m.interval_km)&&m.interval_km>0&&m.interval_km<=1000000&&validDate(m.performed_on)&&String(m.service_key??m.service_name).trim().replace(/\s+/g,' ').toLocaleLowerCase('es-EC')!=='corrección de kilometraje').sort((a,b)=>b.performed_on.localeCompare(a.performed_on)||b.odometer-a.odometer||b.id-a.id).forEach(m=>{
  const serviceKey=m.service_key??String(m.service_name).trim().replace(/\s+/g,' ').toLocaleLowerCase('es-EC'),key=`${m.vehicle_id}-${serviceKey}`;
  if(!byKey.has(key))byKey.set(key,m);
 });
 return [...byKey.values()].map(m=>{const next_due_km=m.odometer+m.interval_km;return {...m,next_due_km,kmLeft:next_due_km-(state.vehicles.find(v=>v.id===m.vehicle_id)?.odometer||0)}}).sort((a,b)=>a.kmLeft-b.kmLeft||a.vehicle_id-b.vehicle_id||a.id-b.id);
}
function maintenancePlanNotice(){return Array.isArray(state.maintenance_plans)?'':'<div class="notice warn" style="margin-bottom:15px">Esta copia local es anterior al cálculo completo de planes. Conecta con el servidor para actualizarla; los servicios mostrados pueden estar incompletos.</div>'}
function renderHome(){
 const faulted=state.inspections.filter(i=>i.failure_count>0).length, low=state.parts.filter(p=>p.stock_milli<=p.min_milli).length;
 const due=latestDue();return `<div class="kpis">${summaryCard('Vehículos registrados',state.vehicles.length,'Livianos, pesados y especiales')}${summaryCard('Inspecciones',state.inspections.length,'Historial registrado')}${summaryCard('Repuestos bajo mínimo',low,'Incluye artículos sin stock','warn')}${summaryCard('Servicios vencidos',due.filter(x=>x.kmLeft<=0).length,'Según kilometraje','bad')}</div>
 ${maintenancePlanNotice()}<div class="layout2">${pane('Mantenimiento vehicular',due.length?table(['Vehículo','Servicio','Próximo km','Restan'],due.slice(0,6).map(m=>`<tr><td><strong>${esc(m.plate)}</strong><div class="sub">${esc(m.vehicle_name)}</div></td><td>${esc(m.service_name)}</td><td>${m.next_due_km.toLocaleString('es-EC')}</td><td>${m.kmLeft<=0?`<span class="badge bad">Vencido</span>`:`<span class="badge ${m.kmLeft<=500?'warn':'ok'}">${m.kmLeft.toLocaleString('es-EC')} km</span>`}</td></tr>`).join('')):empty('Sin planes registrados','Añade un mantenimiento real y su intervalo para calcular el próximo servicio.'),`<button class="btn secondary tiny" data-nav="mantenimientos">Ver servicios →</button>`)}
 ${pane('Operaciones rápidas',`<div class="stack"><button class="btn" data-action="new-inspection">☑ Nueva inspección liviana</button><button class="btn secondary" data-action="new-movement" data-mtype="ENTRADA">＋ Ingresar repuestos</button><button class="btn secondary" data-action="new-request">☷ Solicitar repuestos</button><div class="notice">${faulted} inspecciones registradas con al menos una falla. Las observaciones deben revisarse por mantenimiento.</div></div>`)}</div>
 <div class="panel panel-pad" style="margin-top:17px"><div class="heading"><h2>Inspecciones recientes</h2><button class="btn secondary tiny" data-nav="inspecciones">Abrir historial →</button></div>${state.inspections.length?table(['Vehículo','Fecha','Inspector','Km','Fallas','Detalle'],state.inspections.slice(0,5).map(i=>`<tr><td><strong>${esc(i.plate)}</strong></td><td>${esc(i.date)}</td><td>${esc(i.inspector)}</td><td>${i.odometer.toLocaleString('es-EC')}</td><td>${i.failure_count?`<span class="badge bad">${i.failure_count}</span>`:'<span class="badge ok">0</span>'}</td><td><button class="link" data-action="inspection-detail" data-id="${i.id}">Ver →</button></td></tr>`).join('')):empty('Sin inspecciones todavía','Usa Nueva inspección para registrar el primer checklist.')}</div>`;
}
function renderVehicles(){return `<div class="toolbar"><div class="notice">Las placas iniciales provienen del listado proporcionado; deben comprobarse con la matrícula de cada unidad.</div><button class="btn" data-action="new-vehicle">＋ Añadir vehículo</button></div>
 ${state.vehicles.length?`<div class="vehicle-grid">${state.vehicles.map(v=>`<article class="panel vehicle-card"><span class="vehicle-mark">${v.vehicle_type==='LIVIANO'?'🚙':v.vehicle_type==='PESADO'?'🚛':'⚙'}</span><div class="meta"><h3>${esc(v.name)}</h3><div class="plate">${esc(v.plate)} · ${esc(v.vehicle_type.replaceAll('_',' '))}</div><div class="km">Kilometraje conocido: <strong>${v.odometer.toLocaleString('es-EC')} km</strong></div><div class="controls btn-group"><button class="btn tiny secondary" data-action="vehicle-detail" data-id="${v.id}">Ver ficha</button><button class="btn tiny secondary" data-action="edit-vehicle" data-id="${v.id}">Editar</button><button class="btn tiny secondary" data-action="correct-odometer" data-id="${v.id}">Corregir kilometraje</button></div></div></article>`).join('')}</div>`:empty('Flota vacía','Añade vehículos o utiliza el script opcional para cargar el listado de las nueve unidades.')}`}
function renderInspections(){return `<div class="toolbar"><div class="notice">Plantilla 01: formulario liviano completado. Vehículos pesados se añadirán cuando esté disponible su formato.</div><button class="btn" data-action="new-inspection">☑ Nueva inspección</button></div>
 ${pane('Inspecciones registradas',state.inspections.length?table(['Fecha','Placa / Vehículo','Inspector','Km','Fallas','Acciones'],state.inspections.map(i=>`<tr><td>${esc(i.date)}</td><td><strong>${esc(i.plate)}</strong><div class="sub">${esc(i.vehicle_name)}</div></td><td>${esc(i.inspector)}</td><td>${i.odometer.toLocaleString('es-EC')}</td><td>${i.failure_count?`<span class="badge bad">${i.failure_count}</span>`:'<span class="badge ok">Sin fallas</span>'}</td><td><button class="link" data-action="inspection-detail" data-id="${i.id}">Ver / imprimir</button></td></tr>`).join('')):empty('Sin inspecciones','Empieza registrando una inspección del vehículo liviano.'))}
 <div class="panel panel-pad" style="margin-top:15px">${pendingPanel()}</div>`}
function pendingPanel(){const c=Number(localStorage.getItem('ge_pending_count')||0);return `<div class="heading"><h2>Cola de inspecciones offline</h2><span class="badge ${c?'warn':'ok'}">${c} pendientes</span></div><p class="small muted">Las inspecciones sin conexión se guardan localmente. Sincroniza al volver a conectarte con el servidor.</p><button class="btn secondary" data-action="sync">↻ Sincronizar pendientes</button>`}
function maintenanceTypeLabel(value){return value==='MPG'?'Preventivo (MPG)':value==='CORRECTIVO'?'Correctivo':'Sin clasificar'}
function serviceDateLabel(value){return /^\d{4}-\d{2}-\d{2}$/.test(value||'')?value.split('-').reverse().join('/'):String(value||'—')}
function maintenancePlanHTML(){
 const h=maintenanceHistory,source=!h.loading&&!h.error&&Array.isArray(h.data?.plans)?h.data.plans.map(m=>{const next_due_km=m.odometer+m.interval_km,current=Number.isSafeInteger(m.current_odometer)?m.current_odometer:(state.vehicles.find(v=>v.id===m.vehicle_id)?.odometer||0);return {...m,next_due_km,kmLeft:next_due_km-current}}).sort((a,b)=>a.kmLeft-b.kmLeft||a.vehicle_id-b.vehicle_id||a.id-b.id):latestDue(),due=source.filter(m=>!h.vehicle_id||m.vehicle_id===Number(h.vehicle_id));
 return due.length?due.map(m=>`<div class="list-item"><div style="min-width:0;overflow-wrap:anywhere"><strong>${esc(m.plate)} · ${esc(m.service_name)}</strong><div class="sub">Último: ${m.odometer.toLocaleString('es-EC')} km · Objetivo: ${m.next_due_km.toLocaleString('es-EC')} km</div></div><span class="badge ${m.kmLeft<=0?'bad':m.kmLeft<=500?'warn':'ok'}">${m.kmLeft<=0?'Vencido':`${m.kmLeft.toLocaleString('es-EC')} km`}</span></div>`).join(''):empty('Sin servicios programados','Registra un servicio y su intervalo de kilometraje.');
}
function maintenanceLatestHTML(){const m=maintenanceHistory.data?.latest;if(maintenanceHistory.loading)return '<p class="muted small">Consultando último mantenimiento…</p>';if(maintenanceHistory.error)return '<p class="muted small">Conecta con el servidor para consultar el último mantenimiento.</p>';return m?`<div class="list-item"><div style="min-width:0;overflow-wrap:anywhere"><strong>${esc(m.plate)} · ${esc(m.service_name)}</strong><div class="sub">${esc(serviceDateLabel(m.performed_on))} · ${m.odometer.toLocaleString('es-EC')} km · ${esc(maintenanceTypeLabel(m.maintenance_type))}</div></div><button class="link" data-action="maintenance-detail" data-id="${m.id}">Detalle</button></div><p class="inline-note">La fecha y el tipo filtrados no alteran el último servicio ni los planes vigentes.</p>`:empty('Sin mantenimiento registrado','El historial se completará a medida que se registren trabajos reales.')}
function maintenanceHistoryHTML(){
 const h=maintenanceHistory,d=h.data;
 if(h.loading)return '<div class="notice" role="status">Consultando historial completo…</div>';
 if(h.error)return `<div class="notice warn" role="status">No se pudo consultar el historial completo. ${esc(h.error)} Necesitas conexión con el servidor local.</div><button class="btn secondary" style="margin-top:12px" data-action="maintenance-history-retry">Reintentar consulta</button>`;
 if(!d)return '<div class="notice">Selecciona un vehículo o consulta todos los mantenimientos.</div>';
 const rows=d.items.map(m=>`<tr data-maintenance-id="${m.id}"><td>${esc(serviceDateLabel(m.performed_on))}</td><td><strong>${esc(m.plate)}</strong><div class="sub">${esc(m.vehicle_name)}</div></td><td>${esc(maintenanceTypeLabel(m.maintenance_type))}</td><td><strong>${esc(m.service_name)}</strong>${m.work_performed?`<div class="sub">${esc(m.work_performed.slice(0,120))}${m.work_performed.length>120?'…':''}</div>`:''}</td><td>${m.odometer.toLocaleString('es-EC')}</td><td><button class="link" data-action="maintenance-detail" data-id="${m.id}">Detalle</button></td></tr>`).join('');
 const from=d.total?(d.page-1)*d.page_size+1:0,to=Math.min(d.page*d.page_size,d.total);
 return `${d.items.length?table(['Fecha','Vehículo','Tipo','Servicio / trabajo','Km','Acciones'],rows):empty('Sin servicios para estos filtros','Prueba otro vehículo, rango de fechas o tipo de mantenimiento.')}<div class="toolbar" style="margin-top:15px;margin-bottom:0"><span class="small muted" id="maintenance-history-count">${from}–${to} de ${d.total} registros · Página ${d.page} de ${Math.max(1,d.pages)}</span><div class="btn-group"><button class="btn secondary" data-action="maintenance-history-prev" ${d.page<=1?'disabled':''}>← Anterior</button><button class="btn secondary" data-action="maintenance-history-next" ${d.page>=d.pages?'disabled':''}>Siguiente →</button></div></div>`;
}
function renderMaintenance(){const h=maintenanceHistory;return `<div class="toolbar"><div class="notice">La programación se calcula con kilómetros reales y un intervalo definido por servicio. Los trabajos correctivos conservan los planes preventivos.</div><button class="btn" data-action="new-maintenance">＋ Registrar servicio</button></div>
 ${maintenancePlanNotice()}<div class="layout2 equal">${pane('Próximos servicios',`<div id="maintenance-plan-list">${maintenancePlanHTML()}</div>`)}${pane('Último mantenimiento',`<div id="maintenance-latest">${maintenanceLatestHTML()}</div>`)}</div>
 <div class="panel panel-pad" style="margin-top:17px"><div class="heading"><h2>Historial técnico</h2></div><form id="maintenance-history-filters"><div class="fields">${s('Vehículo','history_vehicle_id','<option value="">Todos los vehículos</option>'+state.vehicles.map(v=>`<option value="${v.id}" ${String(v.id)===h.vehicle_id?'selected':''}>${esc(v.plate)} · ${esc(v.name)}</option>`).join(''))}${s('Tipo de mantenimiento','history_maintenance_type',[['','Todos los tipos'],['MPG','Preventivo (MPG)'],['CORRECTIVO','Correctivo'],['LEGACY','Sin clasificar (registros anteriores)']].map(([value,label])=>`<option value="${value}" ${value===h.maintenance_type?'selected':''}>${label}</option>`).join(''))}${f('Desde','history_date_from','date',`value="${esc(h.date_from)}"`)}${f('Hasta','history_date_to','date',`value="${esc(h.date_to)}"`)}</div><div class="btn-group" style="margin-top:13px;margin-bottom:17px"><button class="btn secondary" type="submit">Consultar historial</button><button class="btn secondary" type="button" data-action="maintenance-history-clear">Limpiar filtros</button></div></form><div id="maintenance-history-results">${maintenanceHistoryHTML()}</div></div>`}
function updateMaintenanceHistory(){if(active!=='mantenimientos')return;const results=$('#maintenance-history-results'),latest=$('#maintenance-latest'),plans=$('#maintenance-plan-list');if(results)results.innerHTML=maintenanceHistoryHTML();if(latest)latest.innerHTML=maintenanceLatestHTML();if(plans)plans.innerHTML=maintenancePlanHTML()}
function bindMaintenanceHistory(){const form=$('#maintenance-history-filters');if(!form)return;form.onsubmit=event=>{event.preventDefault();const d=new FormData(form),date_from=d.get('history_date_from'),date_to=d.get('history_date_to');if((date_from&&!validServiceDate(date_from))||(date_to&&!validServiceDate(date_to)))return alertMsg('Indica fechas válidas para consultar el historial.','bad');if(date_from&&date_to&&date_from>date_to)return alertMsg('La fecha desde debe ser anterior o igual a la fecha hasta.','bad');Object.assign(maintenanceHistory,{vehicle_id:d.get('history_vehicle_id'),maintenance_type:d.get('history_maintenance_type'),date_from,date_to,page:1,data:null});fetchMaintenanceHistory()}}
async function fetchMaintenanceHistory(){
 const h=maintenanceHistory,version=++h.version,query=new URLSearchParams({page:String(h.page),page_size:String(h.page_size)});for(const field of ['vehicle_id','date_from','date_to','maintenance_type'])if(h[field])query.set(field,h[field]);h.loading=true;h.error='';updateMaintenanceHistory();
 try{const data=await api('/api/maintenance?'+query);if(version!==h.version||active!=='mantenimientos')return;h.data=data;h.loading=false;updateMaintenanceHistory()}
 catch(error){if(version!==h.version||active!=='mantenimientos')return;h.error=error.message;h.loading=false;updateMaintenanceHistory()}
}
function openVehicleMaintenance(id){closeModal();Object.assign(maintenanceHistory,{vehicle_id:String(id),date_from:'',date_to:'',maintenance_type:'',page:1,data:null,error:''});navigate('mantenimientos')}
function renderParts(){const low=state.parts.filter(p=>p.stock_milli<=p.min_milli).length;return `<div class="kpis" style="grid-template-columns:repeat(3,1fr)">${summaryCard('Referencias',state.parts.length,'Catálogo vehicular')}${summaryCard('Bajo mínimo',low,'Revisar existencias','warn')}${summaryCard('Salidas registradas',state.movements.filter(m=>m.movement_type==='SALIDA').length,'Movimientos confirmados')}</div>
 <div class="toolbar"><input class="search" id="part-search" placeholder="Buscar código, repuesto o número de parte..." aria-label="Buscar repuesto"><div class="btn-group"><button class="btn secondary" data-action="new-movement" data-mtype="ENTRADA">＋ Entrada</button><button class="btn secondary" data-action="new-movement" data-mtype="SALIDA">－ Salida</button><button class="btn" data-action="new-part">＋ Nuevo repuesto</button></div></div>
 <div class="panel panel-pad"><div class="heading"><h2>Inventario de bodega vehicular</h2><span class="badge">No comparte stock con BodegaPro</span></div>${state.parts.length?table(['Código','Repuesto','Categoría','Parte','Disponible','Mínimo','Estado'],state.parts.map(p=>`<tr data-filter-part="${esc(`${p.code} ${p.name} ${p.part_number}`.toLowerCase())}"><td><strong>${esc(p.code)}</strong></td><td>${esc(p.name)}</td><td>${esc(p.category)}</td><td>${esc(p.part_number)||'—'}</td><td><strong>${milli(p.stock_milli)} ${esc(p.unit)}</strong></td><td>${milli(p.min_milli)}</td><td>${p.stock_milli<=p.min_milli?'<span class="badge warn">Bajo mínimo</span>':'<span class="badge ok">Normal</span>'}</td></tr>`).join('')):empty('Bodega sin repuestos registrados','La aplicación empieza con stock cero y sin existencias inventadas. Registra los artículos y luego sus entradas reales.')}</div>`}
function renderRequests(){return `<div class="toolbar"><div class="notice">Una solicitud no descuenta existencias. El stock disminuye solo cuando bodega confirma la entrega.</div><button class="btn" data-action="new-request">＋ Nueva solicitud</button></div>
 ${pane('Solicitudes de materiales',state.requests.length?table(['Código','Vehículo','Solicitante','Estado','Fecha','Acción'],state.requests.map(r=>`<tr><td><strong>SOL-${String(r.id).padStart(5,'0')}</strong></td><td>${esc(r.plate)}</td><td>${esc(r.requester)}</td><td>${statusPill(r.status)}</td><td>${formatTime(r.created_at)}</td><td><button class="link" data-action="request-detail" data-id="${r.id}">Ver / entregar</button></td></tr>`).join('')):empty('Sin solicitudes','Registra una solicitud para crear el primer pedido a la bodega vehicular.'))}`}
function renderHistory(){return `<div class="layout2 equal">${pane('Exportar datos',`<div class="stack"><a class="btn secondary" href="/api/export/vehicles">Descargar vehículos (CSV)</a><a class="btn secondary" href="/api/export/inspections">Descargar inspecciones (CSV)</a><a class="btn secondary" href="/api/export/maintenance">Descargar mantenimientos (CSV)</a><a class="btn secondary" href="/api/export/movements">Descargar movimientos (CSV)</a><a class="btn teal" href="/api/backup">Descargar respaldo SQLite</a></div><p class="inline-note" style="margin-top:12px">CSV separado por punto y coma, compatible con Excel. No sustituye un respaldo completo.</p>`)}
 ${pane('Historial reciente de bodega',state.movements.length?state.movements.slice(0,12).map(m=>`<div class="list-item"><div><strong>${esc(m.code)} · ${esc(m.part_name)}</strong><div class="sub">${formatTime(m.created_at)} · ${esc(m.reference||m.responsible)} ${m.plate?'· '+esc(m.plate):''}</div></div><span class="badge ${m.movement_type==='ENTRADA'?'ok':'bad'}">${m.movement_type==='ENTRADA'?'+':'−'}${milli(m.qty_milli)} ${esc(m.unit)}</span></div>`).join(''):empty('Sin movimientos','Los ingresos, salidas y entregas confirmadas aparecerán aquí.'))}</div>
 <div class="notice warn" style="margin-top:15px">Uso piloto: no publiques esta aplicación en Internet sin implementar cuentas, permisos, HTTPS, auditoría y copias externas de respaldo.</div>`}
function openModal(title,html,submit){$('#modal-title').textContent=title;$('#modal-content').innerHTML=`<form id="modal-form" class="modal-body">${html}<div class="modal-foot"><button class="btn secondary" type="button" id="modal-cancel">Cancelar</button><button class="btn" type="submit">Guardar</button></div></form>`;$('#modal-backdrop').classList.remove('hidden');$('#modal-cancel').onclick=closeModal;const form=$('#modal-form');form.onsubmit=async e=>{e.preventDefault();const b=form.querySelector('button[type=submit]');b.disabled=true;try{await submit(new FormData(form));closeModal();await load();alertMsg('Registro guardado correctamente.')}catch(err){alertMsg(err.message,'bad')}finally{b.disabled=false}}}
function openInfo(title,html){$('#modal-title').textContent=title;$('#modal-content').innerHTML=`<div class="modal-body">${html}<div class="modal-foot no-print"><button class="btn secondary" id="modal-print" type="button">Imprimir / Guardar PDF</button><button class="btn" id="modal-done" type="button">Cerrar</button></div></div>`;$('#modal-backdrop').classList.remove('hidden');$('#modal-print').onclick=()=>window.print();$('#modal-done').onclick=closeModal}
function closeModal(){
 const inventoryForm=$('#modal-form[data-inventory-context]');
 if(inventoryForm?.dataset.inventoryBusy==='true')return alertMsg('La operación de bodega se está procesando. Espera su resultado antes de cerrar.','bad');
 if(checkDraft&&checklistIsOpen()){
  if(checkSubmitting)return alertMsg('La inspección se está guardando. Espera a que termine.','bad');
  if(checkPhotoBusy)return alertMsg('Las fotos se están preparando. Espera a que termine antes de cerrar.','bad');
  if(!flushCheckDraft())return;
 }
 $('#modal-backdrop').classList.add('hidden');$('#modal-content').innerHTML='';
}
function f(label,name,type='text',extra=''){return `<div class="field"><label for="fld-${name}">${label}</label><input id="fld-${name}" type="${type}" name="${name}" ${extra}></div>`}
function s(label,name,options,extra=''){return `<div class="field"><label>${label}</label><select name="${name}" ${extra}>${options}</select></div>`}
function area(label,name,extra=''){return `<div class="field full"><label>${label}</label><textarea name="${name}" ${extra}></textarea></div>`}
// Inventory retries retain both their identity and their exact payload before any network send.
// This journal is separate from inspection drafts and IndexedDB; inventory is never synced automatically.
function inventoryStorageKey(context){return `ge_inventory_pending:${context}`}
function getInventoryOperation(context,path){
 try{
  const raw=localStorage.getItem(inventoryStorageKey(context));if(raw===null)return null;
  const operation=JSON.parse(raw),payload=JSON.parse(operation.body);
  if(operation.version!==1||typeof operation.key!=='string'||!operation.key||operation.path!==path||!payload||typeof payload!=='object'||Array.isArray(payload)||!operation.fields||typeof operation.fields!=='object'||Array.isArray(operation.fields))throw new Error();
  return operation;
 }catch{throw new Error('No se pudo leer la operación pendiente de bodega. No se enviará otra operación hasta recuperar su comprobante local.')}
}
function saveInventoryOperation(context,operation){
 try{localStorage.setItem(inventoryStorageKey(context),JSON.stringify(operation))}
 catch{const error=new Error('No se pudo guardar la clave de la operación en este navegador. No se envió ningún movimiento; revisa el almacenamiento local antes de confirmar.');error.notSent=true;throw error}
}
function removeInventoryOperation(context,operation){
 // An older response must never erase a different operation opened in another tab.
 const stored=getInventoryOperation(context,operation.path);
 if(stored&&stored.key!==operation.key)throw new Error('Existe otra operación pendiente en este navegador.');
 localStorage.removeItem(inventoryStorageKey(context));
}
async function sendInventoryOperation(operation){
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
 try{
  let response;
  try{response=await fetch(operation.path,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':operation.key},body:operation.body,signal:controller.signal})}
  catch{setOnline(false);throw new Error('No se recibió una respuesta del servidor dentro del tiempo disponible.')}
  setOnline(true);
  let result;
  try{result=await response.json()}catch{throw new Error('No se pudo verificar la respuesta del servidor.')}
  if(!response.ok){
   const detail=result?.detail,reason=typeof detail==='string'?detail:detail?.message||JSON.stringify(detail)||'El servidor no confirmó la operación.';
   const error=new Error(reason);
   error.definitive=response.status>=400&&response.status<500&&response.status!==408&&detail?.code!=='idempotency_conflict';
   error.idempotencyConflict=detail?.code==='idempotency_conflict';
   throw error;
  }
  if(result?.ok!==true)throw new Error('El servidor respondió sin confirmar la operación.');
  return result;
 }finally{clearTimeout(timer)}
}
function openInventoryModal(title,html,context,path,buildPayload){
 let operation=getInventoryOperation(context,path),busy=false;
 $('#modal-title').textContent=title;
 $('#modal-content').innerHTML=`<form id="modal-form" class="modal-body" data-inventory-context="${esc(context)}">${html}<div id="inventory-status" class="notice" role="status" style="margin-top:13px"></div><div class="modal-foot"><button class="btn secondary" type="button" id="modal-cancel">Cerrar</button><button class="btn" type="submit" id="inventory-confirm">Confirmar operación</button></div></form>`;
 $('#modal-backdrop').classList.remove('hidden');$('#modal-cancel').onclick=closeModal;
 const form=$('#modal-form'),button=$('#inventory-confirm'),status=$('#inventory-status');
 const lockFields=value=>$$('input,select,textarea',form).forEach(field=>field.disabled=value);
 const showStatus=(message,warn=false)=>{status.className=`notice${warn?' warn':''}`;status.textContent=message};
 const showUnknown=reason=>{
  lockFields(true);button.textContent='Reintentar operación';
  showStatus(`RESULTADO DESCONOCIDO. ${reason||'Se recuperó una operación pendiente.'} Reintenta para consultar o confirmar la misma operación; sus datos y su clave se conservan. Clave: ${operation.key}`,true);
 };
 if(operation){
  for(const [name,value] of Object.entries(operation.fields)){const field=form.elements.namedItem(name);if(field)field.value=value}
  showUnknown();
 }else showStatus('La operación quedará confirmada cuando el servidor responda.');
 form.onsubmit=async event=>{
  event.preventDefault();if(busy)return;
  busy=true;form.dataset.inventoryBusy='true';button.disabled=true;
  try{
   // Recheck the journal so an operation recovered by another tab is not overwritten.
   const stored=getInventoryOperation(context,path);
   if(stored){
    operation=stored;
    for(const [name,value] of Object.entries(operation.fields)){const field=form.elements.namedItem(name);if(field)field.value=value}
   }else if(!operation){
    const data=new FormData(form);
    operation={version:1,key:uid(),path,body:JSON.stringify(buildPayload(data)),fields:Object.fromEntries(data.entries())};
   }
   if(!stored)saveInventoryOperation(context,operation);
   lockFields(true);showStatus(`PROCESANDO. Espera la confirmación de esta operación. Clave: ${operation.key}`);
   await sendInventoryOperation(operation);
   try{removeInventoryOperation(context,operation)}
   catch{
    showStatus(`OPERACIÓN CONFIRMADA. No se pudo limpiar su comprobante local. Cierra y reabre para verificar la misma clave antes de realizar otra operación. Clave: ${operation.key}`,true);
    button.textContent='Verificar operación confirmada';return;
   }
   operation=null;busy=false;form.dataset.inventoryBusy='false';closeModal();await load();
   alertMsg('Operación CONFIRMADA. El movimiento de bodega se registró una sola vez.');
  }catch(error){
   if(error.notSent){
    operation=null;lockFields(false);button.textContent='Confirmar operación';showStatus(`OPERACIÓN NO ENVIADA. ${error.message}`,true);
   }else if(operation&&error.definitive){
    try{removeInventoryOperation(context,operation);operation=null;lockFields(false);button.textContent='Confirmar operación';showStatus(`OPERACIÓN NO CONFIRMADA. ${error.message} Corrige los datos antes de confirmar nuevamente.`,true)}
    catch{showUnknown(`${error.message} No se pudo limpiar el comprobante local.`)}
   }else if(operation){
    showUnknown(error.idempotencyConflict?`${error.message} La clave requiere revisión; no se generará una operación nueva.`:error.message);
   }else showStatus(error.message,true);
  }finally{busy=false;form.dataset.inventoryBusy='false';button.disabled=false}
 };
}
function fleetInteger(value,label,min,max){
 const text=String(value??'').trim(),number=Number(text);
 if(!text||!Number.isSafeInteger(number)||number<min||number>max)throw new Error(`${label}: introduce un número entero entre ${min.toLocaleString('es-EC')} y ${max.toLocaleString('es-EC')}.`);
 return number;
}
function validServiceDate(value){
 const match=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value||'');if(!match)return false;
 const year=Number(match[1]),month=Number(match[2]),day=Number(match[3]),days=[31,year%4===0&&(year%100!==0||year%400===0)?29:28,31,30,31,30,31,31,30,31,30,31];
 return year>=1&&month>=1&&month<=12&&day>=1&&day<=days[month-1];
}
function newVehicle(v){
 const original=v?{...v}:null,typeOpts=['LIVIANO','PESADO','ESPECIAL','POR_VERIFICAR'].map(t=>`<option ${original?.vehicle_type===t?'selected':''}>${t}</option>`).join('');
 openModal(original?'Editar vehículo':'Nuevo vehículo',`<div class="fields">${f('Placa','plate','text',`required maxlength="20" value="${esc(original?.plate||'')}"`)}${f('Nombre','name','text',`required value="${esc(original?.name||'')}"`)}${s('Tipo','vehicle_type',typeOpts)}${f('Marca','brand','text',`value="${esc(original?.brand||'')}"`)}${f('Modelo','model','text',`value="${esc(original?.model||'')}"`)}${f('Kilometraje conocido','odometer','number',`min="0" step="1" ${original?.odometer>10000000?'':'max="10000000"'} required value="${original?.odometer??0}"`)}${area('Notas','notes',`maxlength="500"`)}<div class="full notice">La lectura se actualiza también desde inspecciones y mantenimientos. Para reducir una lectura errónea, usa Corregir kilometraje e indica el motivo.</div></div>`,async d=>{
  const values={plate:d.get('plate'),name:d.get('name'),vehicle_type:d.get('vehicle_type'),brand:d.get('brand'),model:d.get('model'),notes:d.get('notes')};
  if(!String(values.plate).trim()||!String(values.name).trim())throw new Error('Completa la placa y el nombre del vehículo.');
  if(!original)return post('/api/vehicles',{...values,odometer:fleetInteger(d.get('odometer'),'Kilometraje',0,10000000)});
  // Unedited fields are omitted: an old form cannot overwrite newer server readings.
  const payload=Object.fromEntries(Object.entries(values).filter(([key,value])=>value!==String(original[key]??''))),reading=String(d.get('odometer')??'').trim();
  if(!reading)throw new Error('Completa el kilometraje conocido.');
  if(Number(reading)!==original.odometer){
   const odometer=fleetInteger(reading,'Kilometraje',0,10000000);
   if(odometer<original.odometer)throw new Error('La edición normal no permite reducir el kilometraje. Usa Corregir kilometraje e indica el motivo.');
   payload.odometer=odometer;payload.expected_odometer=original.odometer;
  }
  if(!Object.keys(payload).length)return;
  return patch(`/api/vehicles/${original.id}`,payload);
 });
 if(original)$('[name=notes]',$('#modal-content')).value=original.notes||'';
}
function correctOdometer(id){
 const original=state.vehicles.find(v=>v.id===id);if(!original)return;
 const expected=original.odometer;
 openModal(`Corregir kilometraje: ${original.plate}`,`<div class="fields">${f('Lectura corregida (km)','odometer','number',`required min="0" max="10000000" step="1" value="${Math.min(expected,10000000)}"`)}${f('Fecha de la corrección','corrected_on','date',`required value="${nowDate()}"`)}${f('Responsable de la corrección','responsible','text','required maxlength="100"')}${area('Motivo de la corrección','reason','required maxlength="500"')}</div><div class="notice warn" style="margin-top:13px">Lectura actual: ${expected.toLocaleString('es-EC')} km. Esta acción registra la lectura anterior, la nueva, el motivo y el responsable. El historial de servicios e inspecciones se conserva.</div>`,d=>{
  const odometer=fleetInteger(d.get('odometer'),'Lectura corregida',0,10000000),reason=String(d.get('reason')??'').trim(),responsible=String(d.get('responsible')??'').trim(),corrected_on=d.get('corrected_on');
  if(odometer===expected)throw new Error('La lectura corregida debe ser diferente de la lectura actual.');
  if(!reason||reason.length>500||!responsible||responsible.length>100)throw new Error('Indica el motivo (máximo 500 caracteres) y el responsable (máximo 100 caracteres).');
  if(!validServiceDate(corrected_on))throw new Error('Indica una fecha válida para la corrección.');
  return post(`/api/vehicles/${id}/odometer-correction`,{odometer,expected_odometer:expected,reason,responsible,corrected_on});
 });
}
function vehicleDetail(id){const v=state.vehicles.find(x=>x.id===id),p=state.compatibility.filter(x=>x.vehicle_id===id).map(c=>state.parts.find(p=>p.id===c.part_id)).filter(Boolean),due=latestDue().filter(x=>x.vehicle_id===id);if(!v)return;openInfo(`Ficha: ${v.plate}`,`<div class="print-sheet"><h3>${esc(v.name)} · ${esc(v.plate)}</h3><p>Tipo: ${esc(v.vehicle_type)} · Kilometraje: ${v.odometer.toLocaleString('es-EC')} km</p><p>${esc(v.notes||'Sin observaciones')}</p><h4>Repuestos compatibles registrados</h4>${p.length?p.map(x=>`<p>• ${esc(x.code)} · ${esc(x.name)} · ${esc(x.part_number||'Sin número de parte')} · Stock ${milli(x.stock_milli)} ${esc(x.unit)}</p>`).join(''):'<p>No se ha validado ninguna compatibilidad todavía.</p>'}<h4>Próximos servicios</h4>${due.length?due.map(m=>`<p>• ${esc(m.service_name)}: ${m.next_due_km.toLocaleString('es-EC')} km (${m.kmLeft} km restantes)</p>`).join(''):'<p>Sin planes de mantenimiento registrados.</p>'}<button class="btn secondary no-print" data-action="vehicle-maintenance-history" data-id="${id}">Abrir historial técnico</button></div>`)}
function newPart(){openModal('Registrar repuesto vehicular',`<div class="fields">${f('Código interno','code','text','required')}${f('Nombre del repuesto','name','text','required')}${f('Número de parte del fabricante','part_number')}${s('Categoría','category',['Filtros','Bandas','Baterías','Lubricantes','Frenos','Mecánica','Otros'].map(x=>`<option>${x}</option>`).join(''))}${s('Unidad','unit',['unidad','galón','litro','metro'].map(x=>`<option value="${x}">${x}</option>`).join(''))}${f('Stock mínimo','min_qty','number','min="0" step="0.001" value="0" required')}${area('Observaciones / especificaciones','notes')}<div class="field full"><label>Vehículos compatibles (solo los validados)</label><div class="pill-row">${state.vehicles.map(v=>`<label class="badge gray"><input type="checkbox" name="compat" value="${v.id}"> ${esc(v.plate)}</label>`).join('')}</div></div></div>`,d=>post('/api/parts',{code:d.get('code'),name:d.get('name'),part_number:d.get('part_number'),category:d.get('category'),unit:d.get('unit'),min_qty:d.get('min_qty'),notes:d.get('notes'),compatible_vehicle_ids:d.getAll('compat').map(Number)}))}
function newMovement(type='ENTRADA'){if(!state.parts.length)return alertMsg('Registra un repuesto antes de crear movimientos.','bad');openInventoryModal(type==='ENTRADA'?'Entrada de bodega':'Salida de bodega',`<div class="fields">${s('Repuesto','part_id',partOptions(),'required')}${f('Cantidad','quantity','number','required min="0.001" step="0.001"')}${f('Responsable','responsible','text','required')}${s('Vehículo destino (opcional)','vehicle_id',vehicleOptions())}${f('Documento / referencia','reference')}${area('Observaciones','notes')}</div><div class="notice warn" style="margin-top:13px">Las salidas descuentan existencias al confirmar el movimiento. No se puede retirar más de lo disponible.</div>`,`movement:${type}`,'/api/movements',d=>({part_id:Number(d.get('part_id')),movement_type:type,quantity:d.get('quantity'),responsible:d.get('responsible'),vehicle_id:d.get('vehicle_id')?Number(d.get('vehicle_id')):null,reference:d.get('reference'),notes:d.get('notes')}))}
function newMaintenance(){
 if(!state.vehicles.length)return alertMsg('Primero registra un vehículo.','bad');
 openModal('Registrar mantenimiento realizado',`<div class="fields">${s('Vehículo y placa','vehicle_id',vehicleOptions(),'required')}${s('Tipo de mantenimiento','maintenance_type','<option value="">Sin clasificar</option><option value="MPG">Preventivo (MPG)</option><option value="CORRECTIVO">Correctivo</option>')}${f('Servicio / familia del plan','service_name','text','required maxlength="250" placeholder="Ej. Cambio de aceite; usa el mismo nombre para su plan"')}${f('Fecha del servicio','performed_on','date',`required value="${nowDate()}"`)}${f('Kilometraje en el servicio','odometer','number','min="0" max="10000000" step="1" required')}${f('Intervalo hasta próximo servicio (km, opcional)','interval_km','number','min="1" max="1000000" step="1" placeholder="Definido para este servicio"')}${f('Próximo mantenimiento (km, calculado)','next_due_preview','text','readonly placeholder="Sin plan configurado"')}${f('Responsable o mecánico','responsible','text','maxlength="150"')}${f('Lugar del trabajo','location','text','maxlength="150"')}${area('Trabajo realizado','work_performed','maxlength="4000"')}${area('Observaciones','notes','maxlength="4000"')}</div><div class="notice" id="maintenance-schedule-notice" style="margin-top:13px">Cada familia de servicio conserva su propio plan. Los trabajos correctivos no admiten intervalo ni reinician planes preventivos. Usa la fecha y lectura reales para servicios históricos; no reducen el odómetro actual.</div><div class="notice" style="margin-top:13px">Este formulario registra el trabajo, pero no descuenta repuestos. Registra la entrega en bodega para afectar existencias.</div>`,d=>{
  const vehicle_id=fleetInteger(d.get('vehicle_id'),'Vehículo',1,Number.MAX_SAFE_INTEGER),service_name=String(d.get('service_name')??'').trim(),performed_on=d.get('performed_on'),interval=String(d.get('interval_km')??'').trim(),maintenance_type=d.get('maintenance_type');
  if(!service_name||service_name.length>250)throw new Error('Indica el servicio realizado (máximo 250 caracteres).');
  if(!validServiceDate(performed_on))throw new Error('Indica una fecha de servicio válida.');
  if(maintenance_type==='CORRECTIVO'&&interval)throw new Error('Un mantenimiento correctivo no configura el próximo servicio. Deja el intervalo vacío para conservar los planes preventivos.');
  const texts={};for(const [key,label,max] of [['work_performed','Trabajo realizado',4000],['responsible','Responsable o mecánico',150],['location','Lugar del trabajo',150],['notes','Observaciones',4000]]){texts[key]=String(d.get(key)??'');if(texts[key].length>max)throw new Error(`${label}: máximo ${max} caracteres. El texto no se ha recortado.`)}
  return post('/api/maintenance',{vehicle_id,service_name,performed_on,odometer:fleetInteger(d.get('odometer'),'Kilometraje del servicio',0,10000000),interval_km:interval?fleetInteger(interval,'Intervalo del servicio',1,1000000):null,maintenance_type,...texts});
 });
 const form=$('#modal-form'),preview=form.elements.namedItem('next_due_preview');
 const updatePreview=()=>{const reading=form.elements.namedItem('odometer').value,interval=form.elements.namedItem('interval_km').value,type=form.elements.namedItem('maintenance_type').value,a=Number(reading),b=Number(interval);preview.value=type!=='CORRECTIVO'&&reading&&interval&&Number.isSafeInteger(a)&&a>=0&&a<=10000000&&Number.isSafeInteger(b)&&b>0&&b<=1000000?(a+b).toLocaleString('es-EC'):'';preview.placeholder=type==='CORRECTIVO'?'Correctivo: conserva los planes preventivos':'Sin plan configurado'};
 for(const name of ['odometer','interval_km','maintenance_type'])form.elements.namedItem(name).addEventListener('input',updatePreview);
 if(maintenanceHistory.vehicle_id)form.elements.namedItem('vehicle_id').value=maintenanceHistory.vehicle_id;
}
async function maintenanceDetail(id){
 const version=++maintenanceDetailVersion;openInfo('Registro de mantenimiento',`<div id="maintenance-detail-body" data-id="${id}" class="notice" role="status">Consultando detalle del servicio…</div>`);$('#modal-print').disabled=true;
 const isCurrent=()=>version===maintenanceDetailVersion&&!$('#modal-backdrop').classList.contains('hidden')&&$('#maintenance-detail-body')?.dataset.id===String(id);
 try{const m=await api(`/api/maintenance/${id}`);if(!isCurrent())return;openInfo('Registro de mantenimiento',`<div id="maintenance-detail-body" data-id="${id}" class="print-sheet" style="overflow-wrap:anywhere"><h3>GOLD ENGINE · REGISTRO DE MANTENIMIENTO</h3><p><strong>Vehículo:</strong> ${esc(m.plate)} · ${esc(m.vehicle_name)}</p><p><strong>Tipo de mantenimiento:</strong> ${esc(maintenanceTypeLabel(m.maintenance_type))}</p><p><strong>Servicio / familia del plan:</strong> ${esc(m.service_name)}</p><p><strong>Fecha:</strong> ${esc(serviceDateLabel(m.performed_on))}</p><p><strong>Kilometraje del servicio:</strong> ${m.odometer.toLocaleString('es-EC')} km</p><p><strong>Próximo mantenimiento:</strong> ${m.next_due_km==null?'No configurado para este registro':m.next_due_km.toLocaleString('es-EC')+' km'}</p><p><strong>Intervalo:</strong> ${m.interval_km==null?'Sin intervalo':m.interval_km.toLocaleString('es-EC')+' km'}</p><p><strong>Responsable o mecánico:</strong> ${esc(m.responsible||'No registrado')}</p><p><strong>Lugar del trabajo:</strong> ${esc(m.location||'No registrado')}</p><p><strong>Trabajo realizado:</strong></p><div class="pre">${esc(m.work_performed||'No registrado en campo separado')}</div><p><strong>Observaciones:</strong></p><div class="pre">${esc(m.notes||'Sin observaciones')}</div>${!m.maintenance_type?'<p class="inline-note">Registro sin clasificación. Los textos originales se conservan en observaciones.</p>':''}</div>`)}
 catch(error){if(!isCurrent())return;$('#maintenance-detail-body').innerHTML=`No se pudo consultar este servicio. ${esc(error.message)}<div style="margin-top:12px"><button class="btn secondary" data-action="maintenance-detail" data-id="${id}">Reintentar detalle</button></div>`}
}
function newRequest(){if(!state.parts.length)return alertMsg('Primero registra los repuestos de la bodega.','bad');if(!state.vehicles.length)return alertMsg('Primero registra un vehículo.','bad');openModal('Solicitud de repuestos',`<div class="fields">${s('Vehículo','vehicle_id',vehicleOptions(),'required')}${f('Nombre del solicitante','requester','text','required')}${s('Repuesto','part_id',partOptions(),'required')}${f('Cantidad solicitada','quantity','number','min="0.001" step="0.001" required')}${area('Motivo de la solicitud','reason')}</div><div class="notice" style="margin-top:13px">Esta solicitud tendrá una línea inicialmente. Se podrán registrar nuevas solicitudes sin descontar existencias.</div>`,d=>post('/api/requests',{vehicle_id:Number(d.get('vehicle_id')),requester:d.get('requester'),reason:d.get('reason'),items:[{part_id:Number(d.get('part_id')),quantity:d.get('quantity')}]}))}
async function requestDetail(id){
 const data=await api(`/api/requests/${id}`),v=state.vehicles.find(x=>x.id===data.vehicle_id);
 const context=`delivery:${id}`,path=`/api/requests/${id}/deliver`,operation=getInventoryOperation(context,path);
 const lines=data.lines.map(l=>`<tr><td>${esc(l.code)}<div class="sub">${esc(l.name)}</div></td><td>${milli(l.qty_milli)} ${esc(l.unit)}</td><td>${milli(l.delivered_milli)}</td></tr>`).join('');
 const buttons=`<div class="btn-group no-print" style="margin-top:14px">${data.status==='PENDIENTE'?`<button type="button" class="btn" id="approve-btn">Confirmar autorización</button>`:''}${operation||['APROBADA','PARCIAL'].includes(data.status)?`<button type="button" class="btn teal" id="deliver-btn">${operation?'Resolver entrega pendiente':'Registrar entrega'}</button>`:''}</div>`;
 openInfo(`Solicitud SOL-${String(id).padStart(5,'0')}`,`<div class="print-sheet"><h3>GOLD ENGINE · SOLICITUD DE REPUESTOS</h3><p><b>Vehículo:</b> ${esc(v?.plate||'')} · ${esc(v?.name||'')}</p><p><b>Solicitante:</b> ${esc(data.requester)}</p><p><b>Estado:</b> ${esc(data.status)}</p><p><b>Autorización declarada por:</b> ${esc(data.approved_by||'Pendiente')}</p><p><b>Motivo:</b> ${esc(data.reason||'Sin observaciones')}</p>${table(['Repuesto','Solicitado','Entregado'],lines)}<div class="signature"><div>Responsable bodega</div><div>Quien recibe</div></div></div>${buttons}`);
 const approveBtn=$('#approve-btn');if(approveBtn)approveBtn.onclick=async()=>{const name=prompt('Nombre de quien autoriza (declaración manual, sin identidad verificada):');if(!name)return;try{await post(`/api/requests/${id}/approve`,{approved_by:name});closeModal();await load();alertMsg('Solicitud autorizada sin descontar existencias.')}catch(e){alertMsg(e.message,'bad')}};
 const deliverBtn=$('#deliver-btn');if(deliverBtn)deliverBtn.onclick=()=>{
  try{
   // A lost reply may already have completed the request. Keep its original lines available for replay.
   const saved=getInventoryOperation(context,path),savedLines=saved?JSON.parse(saved.body).lines:null;
   const pending=savedLines?savedLines.map(line=>data.lines.find(x=>x.id===line.line_id)).filter(Boolean):data.lines.filter(x=>x.qty_milli>x.delivered_milli);
   openInventoryModal('Entrega de repuestos',`<p class="small muted">Solicitud SOL-${String(id).padStart(5,'0')} · las cantidades se descuentan al confirmar.</p><div class="fields">${f('Entregado por','delivered_by','text','required')}${f('Recibido por','received_by','text','required')}</div><div class="divider"></div>${pending.map(x=>`<div class="field" style="margin-bottom:11px"><label>${esc(x.code)} · ${esc(x.name)} · ${saved?'Cantidad de la operación pendiente':'Pendiente '+milli(x.qty_milli-x.delivered_milli)+' '+esc(x.unit)}</label><input type="number" min="0" step="0.001" name="line-${x.id}" value="${milli(x.qty_milli-x.delivered_milli).replaceAll('.','').replace(',','.')}" required></div>`).join('')}`,context,path,d=>{
    const lines=pending.map(x=>({line_id:x.id,quantity:d.get(`line-${x.id}`)})).filter(x=>Number(x.quantity)>0);
    if(!lines.length)throw new Error('Indica una cantidad mayor que cero.');
    return {delivered_by:d.get('delivered_by'),received_by:d.get('received_by'),lines};
   });
  }catch(error){alertMsg(error.message,'bad')}
 };
}
const getChecklist=()=>state.template.flatMap((g,gi)=>g.items.map((text,ii)=>({id:`${gi}-${ii}`,group:g.group,text})));
function checklistIsOpen(){return !!$('#modal-content #check-step')&&!$('#modal-backdrop').classList.contains('hidden')}
function getCheckDraft(){
 try{const draft=JSON.parse(localStorage.getItem('ge_check_draft')||'null');return draft&&typeof draft==='object'&&!Array.isArray(draft)?draft:null}
 catch{alertMsg('No se pudo leer el borrador de este navegador. Comprueba que el almacenamiento local esté disponible.','bad');return null}
}
function saveCheckDraft(reportError=false){
 if(!checkDraft)return true;
 try{localStorage.setItem('ge_check_draft',JSON.stringify(checkDraft));checkStorageError=false;return true}
 catch{
  if(reportError||!checkStorageError)alertMsg('No se pudo guardar el borrador en este navegador. Los datos siguen en memoria; no recargues ni cierres la pestaña hasta resolver el almacenamiento local.','bad');
  checkStorageError=true;return false;
 }
}
function flushCheckDraft(){
 clearTimeout(checkSaveTimer);checkSaveTimer=null;
 if(!checkDraft||!checklistIsOpen()||checkSubmitting)return true;
 storeCheckInputs(false);return saveCheckDraft(true);
}
function scheduleCheckSave(){
 if(checkSubmitting||!storeCheckInputs(false))return;
 clearTimeout(checkSaveTimer);
 checkSaveTimer=setTimeout(()=>{checkSaveTimer=null;saveCheckDraft()},250);
}
function newInspection(){
 if(!state.template.length)return alertMsg('Debes abrir la aplicación una vez con conexión para descargar el formulario.','bad');
 if(!state.vehicles.some(x=>x.vehicle_type==='LIVIANO'))return alertMsg('No hay vehículos livianos disponibles.','bad');
 flushCheckDraft();
 checkDraft=checkDraft||getCheckDraft()||{client_uuid:uid(),vehicle_id:state.vehicles.find(x=>x.vehicle_type==='LIVIANO').id,date:nowDate(),inspector:'',odometer:0,answers:{},observations:'',photos:[]};
 checkDraft.answers=checkDraft.answers||{};checkDraft.itemNotes=checkDraft.itemNotes||{};
 photos=Array.isArray(checkDraft.photos)?checkDraft.photos:[];checkDraft.photos=photos;
 checkSection=0;showChecklist();saveCheckDraft();
}
function showChecklist(){const d=checkDraft;if(!d)return;const groups=state.template;$('#modal-title').textContent='Inspección de vehículo liviano';$('#modal-backdrop').classList.remove('hidden');let total=getChecklist().length, answered=Object.keys(d.answers).length;$('#modal-content').innerHTML=`<div class="modal-body"><div class="check-head"><small>GOLD ENGINE · CHECKLIST LIVIANO · FORMATO INICIAL</small><h2>${checkSection===0?'Datos de inspección':checkSection>groups.length?'Finalizar':esc(groups[checkSection-1].group)}</h2><div class="check-top"><div><small>Preguntas</small><b>${total}</b></div><div><small>Contestadas</small><b>${answered}</b></div><div><small>Fallas</small><b>${Object.values(d.answers).filter(x=>x==='MAL').length}</b></div></div><div class="progress"><span style="width:${Math.round(answered/total*100)}%"></span></div></div><div class="step-tabs">${['Datos',...groups.map(x=>x.group),'Finalizar'].map((x,i)=>`<button type="button" class="${checkSection===i?'active':''}" data-check-section="${i}">${i===0?'00':String(i).padStart(2,'0')} · ${esc(x)}</button>`).join('')}</div><div id="check-step">${checkStepHTML()}</div><div class="step-foot"><button class="btn secondary" id="check-prev" type="button" ${checkSection===0?'disabled':''}>← Atrás</button><span class="small muted">Paso ${checkSection+1} de ${groups.length+2}</span><button class="btn" id="check-next" type="button">${checkSection===groups.length+1?'Guardar inspección':'Siguiente →'}</button></div><div class="inline-note" style="margin-top:10px">El borrador se guarda en este navegador. Las fotos se reducen para facilitar el uso offline.</div></div>`;
 $$('[data-check-section]',$('#modal-content')).forEach(btn=>btn.onclick=()=>{storeCheckInputs();checkSection=Number(btn.dataset.checkSection);showChecklist()});$('#check-prev').onclick=()=>{storeCheckInputs();checkSection--;showChecklist()};$('#check-next').onclick=async()=>{storeCheckInputs();if(checkSection<groups.length+1){checkSection++;showChecklist()}else await submitCheck()};
 const step=$('#check-step');
 for(const name of ['vehicle_id','date','inspector','odometer','observations']){const field=$(`[name="${name}"]`,step);if(field)field.value=d[name]??''}
 step.addEventListener('input',scheduleCheckSave);step.addEventListener('change',scheduleCheckSave);updateCheckTextStatus();
 const file=$('#check-photos');if(file)file.onchange=async()=>{
  const selected=Array.from(file.files||[]);if(!selected.length)return;
  if(selected.length>3){file.value='';return alertMsg('Selecciona como máximo tres fotografías. Las fotos anteriores se conservaron.','bad')}
  const draft=d,version=++checkPhotoVersion;checkPhotoBusy=true;
  try{
   const compressed=[];for(const img of selected)compressed.push(await compressImage(img));
   if(checkDraft!==draft||version!==checkPhotoVersion)return;
   photos=compressed;draft.photos=photos;const count=$('#check-photo-count');if(count)count.textContent=`Fotos preparadas: ${photos.length}`;
   if(saveCheckDraft())alertMsg(`${photos.length} foto(s) preparada(s).`);
  }catch(e){alertMsg(e.message,'bad')}
  finally{if(version===checkPhotoVersion)checkPhotoBusy=false}
 };
}
function checkStepHTML(){const d=checkDraft;if(checkSection===0)return `<div class="fields">${s('Vehículo liviano','vehicle_id',vehicleOptions('LIVIANO'),'required')}${f('Fecha de inspección','date','date',`required value="${esc(d.date??'')}"`)}${f('Inspector / conductor','inspector','text',`required value="${esc(d.inspector??'')}"`)}${f('Kilometraje actual','odometer','number',`min="0" step="1" required value="${esc(d.odometer??'')}"`)}</div><div class="notice" style="margin-top:15px">Se utiliza únicamente la ficha liviana completada. El checklist de vehículos pesados no está disponible todavía.</div>`;
 if(checkSection<=state.template.length){const gi=checkSection-1;return state.template[gi].items.map((item,ii)=>{const key=`${gi}-${ii}`,res=d.answers[key]||'';return `<div class="question"><b>${ii+1}. ${esc(item)}</b><div class="choices">${[['BIEN','Buen estado'],['MAL','Mal estado'],['NA','No aplica']].map(([value,label])=>`<label class="${value==='MAL'?'bad':''}"><input type="radio" name="answer-${key}" value="${value}" ${res===value?'checked':''}>${label}</label>`).join('')}</div><div class="field" style="margin-top:10px"><textarea class="obs-field" data-key="${key}" rows="1" placeholder="Observación (especialmente si está en mal estado)...">${esc(d.itemNotes?.[key]||'')}</textarea></div></div>`}).join('')}
 return `<div class="fields">${area('Observaciones generales / daños visibles','observations','placeholder="Golpes, rayones, fallas y detalles observados..."')}<div class="field full"><span class="inline-note" id="check-observations-status" role="status" aria-live="polite"></span></div><div class="field full"><label>Fotografías (máximo 3, opcionales)</label><input type="file" id="check-photos" accept="image/*" capture="environment" multiple><span class="inline-note" id="check-photo-count">Fotos preparadas: ${photos.length}</span></div></div><div class="divider"></div><div class="notice">Respondidas: ${Object.keys(d.answers).length}/${getChecklist().length}. Fallas: ${Object.values(d.answers).filter(x=>x==='MAL').length}. Se puede guardar sin conexión y sincronizar después.</div>`}
function checkObservations(d){return [d.observations,...getChecklist().filter(x=>d.itemNotes?.[x.id]).map(x=>`${x.group} / ${x.text}: ${d.itemNotes[x.id]}`)].filter(Boolean).join('\n')}
function updateCheckTextStatus(){
 const status=$('#check-observations-status');if(!status||!checkDraft)return;
 const count=Array.from(checkObservations(checkDraft)).length;
 status.textContent=`Observaciones combinadas: ${count}/${CHECK_OBSERVATIONS_LIMIT} caracteres (incluye notas por ítem y sus títulos).${count>CHECK_OBSERVATIONS_LIMIT?' Supera el límite: reduce el texto antes de guardar la inspección. El borrador conserva el texto completo.':''}`;
}
function storeCheckInputs(persist=true){
 const d=checkDraft,step=$('#modal-content #check-step');if(!d||!step||!checklistIsOpen()||checkSubmitting)return false;
 for(const name of ['vehicle_id','date','inspector','odometer','observations']){
  const field=$(`[name="${name}"]`,step);if(!field)continue;
  d[name]=['vehicle_id','odometer'].includes(name)?(field.value===''?'':Number(field.value)):field.value;
 }
 $$('input[name^="answer-"]:checked',step).forEach(field=>d.answers[field.name.slice(7)]=field.value);
 $$('textarea[data-key]',step).forEach(field=>{d.itemNotes=d.itemNotes||{};d.itemNotes[field.dataset.key]=field.value});
 updateCheckTextStatus();
 if(persist){clearTimeout(checkSaveTimer);checkSaveTimer=null;return saveCheckDraft()}
 return true;
}
async function compressImage(file){if(file.size>10*1024*1024)throw Error('La foto supera 10 MB.');const bitmap=await createImageBitmap(file);let scale=Math.min(1,960/Math.max(bitmap.width,bitmap.height));let c=document.createElement('canvas');c.width=Math.max(1,Math.round(bitmap.width*scale));c.height=Math.max(1,Math.round(bitmap.height*scale));c.getContext('2d').drawImage(bitmap,0,0,c.width,c.height);bitmap.close();let img=c.toDataURL('image/jpeg',0.58);if(img.length>350000)throw Error('La fotografía es muy grande. Utiliza menor resolución.');return img}
function validCheckDate(value){
 const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(value||'');if(!m)return false;
 const year=Number(m[1]),month=Number(m[2]),day=Number(m[3]);
 const days=[31,year%4===0&&(year%100!==0||year%400===0)?29:28,31,30,31,30,31,31,30,31,30,31];
 return year>=1&&month>=1&&month<=12&&day>=1&&day<=days[month-1];
}
async function sendCheckInspection(payload){
 let response;
 try{response=await fetch('/api/inspections',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})}
 catch{setOnline(false);const error=new Error('Sin conexión con el servidor local.');error.offline=true;throw error}
 setOnline(true);
 if(!response.ok){let reason='La inspección no pudo guardarse.';try{const data=await response.json();reason=typeof data.detail==='string'?data.detail:JSON.stringify(data.detail)}catch{}throw new Error(reason)}
 return response.json();
}
function discardCheckDraft(){
 clearTimeout(checkSaveTimer);checkSaveTimer=null;checkPhotoVersion++;checkDraft=null;photos=[];
 try{localStorage.removeItem('ge_check_draft');return true}catch{return false}
}
function setCheckSubmitting(value){
 checkSubmitting=value;
 $$('#modal-content input,#modal-content textarea,#modal-content select,#modal-content button').forEach(field=>{
  if(value){field.dataset.checkWasDisabled=field.disabled?'1':'0';field.disabled=true}
  else if(field.dataset.checkWasDisabled!==undefined){field.disabled=field.dataset.checkWasDisabled==='1';delete field.dataset.checkWasDisabled}
 });
}
async function submitCheck(){
 if(checkSubmitting||!checkDraft)return;
 storeCheckInputs();const d=checkDraft;
 if(checkPhotoBusy)return alertMsg('Las fotos se están preparando. Espera a que termine antes de guardar.','bad');
 const inspector=String(d.inspector??'').trim(),odometer=Number(d.odometer),vehicle=state.vehicles.find(v=>v.id===Number(d.vehicle_id)&&v.vehicle_type==='LIVIANO');
 if(!vehicle||!validCheckDate(d.date)||!inspector||String(d.odometer??'').trim()===''||!Number.isSafeInteger(odometer)||odometer<0)return alertMsg('Completa vehículo liviano, fecha válida, inspector / conductor y kilometraje entero no negativo.','bad');
 if(Array.from(inspector).length>250)return alertMsg('El inspector / conductor supera el límite de 250 caracteres. Reduce el texto antes de guardar.','bad');
 const keys=getChecklist(),missing=keys.filter(x=>!['BIEN','MAL','NA'].includes(d.answers[x.id]));
 if(missing.length)return alertMsg(`Faltan ${missing.length} preguntas válidas. Responde todas con BIEN, MAL o N/A.`, 'bad');
 const missingNote=keys.filter(x=>d.answers[x.id]==='MAL'&&!String(d.itemNotes?.[x.id]||'').trim());
 if(missingNote.length)return alertMsg(`Escribe observaciones para ${missingNote.length} ítems marcados Mal.`, 'bad');
 const notes=checkObservations(d),count=Array.from(notes).length;
 if(count>CHECK_OBSERVATIONS_LIMIT)return alertMsg(`Las observaciones combinadas tienen ${count} caracteres; el límite es ${CHECK_OBSERVATIONS_LIMIT}. Reduce el texto antes de guardar. El borrador conserva todas las observaciones.`, 'bad');
 const payload={client_uuid:d.client_uuid,vehicle_id:vehicle.id,date:d.date,inspector,odometer,answers:Object.fromEntries(keys.map(x=>[x.id,d.answers[x.id]])),observations:notes,photos:d.photos||[]};
 clearTimeout(checkSaveTimer);checkSaveTimer=null;setCheckSubmitting(true);
 try{
  let offline=false;
  try{await sendCheckInspection(payload)}catch(e){if(!e.offline)throw e;await queueInspection(payload);offline=true}
  const cleared=discardCheckDraft();closeModal();
  if(offline)navigate('inspecciones');else await load();
  const message=offline?'Inspección guardada sin conexión. Sincronízala cuando regreses a la red.':'Inspección registrada en el servidor.';
  alertMsg(cleared?message:`${message} No se pudo limpiar el borrador del navegador.`,cleared?'ok':'bad');
 }catch(e){alertMsg(e.message||'No se pudo guardar la inspección. El borrador se conserva.','bad')}
 finally{setCheckSubmitting(false)}
}
async function inspectionDetail(id){const d=await api(`/api/inspections/${id}`),all=d.template.flatMap((g,gi)=>g.items.map((x,ii)=>({key:`${gi}-${ii}`,group:g.group,text:x})));const fails=all.filter(x=>d.answers[x.key]==='MAL');openInfo(`Inspección INS-${String(id).padStart(5,'0')}`,`<div class="print-sheet"><h3>GOLD ENGINE · INSPECCIÓN DE VEHÍCULOS LIVIANOS</h3><p><b>Vehículo:</b> ${esc(d.vehicle_name)} · ${esc(d.plate)}</p><p><b>Fecha:</b> ${esc(d.date)} &nbsp; <b>Inspector:</b> ${esc(d.inspector)} &nbsp; <b>Km:</b> ${d.odometer.toLocaleString('es-EC')}</p><p><b>Condiciones no conformes:</b> ${fails.length}</p>${table(['Sección / ítem','Resultado'],all.map(x=>`<tr><td>${esc(x.group)} · ${esc(x.text)}</td><td>${esc(d.answers[x.key])}</td></tr>`).join(''))}<h4>Observaciones</h4><div class="pre">${esc(d.observations||'Sin observaciones adicionales.')}</div>${d.photos.map(img=>`<img src="${img}" alt="Evidencia fotográfica" style="max-width:30%;margin:8px 1%;border-radius:6px">`).join('')}<div class="signature"><div>Inspector / conductor</div><div>Supervisor / bodega</div></div></div>`)}
function dbOpen(){return new Promise((resolve,reject)=>{const r=indexedDB.open('GE_FLEET_OFFLINE',1);r.onupgradeneeded=()=>{if(!r.result.objectStoreNames.contains('pending'))r.result.createObjectStore('pending',{keyPath:'client_uuid'})};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error)})}
async function dbAll(){const db=await dbOpen();return new Promise((resolve,reject)=>{const t=db.transaction('pending','readonly');const req=t.objectStore('pending').getAll();req.onsuccess=()=>{resolve(req.result);db.close()};req.onerror=()=>reject(req.error)})}
async function queueInspection(data){const db=await dbOpen();await new Promise((resolve,reject)=>{const t=db.transaction('pending','readwrite');t.objectStore('pending').put(data);t.oncomplete=resolve;t.onerror=()=>reject(t.error)});db.close();await countPending()}
async function removePending(id){const db=await dbOpen();await new Promise((resolve,reject)=>{const t=db.transaction('pending','readwrite');t.objectStore('pending').delete(id);t.oncomplete=resolve;t.onerror=()=>reject(t.error)});db.close()}
async function countPending(){try{const q=await dbAll();localStorage.setItem('ge_pending_count',String(q.length));return q.length}catch{return 0}}
async function syncPending(){const list=await dbAll();if(!list.length){alertMsg('No existen inspecciones pendientes.');return}let synced=0;for(const record of list){try{await post('/api/inspections',record);await removePending(record.client_uuid);synced++}catch(e){alertMsg(`Sincronizadas ${synced}/${list.length}. Pendiente: ${e.message}`,'bad');await countPending();return}}await countPending();await load();alertMsg(`${synced} inspecciones sincronizadas correctamente.`)}
$('#modal-close').addEventListener('click',closeModal);$('#modal-backdrop').addEventListener('click',e=>{if(e.target.id==='modal-backdrop')closeModal()});
window.addEventListener('pagehide',flushCheckDraft);window.addEventListener('beforeunload',flushCheckDraft);
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')flushCheckDraft()});
document.addEventListener('click',async e=>{const b=e.target.closest('[data-nav],[data-action],[data-mobile-menu]');if(!b)return;if(b.dataset.nav){e.preventDefault();navigate(b.dataset.nav);return}if(b.dataset.mobileMenu){openInfo('Más opciones',`<div class="stack"><button class="btn secondary" data-nav="mantenimientos" id="more-maint">Mantenimientos</button><button class="btn secondary" data-nav="historial" id="more-history">Historial y exportaciones</button></div>`);$('#more-maint').onclick=()=>{closeModal();navigate('mantenimientos')};$('#more-history').onclick=()=>{closeModal();navigate('historial')};return}
 const id=Number(b.dataset.id);try{switch(b.dataset.action){case'new-vehicle':newVehicle();break;case'edit-vehicle':newVehicle(state.vehicles.find(x=>x.id===id));break;case'vehicle-detail':vehicleDetail(id);break;case'vehicle-maintenance-history':openVehicleMaintenance(id);break;case'correct-odometer':correctOdometer(id);break;case'new-part':newPart();break;case'new-movement':newMovement(b.dataset.mtype||'ENTRADA');break;case'new-maintenance':newMaintenance();break;case'maintenance-detail':await maintenanceDetail(id);break;case'maintenance-history-retry':fetchMaintenanceHistory();break;case'maintenance-history-clear':Object.assign(maintenanceHistory,{vehicle_id:'',date_from:'',date_to:'',maintenance_type:'',page:1,data:null,error:''});navigate('mantenimientos',false);break;case'maintenance-history-prev':if(!maintenanceHistory.loading&&maintenanceHistory.page>1){maintenanceHistory.page--;fetchMaintenanceHistory()}break;case'maintenance-history-next':if(!maintenanceHistory.loading&&maintenanceHistory.data&&maintenanceHistory.page<maintenanceHistory.data.pages){maintenanceHistory.page++;fetchMaintenanceHistory()}break;case'new-request':newRequest();break;case'request-detail':await requestDetail(id);break;case'new-inspection':newInspection();break;case'inspection-detail':await inspectionDetail(id);break;case'sync':await syncPending();break}}catch(err){alertMsg(err.message,'bad')}});
document.addEventListener('input',e=>{if(e.target.id==='part-search'){let q=e.target.value.toLowerCase().trim();$$('[data-filter-part]').forEach(el=>el.hidden=!el.dataset.filterPart.includes(q))}});
window.addEventListener('online',()=>{load().then(()=>countPending())});window.addEventListener('offline',()=>setOnline(false));
if('serviceWorker' in navigator)navigator.serviceWorker.register('/service-worker.js').catch(()=>{});
countPending().then(load);
