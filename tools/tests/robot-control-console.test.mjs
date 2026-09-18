import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../robot-control-console.html', import.meta.url), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// Execute the actual standalone page with a controllable WebSocket handshake.
// No network or robot commands leave this harness.
function page({ storageDenied = false, savedConfig = null, savedConfigV2 = null, socketSupported = true } = {}) {
  const elements = new Map();
  function element() {
    return {
      value: '', textContent: '', className: '', disabled: false, childNodes: [],
      classList: { toggle() {} }, setAttribute() {}, addEventListener() {},
      appendChild(child) { this.childNodes.push(child); },
      removeChild(child) { this.childNodes.splice(this.childNodes.indexOf(child), 1); },
      get firstChild() { return this.childNodes[0]; },
      set innerHTML(value) { assert.equal(value, ''); this.childNodes = []; }
    };
  }
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const node = element();
    node.value = match[0].match(/\bvalue="([^"]*)"/)?.[1] ?? '';
    node.disabled = /\bdisabled\b/.test(match[0]);
    elements.set(match[1], node);
  }
  elements.get('pointMode').value = '4';
  const choices = () => elements.get('pieceChoices').childNodes.map(label => label.childNodes[0]);
  let now = 1000;
  class Clock extends Date { static now() { return now; } }
  const timers = new Map();
  let nextTimer = 0;
  const sockets = [];
  class Socket {
    constructor(url) {
      if (!/^wss?:\/\//.test(url)) throw new SyntaxError('Invalid WebSocket URL');
      this.url = url;
      this.readyState = 0;
      this.sent = [];
      this.closeCalls = 0;
      sockets.push(this);
    }
    open() { this.readyState = 1; this.onopen?.(); }
    close() { this.closeCalls += 1; this.readyState = 2; }
    closed(code = 1000, wasClean = true, reason = '') {
      this.readyState = 3;
      this.onclose?.({ code, wasClean, reason });
    }
    send(text) {
      assert.equal(this.readyState, 1);
      this.sent.push(JSON.parse(text));
    }
    message(payload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
  }
  const storage = new Map();
  if (savedConfig !== null) storage.set('robot_camerawork_yaw_pitch_v1', savedConfig);
  if (savedConfigV2 !== null) storage.set('robot_camerawork_yaw_pitch_v2', savedConfigV2);
  const window = {
    addEventListener() {},
    setInterval() {},
    setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    get localStorage() {
      if (storageDenied) throw new Error('Storage is disabled');
      return {
        getItem: key => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key)
      };
    }
  };
  const context = vm.createContext({
    window, console,
    Date: Clock,
    WebSocket: socketSupported ? Socket : undefined,
    document: {
      getElementById: id => elements.get(id) ?? null,
      querySelectorAll: selector => {
        if (selector === '#pieceChoices input:checked') return choices().filter(input => input.checked);
        if (selector === '.auto-input, #pieceChoices input') return [...choices(), ...['yawMin', 'yawMax', 'pitchMin', 'pitchMax', 'speedMax', 'pointMode'].map(id => elements.get(id))];
        return [];
      },
      createElement: element,
      createTextNode: textContent => ({ textContent })
    }
  });
  vm.runInContext(source, context, { filename: 'robot-control-console.html' });
  const get = id => elements.get(id);
  return {
    get, sockets, timers, storage,
    choices,
    start: () => get('autoStart').onclick(),
    advance(ms) { now += ms; this.runTimers(); },
    async acknowledge(socket) {
      const command = socket.sent.at(-1).gimbal_control;
      for (let i = 0; i < 2; i++) {
        socket.message({ gimbal: { yaw: command.yaw_end, pitch: command.pitch_end } });
        await this.drainMessages();
        this.advance(100);
        await this.drainMessages();
      }
    },
    connect(url = 'ws://robot.local:8765') {
      get('url').value = url;
      get('btnConn').onclick();
      return sockets.at(-1);
    },
    runTimers() {
      for (const [id, timer] of [...timers]) {
        timers.delete(id);
        timer.fn();
      }
    },
    async drainMessages() { for (let n = 0; n < 8; n += 1) await Promise.resolve(); }
  };
}

