/* Run: node --test tests/test_inventory_browser.mjs
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



test('Inventory idempotency browser regressions with isolated SQLite and profile', {timeout: 180000}, async t => {
  const temporary = mkdtempSync(join(tmpdir(), 'gefleet-inventory-browser-'));
  const python = process.env.GEFLEET_TEST_PYTHON || join(root, '.venv', 'Scripts', 'python.exe');
  const browserPath = process.env.GEFLEET_TEST_BROWSER || [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
  ].find(existsSync);
  assert.ok(existsSync(python), 'Set GEFLEET_TEST_PYTHON');
  assert.ok(browserPath && existsSync(browserPath), 'Set GEFLEET_TEST_BROWSER');
  const serverPort = await freePort(), debugPort = await freePort();
  const origin = 'http://127.0.0.1:' + serverPort;
  let serverLog = '', browser, cdp;
  const server = spawn(python, ['-B', '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(serverPort)], {
    cwd: root, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    env: {...process.env, GEFLEET_DB: join(temporary, 'inventory.db'), PYTHONDONTWRITEBYTECODE: '1'},
  });
  server.stdout.on('data', data => { serverLog += data; });
  server.stderr.on('data', data => { serverLog += data; });
  try {
    await poll(async () => (await fetch(origin + '/api/health')).ok, 'Temporary API did not start');
    async function post(path, body, key) {
      const response = await fetch(origin + path, {
        method: 'POST', headers: {'Content-Type': 'application/json', ...(key ? {'Idempotency-Key': key} : {})},
        body: JSON.stringify(body),
      });
      assert.equal(response.status, 200, await response.clone().text());
      return response.json();
    }
    await post('/api/vehicles', {plate: 'BROWSER-INV', name: 'Temporary vehicle', vehicle_type: 'LIVIANO'});
    const partId = (await post('/api/parts', {code: 'BROWSER-P01', name: 'Temporary part', unit: 'unidad'})).id;
    await post('/api/movements', {part_id: partId, movement_type: 'ENTRADA', quantity: '100', responsible: 'Fixture'}, 'browser-fixture-stock');
    const snapshot = async () => (await fetch(origin + '/api/bootstrap')).json();
    const vehicleId = (await snapshot()).vehicles[0].id;
    const stock = async () => (await snapshot()).parts[0].stock_milli;
    browser = spawn(browserPath, [
      '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
      '--remote-debugging-port=' + debugPort, '--user-data-dir=' + join(temporary, 'profile'), 'about:blank',
    ], {windowsHide: true, stdio: ['ignore', 'ignore', 'ignore']});
    const target = await poll(async () => {
      const targets = await (await fetch('http://127.0.0.1:' + debugPort + '/json/list')).json();
      return targets.find(target => target.type === 'page' && target.webSocketDebuggerUrl);
    }, 'Temporary browser did not start');
    cdp = await connectCDP(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable'); await cdp.send('Runtime.enable'); await cdp.send('Network.enable');
    await cdp.send('Page.navigate', {url: origin});
    const evaluate = cdp.evaluate;
    const wait = (expression, message) => poll(() => evaluate(expression), message);
    const click = selector => evaluate('document.querySelector(' + JSON.stringify(selector) + ').click()');
    const field = name => '#modal-form [name="' + name + '"]';
    const fill = (name, value) => evaluate('document.querySelector(' + JSON.stringify(field(name)) + ').value=' + JSON.stringify(String(value)));
    const journal = context => evaluate('JSON.parse(localStorage.getItem(' + JSON.stringify('ge_inventory_pending:' + context) + ') || "null")');
    const unknown = () => wait('document.querySelector("#inventory-status")?.textContent.includes("RESULTADO DESCONOCIDO")', 'Unknown result was not shown');
    const confirmed = () => wait('document.querySelector("#modal-backdrop").classList.contains("hidden") && document.querySelector("#alert").textContent.includes("CONFIRMADA")', 'Operation was not confirmed');
    async function ready() {
      await wait('typeof state!=="undefined" && state.parts.length===1 && document.querySelector("#screen").children.length>0', 'Inventory UI did not load');
    }
    async function reload() {
      const loads = cdp.events.filter(event => event.method === 'Page.loadEventFired').length;
      await cdp.send('Page.reload');
      await poll(() => cdp.events.filter(event => event.method === 'Page.loadEventFired').length > loads, 'Reload did not finish');
      await ready();
    }
    async function reset() {
      await reload();
      await evaluate('Object.keys(localStorage).filter(key=>key.startsWith("ge_inventory_pending:")).forEach(key=>localStorage.removeItem(key));navigate("repuestos")');
    }
    async function movement(kind='ENTRADA', quantity='1') {
      await evaluate('newMovement(' + JSON.stringify(kind) + ')');
      await fill('part_id', partId); await fill('quantity', quantity); await fill('responsible', 'Browser warehouse');
      await fill('reference', 'BROWSER'); await fill('notes', 'Actual operation notes');
    }
    async function request(quantity='4') {
      const id = (await post('/api/requests', {vehicle_id: vehicleId, requester: 'Browser requester', items: [{part_id: partId, quantity}]})).id;
      await post('/api/requests/' + id + '/approve', {approved_by: 'Browser approver'});
      await evaluate('load()');
      const detail = await (await fetch(origin + '/api/requests/' + id)).json();
      return {id, line: detail.lines[0].id};
    }
    async function delivery(record, quantity='1') {
      await evaluate('requestDetail(' + record.id + ')'); await click('#deliver-btn');
      await fill('delivered_by', 'Browser warehouse'); await fill('received_by', 'Browser receiver');
      await fill('line-' + record.line, quantity);
    }
    function requests(path) {
      return cdp.events.filter(event => event.method === 'Network.requestWillBeSent'
        && event.params.request.method === 'POST' && event.params.request.url === origin + path).map(event => event.params.request);
    }
    const headerKey = request => Object.entries(request.headers).find(([key])=>key.toLowerCase()==='idempotency-key')?.[1];
    async function fakeReply(path, mode) {
      await evaluate('(function(){const original=window.fetch;window.fetch=async function(path,options){' +
        'if(path===' + JSON.stringify(path) + '){' +
        (mode==='before' ? 'throw new TypeError("Network unavailable");' :
          'const response=await original.call(this,path,options);' +
          (mode==='lost' ? 'throw new TypeError("Response lost after commit");' :
            mode==='server' ? 'return new Response(JSON.stringify({detail:"Proxy failed after commit"}),{status:503,headers:{"Content-Type":"application/json"}});' :
              'return new Response("Invalid JSON",{status:200,headers:{"Content-Type":"application/json"}});')) +
        '}return original.call(this,path,options)}})()');
    }
    await ready();

    await t.test('key is stored before send; processing disables confirmation and double submit creates one entry', async () => {
      await reset(); await movement();
      await evaluate('(function(){const original=window.fetch;window.fetch=async function(path,options){if(path==="/api/movements"){window.sentOperation={key:options.headers["Idempotency-Key"],body:options.body,journal:JSON.parse(localStorage.getItem("ge_inventory_pending:movement:ENTRADA"))};await new Promise(resolve=>setTimeout(resolve,250))}return original.call(this,path,options)}})()');
      const before = await stock(), count = requests('/api/movements').length;
      await evaluate('document.querySelector("#modal-form").requestSubmit();document.querySelector("#modal-form").dispatchEvent(new Event("submit",{bubbles:true,cancelable:true}))');
      assert.equal(await evaluate('document.querySelector("#inventory-confirm").disabled'), true);
      await click('#modal-close');
      assert.equal(await evaluate('document.querySelector("#modal-backdrop").classList.contains("hidden")'), false);
      await confirmed();
      const sent = await evaluate('window.sentOperation');
      assert.equal(sent.key, sent.journal.key); assert.equal(sent.body, sent.journal.body);
      assert.equal(await stock(), before + 1000);
      assert.equal(requests('/api/movements').length - count, 1);
      assert.equal(await journal('movement:ENTRADA'), null);
    });

    await t.test('unsent transport failure persists identity and allows exact retry after close/reload', async () => {
      await reset(); await movement(); await fakeReply('/api/movements', 'before');
      const before = await stock(); await click('#inventory-confirm'); await unknown();
      const pending = await journal('movement:ENTRADA');
      assert.ok(pending.key); assert.equal(await stock(), before);
      await click('#modal-close'); await reload(); await evaluate('newMovement("ENTRADA")');
      assert.equal((await journal('movement:ENTRADA')).key, pending.key);
      assert.equal(await evaluate('document.querySelector(' + JSON.stringify(field('quantity')) + ').disabled'), true);
      await click('#inventory-confirm'); await confirmed();
      const last = requests('/api/movements').at(-1);
      assert.equal(headerKey(last), pending.key); assert.equal(last.postData, pending.body);
      assert.equal(await stock(), before + 1000);
    });

    for (const mode of ['lost', 'server', 'invalid']) {
      await t.test('committed entry with ' + mode + ' reply reuses key without adding stock on recovery', async () => {
        await reset(); await movement(); await fakeReply('/api/movements', mode);
        const before = await stock(), count = requests('/api/movements').length;
        await click('#inventory-confirm'); await unknown();
        const pending = await journal('movement:ENTRADA');
        assert.equal(await stock(), before + 1000);
        await click('#modal-close'); await reload(); await evaluate('newMovement("ENTRADA")');
        await click('#inventory-confirm'); await confirmed();
        assert.equal(await stock(), before + 1000);
        const sent = requests('/api/movements').slice(count);
        assert.equal(sent.length, 2);
        assert.deepEqual(sent.map(headerKey), [pending.key,pending.key]);
        assert.deepEqual(sent.map(record=>record.postData), [pending.body,pending.body]);
      });
    }

    await t.test('two legitimate identical entries generate different operation keys', async () => {
      await reset(); const before = await stock(), count = requests('/api/movements').length;
      for(let index=0;index<2;index++){await movement();await click('#inventory-confirm');await confirmed();}
      const sent = requests('/api/movements').slice(count);
      assert.equal(sent.length,2); assert.notEqual(headerKey(sent[0]),headerKey(sent[1]));
      assert.equal(sent[0].postData,sent[1].postData); assert.equal(await stock(),before+2000);
    });

    await t.test('insufficient stock is definitive; correcting quantity creates a new key', async () => {
      await reset(); await movement('SALIDA','999999');
      const before=await stock(), count=requests('/api/movements').length;
      await click('#inventory-confirm');
      await wait('document.querySelector("#inventory-status").textContent.includes("existencias")','Insufficient stock was not shown');
      assert.equal(await stock(),before); assert.equal(await journal('movement:SALIDA'),null);
      assert.equal(await evaluate('document.querySelector(' + JSON.stringify(field('quantity')) + ').disabled'),false);
      await fill('quantity','1'); await click('#inventory-confirm'); await confirmed();
      const sent=requests('/api/movements').slice(count);
      assert.equal(sent.length,2);assert.notEqual(headerKey(sent[0]),headerKey(sent[1]));
      assert.equal(await stock(),before-1000);
    });

    await t.test('partial delivery lost reply survives reload; a subsequent identical delivery gets another key', async () => {
      await reset();const record=await request(), path='/api/requests/'+record.id+'/deliver';
      await delivery(record);await fakeReply(path,'lost');
      const before=await stock(),count=requests(path).length;
      await click('#inventory-confirm');await unknown();
      const pending=await journal('delivery:'+record.id);
      assert.equal(await stock(),before-1000);
      await click('#modal-close');await reload();await evaluate('requestDetail('+record.id+')');await click('#deliver-btn');
      assert.equal(await evaluate('document.querySelector('+JSON.stringify(field('line-'+record.line))+').value'),'1');
      await click('#inventory-confirm');await confirmed();assert.equal(await stock(),before-1000);
      await delivery(record);await click('#inventory-confirm');await confirmed();
      const sent=requests(path).slice(count);
      assert.equal(sent.length,3);assert.equal(headerKey(sent[0]),pending.key);assert.equal(headerKey(sent[1]),pending.key);
      assert.notEqual(headerKey(sent[2]),pending.key);assert.equal(sent[0].postData,sent[2].postData);
      assert.equal(await stock(),before-2000);
      const detail=await(await fetch(origin+'/api/requests/'+record.id)).json();
      assert.equal(detail.lines[0].delivered_milli,2000);assert.equal(detail.status,'PARCIAL');
    });

    await t.test('completed request exposes recovery for a lost final delivery reply', async () => {
      await reset();const record=await request('1'),path='/api/requests/'+record.id+'/deliver';
      await delivery(record);await fakeReply(path,'lost');const before=await stock();
      await click('#inventory-confirm');await unknown();
      assert.equal((await(await fetch(origin+'/api/requests/'+record.id)).json()).status,'ENTREGADA');
      await click('#modal-close');await reload();await evaluate('requestDetail('+record.id+')');
      assert.match(await evaluate('document.querySelector("#deliver-btn").textContent'),/pendiente/i);
      await click('#deliver-btn');await click('#inventory-confirm');await confirmed();
      assert.equal(await stock(),before-1000);assert.equal(await journal('delivery:'+record.id),null);
    });

    await t.test('local journal quota failure prevents any network operation', async () => {
      await reset();await movement();const before=await stock(),count=requests('/api/movements').length;
      await evaluate('(function(){const original=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key.startsWith("ge_inventory_pending:"))throw new DOMException("Full storage","QuotaExceededError");return original.call(this,key,value)}})()');
      await click('#inventory-confirm');
      await wait('document.querySelector("#inventory-status").textContent.includes("No se")','Persistence failure was not shown');
      assert.equal(requests('/api/movements').length,count);assert.equal(await stock(),before);
    });

    await t.test('confirmed operation whose journal cannot be cleared only allows replay', async () => {
      await reset();await movement();const before=await stock();
      await evaluate('(function(){const original=Storage.prototype.removeItem;Storage.prototype.removeItem=function(key){if(key.startsWith("ge_inventory_pending:"))throw new DOMException("Cannot delete","SecurityError");return original.call(this,key)}})()');
      await click('#inventory-confirm');
      await wait('document.querySelector("#inventory-status")?.textContent.includes("OPERACIÓN CONFIRMADA")','Confirmation despite cleanup error was not shown');
      const pending=await journal('movement:ENTRADA');assert.equal(await stock(),before+1000);
      await click('#modal-close');await reload();await evaluate('newMovement("ENTRADA")');
      await click('#inventory-confirm');await confirmed();
      assert.equal(await stock(),before+1000);
      assert.equal(headerKey(requests('/api/movements').at(-1)),pending.key);
    });

    const errors=cdp.events.filter(event=>event.method==='Runtime.exceptionThrown');
    assert.deepEqual(errors.map(event=>event.params.exceptionDetails.text),[]);
  } catch(error) {
    error.message += '\nTemporary fixtures: '+temporary+'\nAPI log: '+serverLog.slice(-4000);
    throw error;
  } finally {
    if(cdp){await cdp.send('Browser.close').catch(()=>{});cdp.close();}
    if(browser&&browser.exitCode===null)browser.kill();
    if(server.exitCode===null)server.kill();
    t.diagnostic('Temporary SQLite and profile: '+temporary);
  }
});
