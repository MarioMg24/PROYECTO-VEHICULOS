/* Run: node --test tests/test_maintenance_history_browser.mjs
 * Native CDP, isolated browser profile and TEMP SQLite; fleet.db is never opened.
 * Requires Node 22+, project Python dependencies and Chrome/Edge.
 */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {existsSync, mkdtempSync} from 'node:fs';
import {createServer} from 'node:net';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
async function freePort(){const server=createServer();await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)});const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port}
async function poll(callback,message,timeout=12000){const until=Date.now()+timeout;let error;while(Date.now()<until){try{const value=await callback();if(value)return value}catch(reason){error=reason}await delay(30)}throw new Error(message+(error?': '+error.message:''))}
async function connectCDP(url){
 const socket=new WebSocket(url);await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true})});
 let id=0;const pending=new Map(),events=[];
 socket.addEventListener('message',event=>{const message=JSON.parse(event.data);if(!message.id){events.push(message);return}const request=pending.get(message.id);if(!request)return;pending.delete(message.id);clearTimeout(request.timer);if(message.error)request.reject(new Error(JSON.stringify(message.error)));else request.resolve(message.result)});
 socket.addEventListener('close',()=>{for(const request of pending.values()){clearTimeout(request.timer);request.reject(new Error('Browser connection closed'))}pending.clear()});
 function send(method,params={}){return new Promise((resolve,reject)=>{const requestId=++id,timer=setTimeout(()=>{pending.delete(requestId);reject(new Error('CDP timeout: '+method))},12000);pending.set(requestId,{resolve,reject,timer});socket.send(JSON.stringify({id:requestId,method,params}))})}
 async function evaluate(expression){const result=await send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);return result.result.value}
 return {send,evaluate,events,close:()=>socket.close()};
}