test('a slow valid handshake is allowed to complete after the connection notice', () => {
  const ui = page();
  const socket = ui.connect();
  ui.runTimers();
  assert.equal(socket.closeCalls, 0, 'the page must not abort the handshake at five seconds');
  assert.equal(ui.get('btnDisc').disabled, false);
  socket.open();
  assert.equal(ui.get('connText').textContent, '已连接');
  assert.equal(ui.get('connErr').textContent, '');
  assert.equal(ui.get('manualSend').disabled, false);
  assert.deepEqual(socket.sent, [], 'connecting must not send motion commands');
});

test('canceling a stalled handshake permits an immediate retry without waiting for onclose', () => {
  const ui = page();
  const old = ui.connect();
  ui.get('btnDisc').onclick();
  assert.equal(old.closeCalls, 1);
  assert.equal(ui.get('btnConn').disabled, false);
  assert.equal(ui.get('btnDisc').disabled, true);
  assert.equal(ui.timers.size, 0);
  const next = ui.connect('ws://other-robot.local:8765');
  next.open();
  old.closed(1006, false);
  assert.equal(ui.get('connText').textContent, '已连接');
  assert.equal(ui.get('manualSend').disabled, false);
});

test('blocked storage and malformed saved settings do not prevent connection', () => {
  for (const options of [{ storageDenied: true }, { savedConfig: '{bad' }, { savedConfig: 'null' }]) {
    const ui = page(options);
    ui.connect().open();
    assert.equal(ui.get('connText').textContent, '已连接');
    assert.equal(ui.get('manualSend').disabled, false);
  }
});

test('one connection attempt cannot create multiple sockets', () => {
  const ui = page();
  ui.connect();
  ui.connect();
  assert.equal(ui.sockets.length, 1);
});

test('invalid addresses leave the connect button ready for a corrected address', () => {
  const ui = page();
  ui.connect('invalid address');
  assert.match(ui.get('connErr').textContent, /地址无效/);
  assert.equal(ui.get('btnConn').disabled, false);
  ui.connect().open();
  assert.equal(ui.get('connText').textContent, '已连接');
});

test('an abnormal close exposes the close code and allows reconnecting', () => {
  const ui = page();
  const socket = ui.connect();
  socket.closed(1006, false);
  assert.match(ui.get('connErr').textContent, /1006/);
  assert.equal(ui.get('btnConn').disabled, false);
  assert.equal(ui.get('manualSend').disabled, true);
  assert.equal(ui.timers.size, 0);
});

test('a clean disconnect without a status code is not reported as a connection failure', () => {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  ui.get('btnDisc').onclick();
  socket.closed(1005, true);
  assert.equal(ui.get('connErr').textContent, '');
  assert.equal(ui.get('btnConn').disabled, false);
  assert.equal(ui.get('manualSend').disabled, true);
});

test('normal heartbeat yaw and pitch update only the active connection', async () => {
  const ui = page();
  const old = ui.connect();
  old.open();
  old.message({ gimbal: { yaw: 12, pitch: -4 } });
  await ui.drainMessages();
  assert.equal(ui.get('actualYaw').textContent, '12.0°');
  assert.equal(ui.get('actualPitch').textContent, '-4.0°');
  old.closed();
  const next = ui.connect();
  next.open();
  old.message({ gimbal: { yaw: 80, pitch: 10 } });
  next.message({ gimbal: { yaw: 5, pitch: -2 } });
  await ui.drainMessages();
  assert.equal(ui.get('actualYaw').textContent, '5.0°');
  assert.equal(ui.get('actualPitch').textContent, '-2.0°');
});

test('a browser without WebSocket reports the missing capability', () => {
  const ui = page({ socketSupported: false });
  ui.connect();
  assert.match(ui.get('connErr').textContent, /浏览器.*WebSocket/);
  assert.equal(ui.get('btnConn').disabled, false);
});

async function readyPage() {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  socket.message({ gimbal: { yaw: 3, pitch: -2 } });
  await ui.drainMessages();
  return { ui, socket };
}

