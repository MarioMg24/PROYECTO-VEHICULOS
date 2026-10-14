/* Run: node --test tests/test_checklist_browser.mjs
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

test('Checklist browser regressions with isolated SQLite and profile', {timeout: 180000}, async t => {
  const temporary = mkdtempSync(join(tmpdir(), 'gefleet-checklist-browser-'));
  const python = process.env.GEFLEET_TEST_PYTHON || join(root, '.venv', 'Scripts', 'python.exe');
  const browserPath = process.env.GEFLEET_TEST_BROWSER || [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ].find(existsSync);
  assert.ok(existsSync(python), 'Set GEFLEET_TEST_PYTHON to the project Python executable');
  assert.ok(browserPath && existsSync(browserPath), 'Set GEFLEET_TEST_BROWSER to Chrome/Edge');
  const serverPort = await freePort();
  const debugPort = await freePort();
  const origin = `http://127.0.0.1:${serverPort}`;
  let serverLog = '';
  let browserLog = '';
  let browser;
  let cdp;
  const server = spawn(python, ['-B', '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(serverPort)], {
    cwd: root, windowsHide: true,
    env: {...process.env, GEFLEET_DB: join(temporary, 'checklist.db'), PYTHONDONTWRITEBYTECODE: '1'},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stdout.on('data', data => { serverLog += data; });
  server.stderr.on('data', data => { serverLog += data; });
  try {
    await poll(async () => (await fetch(`${origin}/api/health`)).ok, 'Temporary API did not start');
    for (const plate of ['CHECK-BROWSER-01', 'CHECK-BROWSER-02']) {
      const response = await fetch(`${origin}/api/vehicles`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({plate, name: plate, vehicle_type: 'LIVIANO', odometer: 1000}),
      });
      assert.equal(response.status, 200);
    }
    const initial = await (await fetch(`${origin}/api/bootstrap`)).json();
    const vehicleId = initial.vehicles[1].id;
    const allKeys = initial.template.flatMap((group, gi) => group.items.map((text, ii) => `${gi}-${ii}`));
    assert.equal(allKeys.length, 37);
    browser = spawn(browserPath, [
      '--headless=new', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
      `--remote-debugging-port=${debugPort}`, `--user-data-dir=${join(temporary, 'browser-profile')}`,
      'about:blank',
    ], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
    browser.stderr.on('data', data => { browserLog += data; });
    const target = await poll(async () => {
      const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
      return targets.find(target => target.type === 'page' && target.webSocketDebuggerUrl);
    }, 'Isolated browser did not start');
    cdp = await connectCDP(target.webSocketDebuggerUrl);
    await cdp.send('Page.enable');
    await cdp.send('Runtime.enable');
    await cdp.send('Network.enable');
    await cdp.send('Page.navigate', {url: origin});

    const evaluate = cdp.evaluate;
    const wait = (expression, message) => poll(() => evaluate(expression), message);
    const value = selector => evaluate(`document.querySelector(${JSON.stringify(selector)})?.value`);
    const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
    const fill = (selector, text) => evaluate(`(() => {
      const field = document.querySelector(${JSON.stringify(selector)});
      field.value = ${JSON.stringify(String(text))};
      field.dispatchEvent(new Event('input', {bubbles: true}));
      field.dispatchEvent(new Event('change', {bubbles: true}));
    })()`);
    const step = index => click(`[data-check-section="${index}"]`);
    const draft = () => evaluate("JSON.parse(localStorage.getItem('ge_check_draft') || 'null')");
    const pending = () => evaluate('dbAll()');
    const snapshot = async () => (await fetch(`${origin}/api/bootstrap`)).json();
    const posts = () => cdp.events.filter(event => event.method === 'Network.requestWillBeSent'
      && event.params.request.method === 'POST' && event.params.request.url.endsWith('/api/inspections')).length;
    const online = () => cdp.send('Network.emulateNetworkConditions', {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    const offline = () => cdp.send('Network.emulateNetworkConditions', {
      offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0,
    });
    async function ready() {
      await wait("typeof state !== 'undefined' && state.template.length === 5 && document.querySelector('[data-action=new-inspection]')", 'Checklist interface did not load');
    }
    async function reload() {
      const loads = cdp.events.filter(event => event.method === 'Page.loadEventFired').length;
      await cdp.send('Page.reload');
      await poll(() => cdp.events.filter(event => event.method === 'Page.loadEventFired').length > loads, 'Reload did not finish');
      await ready();
    }
    async function reset() {
      await online();
      await evaluate(`(async () => {
        checkDraft = null;
        localStorage.removeItem('ge_check_draft');
        for (const record of await dbAll()) await removePending(record.client_uuid);
        await countPending();
      })()`);
      await reload();
      await click('[data-action=new-inspection]');
    }
    async function header() {
      await fill('#modal-content [name=vehicle_id]', vehicleId);
      await fill('#modal-content [name=date]', '2026-10-11');
      await fill('#modal-content [name=inspector]', 'Conductor de prueba á');
      await fill('#modal-content [name=odometer]', '12345');
    }
    async function complete(mixed = false) {
      await header();
      for (let gi = 0; gi < initial.template.length; gi++) {
        await step(gi + 1);
        for (let ii = 0; ii < initial.template[gi].items.length; ii++) {
          const choice = mixed && gi === 0 ? ['BIEN', 'MAL', 'NA'][ii % 3] : 'BIEN';
          await click(`input[name="answer-${gi}-${ii}"][value="${choice}"]`);
          if (choice === 'MAL') await fill(`textarea[data-key="${gi}-${ii}"]`, `Falla ${ii}: revisión pendiente`);
        }
      }
      await step(6);
    }
    async function submitAndWait() {
      await click('#check-next');
      await wait("document.querySelector('#modal-backdrop').classList.contains('hidden')", 'Checklist was not finalized');
    }
    await ready();

    await t.test('first step survives next, previous and section tabs', async () => {
      await reset(); await header();
      await click('#check-next'); await click('#check-prev');
      assert.equal(await value('[name=vehicle_id]'), String(vehicleId));
      assert.equal(await value('[name=date]'), '2026-10-11');
      assert.equal(await value('[name=inspector]'), 'Conductor de prueba á');
      assert.equal(await value('[name=odometer]'), '12345');
      await step(3); await step(0);
      assert.equal(await value('[name=vehicle_id]'), String(vehicleId));
      assert.equal(await value('[name=date]'), '2026-10-11');
    });

    await t.test('BIEN MAL NA, per-item notes and general observations survive navigation', async () => {
      await reset(); await complete(true);
      const observations = 'Golpe en puerta\nÚltima línea: á < > & "';
      await fill('[name=observations]', observations);
      await step(1);
      for (const [ii, choice] of ['BIEN', 'MAL', 'NA'].entries()) {
        assert.equal(await evaluate(`document.querySelector('input[name="answer-0-${ii}"]:checked').value`), choice);
      }
      assert.equal(await value('textarea[data-key="0-1"]'), 'Falla 1: revisión pendiente');
      await step(6);
      assert.equal(await value('[name=observations]'), observations);
    });

    await t.test('X flushes latest edit immediately and reopening restores the whole draft', async () => {
      await reset(); await complete(true);
      await fill('[name=observations]', 'Cambio justo antes de cerrar X');
      await click('#modal-close');
      assert.equal((await draft()).observations, 'Cambio justo antes de cerrar X');
      await click('[data-action=new-inspection]');
      assert.equal(await value('[name=vehicle_id]'), String(vehicleId));
      assert.equal(await value('[name=date]'), '2026-10-11');
      assert.equal(await value('[name=inspector]'), 'Conductor de prueba á');
      assert.equal(await value('[name=odometer]'), '12345');
      await step(1);
      assert.equal(await value('textarea[data-key="0-1"]'), 'Falla 1: revisión pendiente');
      await step(6);
      assert.equal(await value('[name=observations]'), 'Cambio justo antes de cerrar X');
    });

    await t.test('backdrop close preserves edits too', async () => {
      await reset(); await header();
      await fill('[name=inspector]', 'Cierre por el fondo');
      await click('#modal-backdrop');
      await click('[data-action=new-inspection]');
      assert.equal(await value('[name=inspector]'), 'Cierre por el fondo');
    });

    await t.test('reload before autosave delay recovers all fields, answers and notes', async () => {
      await reset(); await complete(true);
      await fill('[name=observations]', 'Recuperar tras recarga inmediata 🚙');
      await reload();
      await click('[data-action=new-inspection]');
      assert.equal(await value('[name=vehicle_id]'), String(vehicleId));
      assert.equal(await value('[name=odometer]'), '12345');
      await step(1);
      assert.equal(await evaluate("document.querySelector('input[name=answer-0-2]:checked').value"), 'NA');
      assert.equal(await value('textarea[data-key="0-1"]'), 'Falla 1: revisión pendiente');
      await step(6);
      assert.equal(await value('[name=observations]'), 'Recuperar tras recarga inmediata 🚙');
    });

    await t.test('autosave batches rapid edits and persists the final value', async () => {
      await reset();
      await evaluate(`(() => {
        window.draftWrites = 0;
        const original = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, text) {
          if (key === 'ge_check_draft') window.draftWrites++;
          return original.call(this, key, text);
        };
        const field = document.querySelector('[name=inspector]');
        for (let i = 0; i < 100; i++) {
          field.value = 'Edición ' + i;
          field.dispatchEvent(new Event('input', {bubbles: true}));
        }
      })()`);
      await wait("JSON.parse(localStorage.getItem('ge_check_draft') || '{}').inspector === 'Edición 99'", 'Autosave did not persist final input');
      const writes = await evaluate('window.draftWrites');
      assert.ok(writes >= 1 && writes <= 2, `Expected batched writes, got ${writes}`);
    });

    await t.test('empty kilometraje stays empty across navigation and cannot finalize', async () => {
      await reset(); await complete(); await step(0);
      await fill('[name=odometer]', ''); await step(1); await step(0);
      assert.equal(await value('[name=odometer]'), '');
      await step(6);
      const count = posts(); await click('#check-next');
      assert.equal(posts(), count);
      assert.equal((await pending()).length, 0);
      assert.equal(await evaluate("document.querySelector('#modal-backdrop').classList.contains('hidden')"), false);
    });

    await t.test('incomplete answers never become finalized inspections online or offline', async () => {
      for (const disconnected of [false, true]) {
        await reset(); await header(); await step(6);
        if (disconnected) await offline();
        const before = (await snapshot()).inspections.length;
        const count = posts(); await click('#check-next'); await delay(80);
        assert.equal(posts(), count);
        assert.equal((await pending()).length, 0);
        assert.equal((await snapshot()).inspections.length, before);
        assert.match(await evaluate("document.querySelector('#alert').textContent"), /Faltan/);
      }
    });

    await t.test('blank driver and MAL without a note cannot finalize', async () => {
      await reset(); await complete(); await step(0);
      await fill('[name=inspector]', '   '); await step(6);
      let count = posts(); await click('#check-next');
      assert.equal(posts(), count);
      await step(0); await fill('[name=inspector]', 'Conductor');
      await step(1); await click('input[name="answer-0-0"][value=MAL]');
      await step(6); count = posts(); await click('#check-next');
      assert.equal(posts(), count);
      assert.equal((await pending()).length, 0);
      assert.match(await evaluate("document.querySelector('#alert').textContent"), /observaciones/);
    });

    await t.test('combined notes above 2500 are blocked without truncating the draft', async () => {
      for (const disconnected of [false, true]) {
        await reset(); await complete(true);
        const text = 'Observación larga 🚙\n' + 'á'.repeat(2501);
        await fill('[name=observations]', text);
        if (disconnected) await offline();
        const count = posts(); await click('#check-next');
        assert.equal(posts(), count);
        assert.equal((await pending()).length, 0);
        assert.match(await evaluate("document.querySelector('#alert').textContent"), /2500/);
        await click('#modal-close'); await click('[data-action=new-inspection]');
        await step(6);
        assert.equal(await value('[name=observations]'), text);
        assert.equal((await draft()).observations, text);
      }
    });

    await t.test('exactly 2500 combined characters persist, duplicate clicks do not resend, timer stays cleared', async () => {
      await reset(); await complete(); await step(1);
      await click('input[name="answer-0-0"][value=MAL]');
      await fill('textarea[data-key="0-0"]', 'Falla'); await step(6);
      const suffix = `${initial.template[0].group} / ${initial.template[0].items[0]}: Falla`;
      const general = 'á'.repeat(2500 - Array.from(suffix).length - 1);
      await fill('[name=observations]', general);
      const uuid = (await draft()).client_uuid;
      const count = posts();
      await evaluate("document.querySelector('#check-next').click(); document.querySelector('#check-next')?.click()");
      await wait("document.querySelector('#modal-backdrop').classList.contains('hidden')", 'Boundary inspection did not finalize');
      await delay(350);
      assert.equal(posts() - count, 1);
      assert.equal(await draft(), null);
      const saved = (await snapshot()).inspections.find(record => record.client_uuid === uuid);
      assert.ok(saved);
      const detail = await (await fetch(`${origin}/api/inspections/${saved.id}`)).json();
      assert.equal(detail.observations, `${general}\n${suffix}`);
      assert.equal(Array.from(detail.observations).length, 2500);
      assert.equal(detail.answers['0-0'], 'MAL');
    });

    await t.test('2500 Unicode characters are counted consistently by browser and Python', async () => {
      await reset(); await complete();
      const text = '🚙'.repeat(2500);
      await fill('[name=observations]', text);
      const uuid = (await draft()).client_uuid;
      await submitAndWait();
      const saved = (await snapshot()).inspections.find(record => record.client_uuid === uuid);
      assert.ok(saved);
      assert.equal(saved.observations, text);
    });

    await t.test('storage quota failure reports an error and preserves the open draft', async () => {
      await reset(); await header();
      await evaluate(`window.originalDraftSetItem = Storage.prototype.setItem;
        Storage.prototype.setItem = function(key, value) {
          if (key === 'ge_check_draft') throw new DOMException('Quota exceeded', 'QuotaExceededError');
          return window.originalDraftSetItem.call(this, key, value);
        };`);
      await fill('[name=inspector]', 'Cambio que no cabe en almacenamiento');
      await click('#modal-close');
      assert.equal(await evaluate("document.querySelector('#modal-backdrop').classList.contains('hidden')"), false);
      assert.equal(await value('[name=inspector]'), 'Cambio que no cabe en almacenamiento');
      assert.match(await evaluate("document.querySelector('#alert').textContent"), /borrador|almacenamiento/i);
      await evaluate('Storage.prototype.setItem = window.originalDraftSetItem');
      await click('#modal-close');
      assert.equal((await draft()).inspector, 'Cambio que no cabe en almacenamiento');
    });

    await t.test('HTTP validation error is never queued even after an offline state', async () => {
      await reset(); await complete(); await fill('[name=observations]', 'Conservar al rechazar');
      await evaluate(`window.originalChecklistFetch = window.fetch;
        window.fetch = function(url, options) {
          if (url === '/api/inspections') return Promise.resolve(new Response(JSON.stringify({detail: 'Validación del servidor'}), {status: 422, headers: {'Content-Type': 'application/json'}}));
          return window.originalChecklistFetch.call(this, url, options);
        };
        state.online = false;`);
      await click('#check-next');
      await wait("document.querySelector('#alert').textContent.includes('Validación del servidor')", 'Validation error was not shown');
      assert.equal((await pending()).length, 0);
      assert.equal((await draft()).observations, 'Conservar al rechazar');
      assert.equal(await evaluate("document.querySelector('#modal-backdrop').classList.contains('hidden')"), false);
    });

    await t.test('failure to store offline inspection keeps its draft available', async () => {
      await reset(); await complete(); await fill('[name=observations]', 'No perder si IndexedDB falla');
      await evaluate("queueInspection = async () => { throw new DOMException('Sin espacio en IndexedDB', 'QuotaExceededError'); }");
      await offline(); await click('#check-next');
      await wait("document.querySelector('#alert').textContent.includes('IndexedDB')", 'Offline storage error was not shown');
      assert.equal((await draft()).observations, 'No perder si IndexedDB falla');
      assert.equal(await evaluate("document.querySelector('#modal-backdrop').classList.contains('hidden')"), false);
      assert.equal((await pending()).length, 0);
    });

    await t.test('existing IndexedDB offline queue survives reload and synchronizes unchanged', async () => {
      await reset(); await complete(true);
      const text = 'Offline íntegro\nCon evidencia de falla 🚙';
      await fill('[name=observations]', text);
      const uuid = (await draft()).client_uuid;
      await offline(); await submitAndWait();
      const queued = await pending();
      assert.equal(queued.length, 1);
      assert.equal(queued[0].client_uuid, uuid);
      assert.equal(Object.keys(queued[0].answers).length, 37);
      assert.equal(queued[0].answers['0-2'], 'NA');
      assert.ok(queued[0].observations.startsWith(text + '\n'));
      assert.equal(await draft(), null);
      await online(); await reload();
      assert.equal((await pending())[0].client_uuid, uuid);
      await click('[data-nav=inspecciones]'); await click('[data-action=sync]');
      await wait("Number(localStorage.getItem('ge_pending_count')) === 0", 'Offline inspection did not synchronize');
      assert.equal((await pending()).length, 0);
      const saved = (await snapshot()).inspections.find(record => record.client_uuid === uuid);
      assert.ok(saved);
      const detail = await (await fetch(`${origin}/api/inspections/${saved.id}`)).json();
      assert.deepEqual(detail.answers, queued[0].answers);
      assert.equal(detail.observations, queued[0].observations);
      assert.equal(detail.vehicle_id, vehicleId);
      assert.equal(detail.date, '2026-10-11');
      assert.equal(detail.inspector, 'Conductor de prueba á');
      assert.equal(detail.odometer, 12345);
    });

    await t.test('another modal cannot overwrite an existing checklist draft', async () => {
      await reset(); await header(); await click('#modal-close');
      const before = await draft();
      await click('[data-nav=vehiculos]'); await click('[data-action=new-vehicle]');
      await fill('[name=name]', 'Otro formulario');
      await click('#modal-close');
      assert.deepEqual(await draft(), before);
    });

    const exceptions = cdp.events.filter(event => event.method === 'Runtime.exceptionThrown');
    assert.deepEqual(exceptions.map(event => event.params.exceptionDetails.text), [], 'Unexpected uncaught browser exceptions');
  } catch (error) {
    error.message += `\nTemporary fixtures: ${temporary}\nAPI log: ${serverLog.slice(-4000)}\nBrowser log: ${browserLog.slice(-1000)}`;
    throw error;
  } finally {
    if (cdp) {
      await cdp.send('Browser.close').catch(() => {});
      cdp.close();
    }
    if (browser && browser.exitCode === null) browser.kill();
    if (server.exitCode === null) server.kill();
    t.diagnostic(`Temporary SQLite and browser profile: ${temporary}`);
  }
});