test('Full maintenance history in the PWA with isolated SQLite', {timeout:240000},async t=>{
 const temporary=mkdtempSync(join(tmpdir(),'gefleet-maintenance-history-browser-'));
 const python=process.env.GEFLEET_TEST_PYTHON||join(root,'.venv','Scripts','python.exe');
 const browserPath=process.env.GEFLEET_TEST_BROWSER||['C:/Program Files/Google/Chrome/Application/chrome.exe','C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','/usr/bin/google-chrome','/usr/bin/chromium'].find(existsSync);
 assert.ok(existsSync(python),'Set GEFLEET_TEST_PYTHON');assert.ok(browserPath,'Set GEFLEET_TEST_BROWSER');
 const serverPort=await freePort(),debugPort=await freePort(),origin='http://127.0.0.1:'+serverPort;
 let serverLog='',browser,cdp;
 const server=spawn(python,['-B','-m','uvicorn','server:app','--host','127.0.0.1','--port',String(serverPort)],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],env:{...process.env,GEFLEET_DB:join(temporary,'history.db'),GEFLEET_ALLOW_CREATE_DB:'1',PYTHONDONTWRITEBYTECODE:'1'}});
 server.stdout.on('data',data=>{serverLog+=data});server.stderr.on('data',data=>{serverLog+=data});
 try{
  await poll(async()=>(await fetch(origin+'/api/health')).ok,'Temporary API did not start');
  async function request(path,body,method='POST'){const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert.equal(response.status,200,await response.clone().text());return response.json()}
  const snapshot=async()=>(await fetch(origin+'/api/bootstrap')).json();
  await request('/api/vehicles',{plate:'PDX-2511',name:'Chevrolet NLR',vehicle_type:'PESADO',odometer:118539});
  await request('/api/vehicles',{plate:'TEMP-OTHER',name:'Other temporary vehicle',vehicle_type:'LIVIANO',odometer:50000});
  const initial=await snapshot(),nlr=initial.vehicles.find(v=>v.plate==='PDX-2511'),other=initial.vehicles.find(v=>v.plate==='TEMP-OTHER');
  browser=spawn(browserPath,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--remote-debugging-port='+debugPort,'--user-data-dir='+join(temporary,'profile'),'about:blank'],{windowsHide:true,stdio:['ignore','ignore','ignore']});
  const target=await poll(async()=>(await(await fetch('http://127.0.0.1:'+debugPort+'/json/list')).json()).find(target=>target.type==='page'&&target.webSocketDebuggerUrl),'Temporary browser did not start');
  cdp=await connectCDP(target.webSocketDebuggerUrl);await cdp.send('Page.enable');await cdp.send('Runtime.enable');await cdp.send('Network.enable');await cdp.send('Page.navigate',{url:origin});
  const evaluate=cdp.evaluate,wait=(expression,message)=>poll(()=>evaluate(expression),message);
  const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
  const fill=(selector,value)=>evaluate('(()=>{const field=document.querySelector('+JSON.stringify(selector)+');field.value='+JSON.stringify(String(value))+';field.dispatchEvent(new Event("input",{bubbles:true}))})()');
  const modal=(name,value)=>fill('#modal-form [name="'+name+'"]',value);
  async function historyReady(){await wait('active==="mantenimientos"&&!maintenanceHistory.loading&&!!maintenanceHistory.data&&!maintenanceHistory.error','History did not load')}
  async function filters(values={}){for(const [key,value] of Object.entries({vehicle_id:'',date_from:'',date_to:'',maintenance_type:'',...values}))await fill('#maintenance-history-filters [name="history_'+key+'"]',value);await evaluate('document.querySelector("#maintenance-history-filters").requestSubmit()');await historyReady()}
  const history=()=>evaluate('maintenanceHistory.data');
  const screen=()=>evaluate('document.querySelector("#screen").textContent');
  async function close(){await click('#modal-close')}
  async function detail(id){await click('[data-action="maintenance-detail"][data-id="'+id+'"]');await wait('document.querySelector("#maintenance-detail-body.print-sheet")?.dataset.id==='+JSON.stringify(String(id)),'Detail did not load');return evaluate('document.querySelector("#maintenance-detail-body").textContent')}
  async function service(changes={}){return request('/api/maintenance',{vehicle_id:nlr.id,service_name:'Cambio de aceite',performed_on:'2026-09-30',odometer:118539,interval_km:4500,maintenance_type:'MPG',work_performed:'Cambio de aceite y filtros',responsible:'Samir Lobato',location:'Base Coca',notes:'Servicio registrado en base temporal',...changes})}
  function sentMaintenance(){return cdp.events.filter(event=>event.method==='Network.requestWillBeSent'&&event.params.request.url===origin+'/api/maintenance'&&event.params.request.method==='POST')}
  async function restoreFetch(){await evaluate('if(window._historyFetch){window.fetch=window._historyFetch;delete window._historyFetch}delete window._historyHeld;delete window._historyRelease');}
  await wait('typeof state!=="undefined"&&state.vehicles.length===2&&document.querySelector("#screen").children.length>0','PWA did not load');
  let sample,historic,corrective,legacy;

  await t.test('registers the NLR MPG example and calculates 118539 + 4500 = 123039',async()=>{
   await evaluate('navigate("mantenimientos")');await historyReady();await click('[data-action="new-maintenance"]');
   for(const [key,value] of Object.entries({vehicle_id:nlr.id,maintenance_type:'MPG',service_name:'Cambio de aceite',performed_on:'2026-09-30',odometer:118539,interval_km:4500,responsible:'Samir Lobato',location:'Base Coca',work_performed:'Cambio de aceite y filtros',notes:'Servicio registrado en base temporal'}))await modal(key,value);
   assert.equal(await evaluate('document.querySelector("[name=next_due_preview]").readOnly'),true);
   assert.equal((await evaluate('document.querySelector("[name=next_due_preview]").value')).replace(/\D/g,''),'123039');
   await click('#modal-form button[type=submit]');await wait('document.querySelector("#modal-backdrop").classList.contains("hidden")','Maintenance was not saved');
   await wait('state.maintenance.some(m=>m.vehicle_id==='+nlr.id+')','Saved maintenance not reflected');await historyReady();
   sample=(await history()).items.find(m=>m.vehicle_id===nlr.id);
   assert.equal(sample.performed_on,'2026-09-30');assert.equal(sample.odometer,118539);assert.equal(sample.next_due_km,123039);assert.equal(sample.maintenance_type,'MPG');
   assert.equal(sample.responsible,'Samir Lobato');assert.equal(sample.location,'Base Coca');assert.equal(sample.work_performed,'Cambio de aceite y filtros');
   assert.match(await screen(),/PDX-2511/);assert.match(await screen(),/30\/09\/2026/);
  });

  await t.test('detail shows every technical field and preserves observations separately',async()=>{
   const text=await detail(sample.id);for(const expected of ['Chevrolet NLR','PDX-2511','30/09/2026','Preventivo (MPG)','Samir Lobato','Base Coca','Cambio de aceite y filtros','Servicio registrado en base temporal'])assert.ok(text.includes(expected),expected);
   assert.match(text.replace(/\D/g,''),/118539/);assert.match(text.replace(/\D/g,''),/123039/);await close();
  });

  await t.test('vehicle card opens only that vehicle history',async()=>{
   await service({vehicle_id:other.id,performed_on:'2026-10-11',odometer:50000,service_name:'Frenos',interval_km:8000});await evaluate('load()');await evaluate('navigate("vehiculos")');
   await click('[data-action="vehicle-detail"][data-id="'+nlr.id+'"]');await click('[data-action="vehicle-maintenance-history"]');await historyReady();
   assert.equal(await evaluate('document.querySelector("[name=history_vehicle_id]").value'),String(nlr.id));assert.ok((await history()).items.every(m=>m.vehicle_id===nlr.id));
   assert.equal(await evaluate('document.querySelector("#modal-backdrop").classList.contains("hidden")'),true);
  });

  await t.test('historical insertion keeps chronological order, current plan and current odometer',async()=>{
   historic=await service({performed_on:'2026-08-20',odometer:100000,responsible:'Historical mechanic'});await evaluate('load()');await historyReady();
   const data=await history();assert.equal(data.items[0].id,sample.id);assert.equal(data.latest.id,sample.id);assert.equal(data.plans[0].id,sample.id);assert.equal(data.plans[0].next_due_km,123039);
   assert.equal((await snapshot()).vehicles.find(v=>v.id===nlr.id).odometer,118539);
   assert.deepEqual(data.items.map(m=>m.performed_on),['2026-09-30','2026-08-20']);
  });

  await t.test('date and type filters leave latest service and preventive plan independent',async()=>{
   corrective=await service({performed_on:'2026-10-10',odometer:118600,maintenance_type:'CORRECTIVO',interval_km:null,service_name:'Cambio de aceite',work_performed:'Reparación de fuga'});await evaluate('load()');await historyReady();
   await filters({vehicle_id:nlr.id,date_from:'2026-09-01',date_to:'2026-09-30',maintenance_type:'MPG'});
   let data=await history();assert.equal(data.total,1);assert.equal(data.items[0].id,sample.id);assert.equal(data.latest.id,corrective.id);assert.equal(data.plans[0].id,sample.id);
   assert.match(await evaluate('document.querySelector("#maintenance-latest").textContent'),/10\/10\/2026/);
   await filters({vehicle_id:nlr.id,maintenance_type:'CORRECTIVO'});data=await history();assert.equal(data.total,1);assert.equal(data.items[0].id,corrective.id);
   await filters({vehicle_id:other.id});assert.equal((await history()).total,1);assert.ok((await history()).items.every(m=>m.vehicle_id===other.id));
  });

  await t.test('invalid filter dates show a clear message without sending a history request',async()=>{
   await filters({vehicle_id:nlr.id});await fill('[name=history_date_from]','2026-10-01');await fill('[name=history_date_to]','2026-09-01');
   const before=cdp.events.filter(e=>e.method==='Network.requestWillBeSent'&&e.params.request.url.startsWith(origin+'/api/maintenance?')).length;
   await evaluate('document.querySelector("#maintenance-history-filters").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}))');
   await wait('document.querySelector("#alert").textContent.includes("fecha desde")','Invalid range message missing');
   assert.equal(cdp.events.filter(e=>e.method==='Network.requestWillBeSent'&&e.params.request.url.startsWith(origin+'/api/maintenance?')).length,before);
   await filters({vehicle_id:nlr.id});
  });

  await t.test('corrective registration rejects an interval and preserves the existing preventive plan',async()=>{
   await click('[data-action=new-maintenance]');await modal('vehicle_id',nlr.id);await modal('maintenance_type','CORRECTIVO');await modal('service_name','Cambio de aceite');await modal('performed_on','2026-10-12');await modal('odometer',118600);await modal('interval_km',4500);
   const count=sentMaintenance().length;await click('#modal-form button[type=submit]');await wait('document.querySelector("#alert").textContent.includes("correctivo")','Corrective interval warning missing');
   assert.equal(sentMaintenance().length,count);assert.equal(await evaluate('document.querySelector("[name=next_due_preview]").value'),'');
   await modal('interval_km','');await click('#modal-form button[type=submit]');await wait('document.querySelector("#modal-backdrop").classList.contains("hidden")','Corrective maintenance was not saved');await historyReady();
   assert.equal((await history()).plans.find(m=>m.service_name==='Cambio de aceite').id,sample.id);
  });

  await t.test('legacy records retain their original notes and can be filtered explicitly',async()=>{
   legacy=await request('/api/maintenance',{vehicle_id:nlr.id,service_name:'Servicio anterior',performed_on:'2024-01-01',odometer:80000,notes:'Trabajo y notas heredados'});await evaluate('load()');await historyReady();await filters({vehicle_id:nlr.id,maintenance_type:'LEGACY'});
   const data=await history();assert.equal(data.total,1);assert.equal(data.items[0].id,legacy.id);
   const text=await detail(legacy.id);assert.match(text,/Trabajo y notas heredados/);assert.match(text,/Sin clasificar/);assert.match(text,/No registrado/);await close();
  });

  await t.test('pagination reaches all 205 archive rows and opens a detail omitted from bootstrap',async()=>{
   let firstArchive;
   for(let index=0;index<205;index++){const row=await service({service_name:'Archivo técnico '+String(index).padStart(3,'0'),performed_on:'2025-01-01',odometer:85000+index,interval_km:null,maintenance_type:'CORRECTIVO',work_performed:'Trabajo de archivo '+index});if(index===0)firstArchive=row}
   await evaluate('load()');await historyReady();await filters({vehicle_id:nlr.id});
   const snap=await snapshot();assert.equal(snap.maintenance.length,200);assert.equal(snap.maintenance.some(m=>m.id===sample.id),false);assert.equal(snap.maintenance.some(m=>m.id===firstArchive.id),false);
   assert.ok((await history()).total>200);assert.equal((await history()).plans.find(m=>m.service_name==='Cambio de aceite').id,sample.id);
   assert.match(await detail(sample.id),/Samir Lobato/);await close();
   const total=(await history()).total,ids=new Set((await history()).items.map(m=>m.id));
   while((await history()).page<(await history()).pages){await click('[data-action="maintenance-history-next"]');await historyReady();for(const row of (await history()).items)assert.equal(ids.has(row.id)?'duplicate':ids.add(row.id)&&'added','added')}
   assert.equal(ids.size,total);assert.match(await evaluate('document.querySelector("#maintenance-history-count").textContent'),new RegExp('de '+total+' registros'));
   assert.equal(await evaluate('document.querySelector("[data-action=maintenance-history-next]").disabled'),true);
   assert.ok((await history()).items.some(m=>m.id===firstArchive.id));assert.match(await detail(firstArchive.id),/Trabajo de archivo 0/);await close();
   const last=(await history()).page;await click('[data-action="maintenance-history-prev"]');await historyReady();assert.equal((await history()).page,last-1);
  });

  await t.test('history failure is explicit and retry restores the full result',async()=>{
   await evaluate('window._historyFetch=window.fetch;window.fetch=(url,options)=>String(url).startsWith("/api/maintenance?")?Promise.reject(new TypeError("Simulated offline")):window._historyFetch(url,options);navigate("mantenimientos")');
   await wait('!!maintenanceHistory.error&&!maintenanceHistory.loading','History error not shown');assert.match(await screen(),/historial completo|conexión/);
   await restoreFetch();await click('[data-action="maintenance-history-retry"]');await historyReady();assert.ok((await history()).total>200);
  });

  await t.test('a delayed previous query cannot replace a newer vehicle filter',async()=>{
   await evaluate('window._historyFetch=window.fetch;window._historyHeld=false;window.fetch=(url,options)=>{if(String(url).startsWith("/api/maintenance?")&&!window._historyHeld){window._historyHeld=true;return new Promise(resolve=>window._historyFetch(url,options).then(response=>{window._historyRelease=()=>resolve(response)}))}return window._historyFetch(url,options)};navigate("mantenimientos")');
   await wait('typeof window._historyRelease==="function"','Delayed query was not captured');await filters({vehicle_id:other.id});assert.equal((await history()).total,1);
   await evaluate('window._historyRelease()');await delay(80);assert.equal((await history()).total,1);assert.ok((await history()).items.every(m=>m.vehicle_id===other.id));await restoreFetch();
  });

  await t.test('a delayed history response cannot replace another screen',async()=>{
   await evaluate('window._historyFetch=window.fetch;window._historyHeld=false;window.fetch=(url,options)=>{if(String(url).startsWith("/api/maintenance?")&&!window._historyHeld){window._historyHeld=true;return new Promise(resolve=>window._historyFetch(url,options).then(response=>{window._historyRelease=()=>resolve(response)}))}return window._historyFetch(url,options)};navigate("mantenimientos")');
   await wait('typeof window._historyRelease==="function"','Delayed query was not captured');await evaluate('navigate("vehiculos");window._historyRelease()');await delay(80);
   assert.equal(await evaluate('active'),'vehiculos');assert.equal(await evaluate('document.querySelector("#maintenance-history-results")'),null);await restoreFetch();await evaluate('navigate("mantenimientos")');await historyReady();
  });

  await t.test('closing a pending detail does not reopen it when the response arrives',async()=>{
   await filters({vehicle_id:nlr.id});await evaluate('window._historyFetch=window.fetch;window.fetch=(url,options)=>String(url)==='+JSON.stringify('/api/maintenance/'+sample.id)+'?new Promise(resolve=>window._historyFetch(url,options).then(response=>{window._historyRelease=()=>resolve(response)})):window._historyFetch(url,options)');
   await click('[data-action="maintenance-detail"][data-id="'+sample.id+'"]');await wait('typeof window._historyRelease==="function"','Delayed detail was not captured');await close();await evaluate('window._historyRelease()');await delay(80);
   assert.equal(await evaluate('document.querySelector("#modal-backdrop").classList.contains("hidden")'),true);await restoreFetch();
  });

  await t.test('long technical notes are preserved, escaped and never truncated silently',async()=>{
   const work='<img src=x onerror="window.maintenanceXss=true">'+ 'W'.repeat(3952),notes='Observaciones\n'+'N'.repeat(3986);assert.equal(work.length,4000);assert.equal(notes.length,4000);
   await click('[data-action=new-maintenance]');for(const [key,value] of Object.entries({vehicle_id:nlr.id,service_name:'Informe técnico extenso',performed_on:'2026-10-13',odometer:118600,maintenance_type:'CORRECTIVO',work_performed:work,notes}))await modal(key,value);
   await modal('notes',notes+'N');const count=sentMaintenance().length;await evaluate('document.querySelector("#modal-form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}))');await wait('document.querySelector("#alert").textContent.includes("4000")','Text length error missing');assert.equal(sentMaintenance().length,count);
   await modal('notes',notes);await click('#modal-form button[type=submit]');await wait('document.querySelector("#modal-backdrop").classList.contains("hidden")','Long text service was not saved');await wait('state.maintenance.some(m=>m.service_name==="Informe técnico extenso")','Saved long text was not reflected in the refreshed snapshot');await historyReady();
   const row=(await history()).items.find(m=>m.service_name==='Informe técnico extenso');assert.equal(row.work_performed,work);assert.equal(row.notes,notes);const text=await detail(row.id);assert.ok(text.includes(work));assert.ok(text.includes(notes));assert.equal(await evaluate('!!window.maintenanceXss||!!document.querySelector("#maintenance-detail-body img")'),false);await close();
  });

  await t.test('refreshing history uses an odometer changed in another session without an old bootstrap reading',async()=>{
   const reading=await evaluate('state.vehicles.find(v=>v.id==='+nlr.id+').odometer');assert.equal(reading,118600);
   await request('/api/vehicles/'+nlr.id,{odometer:122539,expected_odometer:reading},'PATCH');await filters({vehicle_id:nlr.id,date_from:'2026-09-01',date_to:'2026-09-30',maintenance_type:'MPG'});
   assert.equal(await evaluate('state.vehicles.find(v=>v.id==='+nlr.id+').odometer'),reading);
   const data=await history();assert.equal(data.plans[0].current_odometer,122539);assert.equal(data.plans[0].id,sample.id);
   const badge=await evaluate('document.querySelector("#maintenance-plan-list .badge").outerHTML');assert.match(badge,/warn/);assert.match(badge,/500 km/);
  });

  await t.test('maintenance filters, table and form remain usable on a narrow screen',async()=>{
   await cdp.send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await evaluate('navigate("mantenimientos")');await historyReady();await filters({vehicle_id:nlr.id});
   assert.ok(await evaluate('document.documentElement.scrollWidth<=window.innerWidth'),'Page overflowed horizontally');assert.equal(await evaluate('getComputedStyle(document.querySelector("#maintenance-history-filters .fields")).gridTemplateColumns.split(" ").length'),1);
   const long=(await history()).items.find(m=>m.service_name==='Informe técnico extenso');await detail(long.id);assert.ok(await evaluate('document.querySelector(".modal-body").scrollWidth<=document.querySelector(".modal-body").clientWidth'),'Long technical detail overflowed horizontally');await close();
   await click('[data-action=new-maintenance]');assert.ok(await evaluate('document.querySelector(".modal").getBoundingClientRect().width<=window.innerWidth'),'Maintenance form overflowed');await close();await cdp.send('Emulation.clearDeviceMetricsOverride');
  });

  assert.deepEqual(cdp.events.filter(e=>e.method==='Runtime.exceptionThrown').map(e=>e.params.exceptionDetails.text),[]);
 }catch(error){error.message+='\nTemporary fixtures: '+temporary+'\nAPI log: '+serverLog.slice(-4000);throw error}
 finally{if(cdp){await cdp.send('Browser.close').catch(()=>{});cdp.close()}if(browser&&browser.exitCode===null)browser.kill();if(server.exitCode===null)server.kill();t.diagnostic('Temporary SQLite and profile: '+temporary)}
});