test('fixed program uses six cardinal pieces and adds four corner round trips in eight-point mode', async () => {
  for (const mode of ['4', '8']) {
    const { ui, socket } = await readyPage();
    ui.get('pointMode').value = mode;
    ui.get('pointMode').onchange();
    assert.equal(ui.choices().length, mode === '4' ? 6 : 10);
    const completion = ui.start();
    const targets = [[0, 0], [60, 0], [-60, 0], [0, 0], [0, -15], [0, 15], [0, 0]];
    if (mode === '8') targets.push([60, -15], [0, 0], [-60, -15], [0, 0], [-60, 15], [0, 0], [60, 15], [0, 0]);
    for (const [index, target] of targets.entries()) {
      assert.equal(socket.sent.length, index + 1, 'only one leg may be outstanding');
      const command = socket.sent[index].gimbal_control;
      assert.deepEqual([command.yaw_end, command.pitch_end], target);
      assert.equal(command.mode, 1);
      assert.equal(command.yaw_speed, 5);
      assert.equal(command.pitch_speed, 5);
      assert.deepEqual([command.zoom_start, command.zoom_speed, command.zoom_end], [1, 0, 1]);
      await ui.acknowledge(socket);
    }
    await completion;
    assert.equal(socket.sent.length, targets.length);
    assert.match(ui.get('autoStatus').textContent, /已完成.*原点/);
    assert.equal(ui.get('autoStart').disabled, false);
  }
});

test('a selected middle piece prepares its start and returns to the origin', async () => {
  const { ui, socket } = await readyPage();
  ui.choices().forEach(input => { input.checked = input.value === 'left-right'; });
  const completion = ui.start();
  for (const target of [[0, 0], [60, 0], [-60, 0], [0, 0]]) {
    const command = socket.sent.at(-1).gimbal_control;
    assert.deepEqual([command.yaw_end, command.pitch_end], target);
    await ui.acknowledge(socket);
  }
  await completion;
  assert.equal(socket.sent.length, 4);
});

test('invalid ranges, speed or empty selection cannot send a motion command', async () => {
  for (const [id, value] of [['yawMin', '0'], ['yawMax', '91'], ['pitchMin', '-61'], ['pitchMax', '16'], ['speedMax', '1'], ['yawMax', '2.5']]) {
    const { ui, socket } = await readyPage();
    ui.get(id).value = value;
    await ui.start();
    assert.equal(socket.sent.length, 0);
    assert.match(ui.get('autoStatus').textContent, /范围必须包含原点/);
  }
  const { ui, socket } = await readyPage();
  ui.choices().forEach(input => { input.checked = false; });
  await ui.start();
  assert.equal(socket.sent.length, 0);
  assert.match(ui.get('autoStatus').textContent, /至少选择一个/);
});

test('automatic movement requires fresh complete position feedback', async () => {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  for (const payload of [{ system: { status: 'ready' } }, { gimbal: { yaw: 0 } }]) {
    socket.message(payload);
    await ui.drainMessages();
    await ui.start();
    assert.equal(socket.sent.length, 0);
    assert.match(ui.get('autoStatus').textContent, /缺少新的完整位置心跳/);
  }
  socket.message({ gimbal: { yaw: 0, pitch: 0 } });
  await ui.drainMessages();
  ui.advance(5001);
  await ui.start();
  assert.equal(socket.sent.length, 0);
});

test('one arrival heartbeat or repeated polling cannot release the next leg', async () => {
  const { ui, socket } = await readyPage();
  const completion = ui.start();
  socket.message({ gimbal: { yaw: 0, pitch: 0 } });
  await ui.drainMessages();
  for (let i = 0; i < 3; i++) { ui.advance(100); await ui.drainMessages(); }
  assert.equal(socket.sent.length, 1);
  ui.advance(30000);
  await completion;
  assert.equal(socket.sent.length, 1, 'timeout stops the program rather than advancing');
  assert.match(ui.get('autoStatus').textContent, /未确认到位.*timeout/);
});

test('stopping or disconnecting cancels future legs without claiming hardware emergency stop', async () => {
  for (const action of ['autoStop', 'btnDisc']) {
    const { ui, socket } = await readyPage();
    const completion = ui.start();
    assert.equal(ui.get('manualSend').disabled, true);
    assert.equal(ui.choices().every(input => input.disabled), true);
    ui.get(action).onclick();
    ui.advance(100);
    await completion;
    assert.equal(socket.sent.length, 1);
    assert.match(ui.get('autoStatus').textContent, /可能继续完成|未确认硬件停止/);
  }
});
