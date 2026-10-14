/* Run: node --test tests/test_mileage_browser.mjs
 * Requires Node 22+, project Python dependencies and an installed Chrome/Edge.
 * Uses native CDP/WebSocket, a temporary SQLite DB and a separate browser profile.
 * Override executables with GEFLEET_TEST_PYTHON / GEFLEET_TEST_BROWSER if needed.
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

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');

async function freePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function poll(callback, message, timeout = 10000) {
  const until = Date.now() + timeout;
  let error;
  while (Date.now() < until) {
    try {
      const value = await callback();
      if (value) return value;
    } catch (reason) { error = reason; }
    await delay(30);
  }
  throw new Error(`${message}${error ? ': ' + error.message : ''}`);
}

async function connectCDP(url) {
  const socket = new WebSocket(url);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, {once: true});
    socket.addEventListener('error', reject, {once: true});
  });
  let id = 0;
  const pending = new Map();
  const events = [];
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id) { events.push(message); return; }
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (message.error) request.reject(new Error(JSON.stringify(message.error)));
    else request.resolve(message.result);
  });
  socket.addEventListener('close', () => {
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error('Browser connection closed'));
    }
    pending.clear();
  });
  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const requestId = ++id;
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`CDP timeout: ${method}`));
      }, 10000);
      pending.set(requestId, {resolve, reject, timer});
      socket.send(JSON.stringify({id: requestId, method, params}));
    });
  }
  async function evaluate(expression) {
    const result = await send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    }
    return result.result.value;
  }
  return {send, evaluate, events, close: () => socket.close()};
}





test('Mileage and maintenance browser regressions with isolated SQLite and profile', {timeout: 180000}, async t => {
  const temporary=mkdtempSync(join(tmpdir(),'gefleet-mileage-browser-'));
  const python=process.env.GEFLEET_TEST_PYTHON||join(root,'.venv','Scripts','python.exe');
  const browserPath=process.env.GEFLEET_TEST_BROWSER||[
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome','/usr/bin/chromium',
  ].find(existsSync);
  assert.ok(existsSync(python),'Set GEFLEET_TEST_PYTHON');assert.ok(browserPath,'Set GEFLEET_TEST_BROWSER');
  const serverPort=await freePort(),debugPort=await freePort(),origin='http://127.0.0.1:'+serverPort;
  let serverLog='',browser,cdp,fixtureNumber=0;
  const server=spawn(python,['-B','-m','uvicorn','server:app','--host','127.0.0.1','--port',String(serverPort)],{
    cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe'],
    env:{...process.env,GEFLEET_DB:join(temporary,'mileage.db'),PYTHONDONTWRITEBYTECODE:'1'},
  });
  server.stdout.on('data',d=>{serverLog+=d});server.stderr.on('data',d=>{serverLog+=d});
  try {
    await poll(async()=> (await fetch(origin+'/api/health')).ok,'Temporary API did not start');
    async function request(path,body,method='POST'){
      const response=await fetch(origin+path,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      assert.equal(response.status,200,await response.clone().text());return response.json();
    }
    const snapshot=async()=> (await fetch(origin+'/api/bootstrap')).json();
    const vehicle=async id=>(await snapshot()).vehicles.find(v=>v.id===id);
    async function fixture(){
      const plate='KM-BROWSER-'+(++fixtureNumber);
      await request('/api/vehicles',{plate,name:'Temporary vehicle',vehicle_type:'LIVIANO',brand:'Toyota',model:'Hilux',notes:'Keep notes',odometer:118539});
      const record=(await snapshot()).vehicles.find(v=>v.plate===plate);
      if(cdp)await cdp.evaluate('load()');
      return record;
    }
    await fixture();
    browser=spawn(browserPath,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking',
      '--remote-debugging-port='+debugPort,'--user-data-dir='+join(temporary,'profile'),'about:blank'],
      {windowsHide:true,stdio:['ignore','ignore','ignore']});
    const target=await poll(async()=>{
      const list=await(await fetch('http://127.0.0.1:'+debugPort+'/json/list')).json();
      return list.find(t=>t.type==='page'&&t.webSocketDebuggerUrl);
    },'Temporary browser did not start');
    cdp=await connectCDP(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');await cdp.send('Runtime.enable');await cdp.send('Network.enable');
    await cdp.send('Page.navigate',{url:origin});
    const evaluate=cdp.evaluate;
    const wait=(expression,message)=>poll(()=>evaluate(expression),message);
    const click=selector=>evaluate('document.querySelector('+JSON.stringify(selector)+').click()');
    const fill=(name,value)=>evaluate('document.querySelector('+JSON.stringify('#modal-form [name="'+name+'"]')+').value='+JSON.stringify(String(value)));
    async function saved(){
      await wait('document.querySelector("#modal-backdrop").classList.contains("hidden")','Form was not saved');
      const fresh=await snapshot(),stamp=JSON.stringify([fresh.vehicles,fresh.maintenance[0]?.id??null,fresh.maintenance_plans]);
      await wait('JSON.stringify([state.vehicles,state.maintenance[0]?.id??null,state.maintenance_plans])==='+JSON.stringify(stamp),'Saved operation was not reflected in the refreshed UI');
    }
    async function ready(){await wait('typeof state!=="undefined"&&state.vehicles.length>0&&state.template.length===5&&document.querySelector("#screen").children.length>0','PWA did not load')}
    async function reset(){
      const loads=cdp.events.filter(e=>e.method==='Page.loadEventFired').length;
      await cdp.send('Page.reload');await poll(()=>cdp.events.filter(e=>e.method==='Page.loadEventFired').length>loads,'Reload did not finish');
      await ready();await evaluate('navigate("vehiculos")');
    }
    const edit=id=>click('[data-action="edit-vehicle"][data-id="'+id+'"]');
    const confirm=()=>click('#modal-form button[type=submit]');
    function sent(path,method){return cdp.events.filter(e=>e.method==='Network.requestWillBeSent'&&e.params.request.url===origin+path&&e.params.request.method===method).map(e=>e.params.request)}
    async function service(record,changes={}){
      return request('/api/maintenance',{vehicle_id:record.id,service_name:'Cambio de aceite',performed_on:'2026-10-12',odometer:118539,interval_km:4500,...changes});
    }
    async function serviceUI(record,changes={}){
      const data={service_name:'Cambio de aceite',performed_on:'2026-10-12',odometer:118539,interval_km:4500,...changes};
      await evaluate('navigate("mantenimientos")');await click('[data-action=new-maintenance]');
      await fill('vehicle_id',record.id);
      for(const [name,value] of Object.entries(data))await fill(name,value===null?'':value);
      await confirm();await saved();
    }
    const due=id=>evaluate('latestDue().filter(m=>m.vehicle_id==='+id+')');
    async function badge(record,serviceName='Cambio de aceite'){
      return evaluate('(()=>{const row=Array.from(document.querySelectorAll(".list-item")).find(x=>x.querySelector("strong")?.textContent==='+JSON.stringify(record.plate+' · '+serviceName)+');const badge=row?.querySelector(".badge");return {text:badge?.textContent,classes:badge?.className,row:row?.textContent}})()');
    }
    await ready();

    await t.test('editing name sends only changed fields and preserves the odometer',async()=>{
      await reset();const record=await fixture();await edit(record.id);await fill('name','Changed name');
      const path='/api/vehicles/'+record.id,count=sent(path,'PATCH').length;
      await confirm();await saved();
      assert.deepEqual(JSON.parse(sent(path,'PATCH').slice(count)[0].postData),{name:'Changed name'});
      assert.deepEqual(await vehicle(record.id),{...record,name:'Changed name'});
    });

    await t.test('editing plate and brand keeps model, notes and kilometraje',async()=>{
      await reset();const record=await fixture();await edit(record.id);
      await fill('plate',record.plate+'A');await fill('brand','New brand');await confirm();await saved();
      assert.deepEqual(await vehicle(record.id),{...record,plate:record.plate+'A',brand:'New brand'});
    });

    await t.test('normal decrease is blocked and points to an explicit correction',async()=>{
      await reset();const record=await fixture();await edit(record.id);await fill('odometer','118538');
      const path='/api/vehicles/'+record.id,count=sent(path,'PATCH').length;
      await confirm();await wait('document.querySelector("#alert").textContent.toLowerCase().includes("corregir")','Correction action guidance missing');
      assert.equal(sent(path,'PATCH').length,count);assert.deepEqual(await vehicle(record.id),record);
    });

    await t.test('an old edit of name cannot replace a newer service reading',async()=>{
      await reset();const record=await fixture();await edit(record.id);
      await service(record,{performed_on:'2026-10-13',odometer:119000});
      await fill('name','Metadata from old form');await confirm();await saved();
      assert.equal((await vehicle(record.id)).odometer,119000);
      assert.equal((await vehicle(record.id)).name,'Metadata from old form');
    });

    await t.test('an odometer edit captures its original reading and rejects concurrent changes',async()=>{
      await reset();const record=await fixture();await edit(record.id);
      await service(record,{performed_on:'2026-10-13',odometer:119000});
      await fill('odometer','118600');await fill('name','Should not overwrite');
      await confirm();await wait('document.querySelector("#alert").textContent.includes("camb")','Stale reading conflict missing');
      const data=JSON.parse(sent('/api/vehicles/'+record.id,'PATCH').at(-1).postData);
      assert.equal(data.expected_odometer,118539);
      assert.equal((await vehicle(record.id)).odometer,119000);
      assert.equal((await vehicle(record.id)).name,record.name);
    });

    await t.test('service UI calculates 118539 + 4500; a historical service does not replace it',async()=>{
      await reset();const record=await fixture();await serviceUI(record);
      const initial=(await due(record.id))[0];
      assert.equal(initial.next_due_km,123039);assert.equal(initial.kmLeft,4500);
      await serviceUI(record,{performed_on:'2025-04-01',odometer:100000});
      const plans=await due(record.id);
      assert.equal(plans.length,1);assert.equal(plans[0].id,initial.id);
      assert.equal(plans[0].next_due_km,123039);assert.equal((await vehicle(record.id)).odometer,118539);
    });

    await t.test('remaining kilometraje shows normal at 501, warning at 500 and overdue at zero',async()=>{
      await reset();const record=await fixture();await service(record);
      let expected=record.odometer;
      for(const [reading,left,kind] of [[122538,501,'ok'],[122539,500,'warn'],[123039,0,'bad'],[123040,-1,'bad']]){
        await request('/api/vehicles/'+record.id,{odometer:reading,expected_odometer:expected},'PATCH');expected=reading;
        await evaluate('load()');await evaluate('navigate("mantenimientos")');
        assert.equal((await due(record.id))[0].kmLeft,left);
        const status=await badge(record);assert.ok(status.classes.split(' ').includes(kind),JSON.stringify(status));
        if(left<=0)assert.equal(status.text,'Vencido');
      }
    });

    await t.test('oil and brakes keep independent plans; same-name corrective work never resets oil',async()=>{
      await reset();const record=await fixture();await service(record);
      await service(record,{service_name:'Frenos',odometer:118000,interval_km:8000});
      const before=await snapshot(),oil=before.maintenance_plans.find(m=>m.vehicle_id===record.id&&m.service_key==='cambio de aceite');
      await serviceUI(record,{performed_on:'2026-10-13',odometer:119000,interval_km:null});
      const plans=await due(record.id);
      assert.equal(plans.length,2);
      assert.equal(plans.find(m=>m.service_key==='cambio de aceite').id,oil.id);
      assert.equal(plans.find(m=>m.service_key==='frenos').next_due_km,126000);
    });

    await t.test('more than 200 later history rows cannot hide a current plan in the UI',async()=>{
      await reset();const record=await fixture(),other=await fixture();const original=await service(record);
      for(let index=0;index<205;index++)await service(other,{service_name:'Correctivo sin plan',performed_on:'2026-10-13',interval_km:null});
      const data=await snapshot();assert.equal(data.maintenance.length,200);
      assert.equal(data.maintenance.some(m=>m.id===original.id),false);
      await evaluate('load()');await evaluate('navigate("mantenimientos")');
      assert.equal((await due(record.id))[0].id,original.id);
      assert.ok((await badge(record)).row);
    });

    await t.test('explicit correction requires justification and appends history without changing previous service',async()=>{
      await reset();const record=await fixture();await service(record);await evaluate('load()');await evaluate('navigate("vehiculos")');
      const before=await snapshot(),old=before.maintenance.find(m=>m.vehicle_id===record.id);
      await click('[data-action=correct-odometer][data-id="'+record.id+'"]');
      await fill('odometer','110000');await fill('corrected_on','2026-10-13');
      const path='/api/vehicles/'+record.id+'/odometer-correction',count=sent(path,'POST').length;
      await evaluate('document.querySelector("#modal-form").dispatchEvent(new Event("submit",{cancelable:true,bubbles:true}))');
      await wait('document.querySelector("#alert").textContent.toLowerCase().includes("motivo")','Missing correction justification not shown');
      assert.equal(sent(path,'POST').length,count);
      await fill('reason','Lectura anterior digitada incorrectamente');await fill('responsible','Responsable de prueba');
      await confirm();await saved();
      assert.equal((await vehicle(record.id)).odometer,110000);
      const after=await snapshot();
      assert.deepEqual(after.maintenance.find(m=>m.id===old.id),old);
      const correction=after.maintenance.find(m=>m.vehicle_id===record.id&&m.id!==old.id);
      assert.equal(correction.interval_km,null);assert.equal(correction.next_due_km,null);
      assert.match(correction.notes,/118539/);assert.match(correction.notes,/110000/);
      assert.match(correction.notes,/Responsable de prueba/);assert.match(correction.notes,/digitada/);
      assert.equal((await due(record.id))[0].id,old.id);
    });

    await t.test('a correction opened before another reading cannot overwrite it',async()=>{
      await reset();const record=await fixture();
      await click('[data-action=correct-odometer][data-id="'+record.id+'"]');
      await service(record,{performed_on:'2026-10-13',odometer:119000});
      await fill('odometer','110000');await fill('corrected_on','2026-10-13');
      await fill('reason','Revisión de lectura');await fill('responsible','Responsable de prueba');
      await confirm();await wait('document.querySelector("#alert").textContent.includes("camb")','Stale correction conflict missing');
      const data=await snapshot();
      assert.equal((await vehicle(record.id)).odometer,119000);
      assert.equal(data.maintenance.filter(m=>m.vehicle_id===record.id).length,1);
    });

    await t.test('empty/out-of-range kilometraje and invalid date cannot be sent from forms',async()=>{
      await reset();const record=await fixture();await edit(record.id);
      for(const reading of ['',-1,10000001]){
        await fill('odometer',reading);const count=sent('/api/vehicles/'+record.id,'PATCH').length;
        await evaluate('document.querySelector("#modal-form").dispatchEvent(new Event("submit",{cancelable:true,bubbles:true}))');await delay(30);
        assert.equal(sent('/api/vehicles/'+record.id,'PATCH').length,count);assert.equal((await vehicle(record.id)).odometer,118539);
      }
      await click('#modal-close');await evaluate('newMaintenance()');
      await fill('vehicle_id',record.id);await fill('service_name','Cambio de aceite');
      await fill('performed_on','2026-02-30');await fill('odometer',118539);await fill('interval_km',4500);
      const count=sent('/api/maintenance','POST').length;
      await evaluate('document.querySelector("#modal-form").dispatchEvent(new Event("submit",{cancelable:true,bubbles:true}))');await delay(30);
      assert.equal(sent('/api/maintenance','POST').length,count);
    });

    await t.test('legacy cached plans warn about incomplete history and discard invalid ranges',async()=>{
      await reset();const record=await fixture();await service(record);await evaluate('load()');
      await evaluate('(function(){delete state.maintenance_plans;const valid=state.maintenance.find(m=>m.vehicle_id==='+record.id+');state.maintenance=[valid,{...valid,id:99991,service_name:"Invalid mileage",odometer:10000001},{...valid,id:99992,service_name:"Invalid interval",interval_km:1000001}];navigate("mantenimientos")})()');
      assert.equal((await due(record.id)).length,1);
      assert.match(await evaluate('document.querySelector("#screen").textContent'),/copia local|incompletos/i);
    });

    assert.deepEqual(cdp.events.filter(e=>e.method==='Runtime.exceptionThrown').map(e=>e.params.exceptionDetails.text),[]);
  } catch(error){
    error.message+='\nTemporary fixtures: '+temporary+'\nAPI log: '+serverLog.slice(-4000);throw error;
  } finally {
    if(cdp){await cdp.send('Browser.close').catch(()=>{});cdp.close();}
    if(browser&&browser.exitCode===null)browser.kill();if(server.exitCode===null)server.kill();
    t.diagnostic('Temporary SQLite and profile: '+temporary);
  }
});
