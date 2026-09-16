import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../robot-control-console.html', import.meta.url), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// Execute the actual standalone page with a controllable WebSocket handshake.
// No network or robot commands leave this harness.
function page({ storageDenied = false, savedConfig = null, socketSupported = true, randomDraw = null } = {}) {
  const elements = new Map();
  function element() {
    return {
      value: '', textContent: '', className: '', disabled: false, childNodes: [],
      classList: { toggle() {} }, setAttribute() {}, addEventListener() {},
      appendChild(child) { this.childNodes.push(child); },
      removeChild(child) { this.childNodes.splice(this.childNodes.indexOf(child), 1); },
      get firstChild() { return this.childNodes[0]; }
    };
  }
  for (const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
    const node = element();
    node.value = match[0].match(/\bvalue="([^"]*)"/)?.[1] ?? '';
    node.disabled = /\bdisabled\b/.test(match[0]);
    elements.set(match[1], node);
  }
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
  const storage = new Map(savedConfig ? [['robot_camerawork_yaw_pitch_v1', savedConfig]] : []);
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
  const math = Object.create(Math);
  if (randomDraw !== null) math.random = () => randomDraw;
  const context = vm.createContext({
    window, console,
    Math: math,
    WebSocket: socketSupported ? Socket : undefined,
    document: {
      getElementById: id => elements.get(id) ?? null,
      querySelectorAll: () => [],
      createElement: element
    }
  });
  // Expose the real planner inside this test VM only; the standalone page keeps it private.
  const testSource = source.replace(/\}\)\(\);\s*$/, `
    window.testYawTarget = adaptiveYawTarget;
    window.testSeparatedTarget = separateCameraworkTarget;
    window.testMoveTarget = async function(yaw, pitch, config, separate) {
      var previousWait = waitForPose;
      autoRunning = true;
      waitForPose = async function() { return "reached"; };
      try { return await moveAutoTarget(yaw, pitch, 3, "test", autoToken, config, separate); }
      finally { autoRunning = false; waitForPose = previousWait; }
    };
  })();`);
  vm.runInContext(testSource, context, { filename: 'robot-control-console.html' });
  const get = id => elements.get(id);
  return {
    get, sockets, timers, yawTarget: window.testYawTarget,
    separatedTarget: window.testSeparatedTarget, moveTarget: window.testMoveTarget,
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

test('adaptive turns inward within ten degrees of either boundary, including exactly ten', () => {
  const ui = page({ randomDraw: 0.99 });
  const config = { yawMin: -60, yawMax: 60 };
  for (const side of [-1, 1]) {
    for (const outwardRoom of [9, 10, 11]) {
      const current = side * (60 - outwardRoom);
      const target = ui.yawTarget(current, config);
      assert.equal(side * (target - current) < 0, outwardRoom <= 10);
      assert.ok(target >= config.yawMin && target <= config.yawMax);
    }
  }
});

test('adaptive opposite branch requires more than ten degrees of space', () => {
  const ui = page({ randomDraw: 0 });
  for (const side of [-1, 1]) {
    const config = side > 0 ? { yawMin: 20, yawMax: 60 } : { yawMin: -60, yawMax: -20 };
    for (const room of [10, 11]) {
      const current = side * (20 + room);
      const target = ui.yawTarget(current, config);
      assert.equal(side * (target - current) < 0, room > 10);
      assert.ok(target >= config.yawMin && target <= config.yawMax);
    }
  }
});

test('center direction uses the same ten-degree threshold', () => {
  for (const [yawMin, yawMax, draw, right] of [
    [-10, 60, 0, false], [-11, 60, 0, true],
    [-60, 10, 0.99, true], [-60, 11, 0.99, false]
  ]) {
    const target = page({ randomDraw: draw }).yawTarget(0, { yawMin, yawMax });
    assert.equal(target < 0, right);
    assert.ok(target >= yawMin && target <= yawMax);
  }
});

test('away from the edges, the 80 percent cutoff and movement fractions are unchanged', () => {
  const config = { yawMin: -60, yawMax: 60 };
  for (const side of [-1, 1]) {
    for (const draw of [0.79, 0.80]) {
      const current = side * 30;
      const target = page({ randomDraw: draw }).yawTarget(current, config);
      const inward = draw < 0.80;
      assert.equal(side * (target - current) < 0, inward);
      const distance = Math.abs(target - current);
      assert.ok(inward ? distance >= 54 && distance <= 81 : distance >= 3 && distance <= 9);
    }
  }
});

function assertSeparated(start, target, config) {
  const yawMid = (config.yawMin + config.yawMax) / 2;
  const pitchMid = (config.pitchMin + config.pitchMax) / 2;
  assert.ok((start[0] >= yawMid) !== (target[0] >= yawMid)
    || (start[1] >= pitchMid) !== (target[1] >= pitchMid));
  assert.ok(target[0] >= config.yawMin && target[0] <= config.yawMax);
  assert.ok(target[1] >= config.pitchMin && target[1] <= config.pitchMax);
}

test('random targets change quadrants in user ranges while preserving the selected yaw', () => {
  const ui = page();
  for (const [yawMin, yawMax, pitchMin, pitchMax] of [
    [-60, 60, -15, 15], [-10, 50, -40, 10], [20, 60, -60, -20], [-5, 5, 3, 4], [0, 1, 0, 1]
  ]) {
    const config = { yawMin, yawMax, pitchMin, pitchMax };
    for (let start of [[yawMin, pitchMin], [yawMax, pitchMax], [(yawMin+yawMax)/2, (pitchMin+pitchMax)/2]]) {
      for (let n = 0; n < 100; n += 1) {
        const yaw = ui.yawTarget(start[0], config);
        const pitch = Math.floor(pitchMin + Math.random() * (pitchMax - pitchMin + 1));
        const target = ui.separatedTarget(yaw, pitch, ...start, config);
        assert.equal(target[0], yaw);
        assertSeparated(start, target, config);
        start = target;
      }
    }
  }
});

test('crossing either center line is enough even for a small move', () => {
  const ui = page();
  const config = { yawMin: -60, yawMax: 60, pitchMin: -15, pitchMax: 15 };
  for (const proposed of [[1, 1], [1, -1], [-1, 0]]) {
    const target = ui.separatedTarget(...proposed, -1, -1, config);
    assert.deepEqual(Array.from(target), proposed);
    assertSeparated([-1, -1], target, config);
  }
});

test('the actual send uses fresh pose separation but keeps explicit anchors unchanged', async () => {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  const config = { yawMin: -60, yawMax: 60, pitchMin: -15, pitchMax: 15 };
  socket.message({ gimbal: { yaw: 30, pitch: 10 } });
  await ui.drainMessages();
  await ui.moveTarget(36, 12, config, true);
  const first = socket.sent.at(-1).gimbal_control;
  assert.equal(first.yaw_end, 36);
  assert.ok(first.pitch_end < 0);
  assertSeparated([30, 10], [first.yaw_end, first.pitch_end], config);

  // The latest heartbeat, rather than the previous command's target, controls the next check.
  socket.message({ gimbal: { yaw: 36, pitch: 12 } });
  await ui.drainMessages();
  await ui.moveTarget(36, 12, config, true);
  const second = socket.sent.at(-1).gimbal_control;
  assertSeparated([36, 12], [second.yaw_end, second.pitch_end], config);

  socket.message({ gimbal: { yaw: 5, pitch: 4 } });
  await ui.drainMessages();
  await ui.moveTarget(0, 0, config, false);
  const anchor = socket.sent.at(-1).gimbal_control;
  assert.equal(anchor.yaw_end, 0);
  assert.equal(anchor.pitch_end, 0);
});
