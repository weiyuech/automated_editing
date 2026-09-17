import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../robot-control-console.html', import.meta.url), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];

// Execute the actual standalone page with a controllable WebSocket handshake.
// No network or robot commands leave this harness.
function page({ storageDenied = false, savedConfig = null, savedConfigV2 = null, socketSupported = true, randomDraw = null } = {}) {
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
    window.testChooseQuadrant = chooseNextQuadrant;
    window.testQuadrantPose = quadrantPose;
    window.testSampleCycle = sampleCameraworkCycle;
    window.testCreateSchedule = createCameraworkSchedule;
    window.testAdvanceSchedule = advanceCameraworkSchedule;
    window.testBeginAnchorHold = beginAnchorHold;
    window.testPrepareArrival = function(schedule, quadrant, config) {
      autoRunning = true;
      autoBusy = true;
      autoToken = 7;
      activeAutoConfig = config;
      autoSchedule = schedule;
      lastAutoQuadrant = quadrant;
      autoStationary = false;
      refreshButtons();
    };
    window.testArrivalState = function() {
      return {
        running: autoRunning,
        busy: autoBusy,
        token: autoToken,
        stationary: autoStationary,
        phase: autoSchedule && autoSchedule.phase,
        deadline: autoSchedule && autoSchedule.deadline,
        serial: autoSchedule && autoSchedule.serial,
        quadrant: lastAutoQuadrant,
        hasConfig: Boolean(activeAutoConfig)
      };
    };
    window.testMoveTarget = async function(yaw, pitch, config) {
      var previousWait = waitForPose;
      var waitCalls = 0;
      autoRunning = true;
      waitForPose = async function() { waitCalls += 1; return "reached"; };
      try {
        var result = await moveAutoTarget(yaw, pitch, 3, "test", autoToken, config);
        return { result:result, waitCalls:waitCalls };
      }
      finally { autoRunning = false; waitForPose = previousWait; }
    };
  })();`);
  vm.runInContext(testSource, context, { filename: 'robot-control-console.html' });
  const get = id => elements.get(id);
  return {
    get, sockets, timers, storage,
    chooseQuadrant: window.testChooseQuadrant,
    quadrantPose: window.testQuadrantPose,
    sampleCycle: window.testSampleCycle,
    createSchedule: window.testCreateSchedule,
    advanceSchedule: window.testAdvanceSchedule,
    beginAnchorHold: window.testBeginAnchorHold,
    prepareArrival: window.testPrepareArrival,
    arrivalState: window.testArrivalState,
    moveTarget: window.testMoveTarget,
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

const legacyConfig = {
  anchorYaw: 3, anchorPitch: -2,
  yawMin: -60, yawMax: 60,
  pitchMin: -15, pitchMax: 15,
  speedMin: 2, speedMax: 5
};

test('legacy v1 settings migrate without carrying obsolete direction policy', () => {
  const ui = page({ savedConfig: JSON.stringify(legacyConfig) });
  assert.equal(ui.get('anchorYaw').value, 3);
  assert.equal(ui.get('anchorPitch').value, -2);
  assert.equal(ui.get('anchorTimePercent').value, 20);
  assert.equal(ui.get('anchorDwellSeconds').value, 5);
  assert.equal(ui.get('configState').textContent, '已保存');
  const migrated = JSON.parse(ui.storage.get('robot_camerawork_yaw_pitch_v2'));
  assert.deepEqual(migrated, { ...legacyConfig, anchorTimePercent: 20, anchorDwellSeconds: 5 });
});

test('a malformed v2 record safely falls back to a valid v1 record', () => {
  const ui = page({ savedConfig: JSON.stringify(legacyConfig), savedConfigV2: '{bad' });
  assert.equal(ui.get('anchorYaw').value, 3);
  assert.equal(ui.get('anchorTimePercent').value, 20);
  assert.doesNotThrow(() => JSON.parse(ui.storage.get('robot_camerawork_yaw_pitch_v2')));
});

test('the first quadrant uses all four choices and every next choice excludes only the previous one', () => {
  assert.equal(page({ randomDraw: 0 }).chooseQuadrant(null), 0);
  assert.equal(page({ randomDraw: 0.999999 }).chooseQuadrant(null), 3);
  for (const previous of [0, 1, 2, 3]) {
    const seen = new Set();
    for (const draw of [0, 0.34, 0.67, 0.999999]) {
      const next = page({ randomDraw: draw }).chooseQuadrant(previous);
      assert.notEqual(next, previous);
      seen.add(next);
    }
    assert.equal(seen.size, 3, `all remaining quadrants must be reachable after ${previous}`);
  }
  const ui = page({ randomDraw: 0.999999 });
  assert.equal(ui.chooseQuadrant(0), 3);
  assert.equal(ui.chooseQuadrant(3), 2);
  assert.notEqual(ui.chooseQuadrant(0), 0);
});

function expectedQuadrant(pose, config) {
  const yawHigh = pose[0] >= (config.yawMin + config.yawMax) / 2 ? 1 : 0;
  const pitchHigh = pose[1] >= (config.pitchMin + config.pitchMax) / 2 ? 2 : 0;
  return yawHigh + pitchHigh;
}

test('quadrant targets stay inside all four halves, including one-degree and asymmetric ranges', () => {
  for (const draw of [0, 0.5, 0.999999]) {
    const ui = page({ randomDraw: draw });
    for (const config of [
      { yawMin: -60, yawMax: 60, pitchMin: -15, pitchMax: 15 },
      { yawMin: -10, yawMax: 50, pitchMin: -40, pitchMax: 10 },
      { yawMin: 20, yawMax: 60, pitchMin: -60, pitchMax: -20 },
      { yawMin: 0, yawMax: 1, pitchMin: 3, pitchMax: 4 }
    ]) {
      for (const quadrant of [0, 1, 2, 3]) {
        const pose = Array.from(ui.quadrantPose(quadrant, config));
        assert.equal(expectedQuadrant(pose, config), quadrant);
        assert.ok(pose[0] >= config.yawMin && pose[0] <= config.yawMax);
        assert.ok(pose[1] >= config.pitchMin && pose[1] <= config.pitchMax);
      }
    }
  }
});

test('anchor dwell jitter stays internal and the timed cycle matches the requested share', () => {
  const low = page({ randomDraw: 0 }).sampleCycle({ anchorDwellSeconds: 5, anchorTimePercent: 20 });
  assert.equal(low.anchorMs, 3500);
  assert.equal(low.roamMs, 14000);
  const high = page({ randomDraw: 1 }).sampleCycle({ anchorDwellSeconds: 5, anchorTimePercent: 20 });
  assert.equal(high.anchorMs, 6500);
  assert.equal(high.roamMs, 26000);
  assert.equal(low.anchorMs / (low.anchorMs + low.roamMs), 0.2);
  assert.doesNotMatch(html, /锚点[^<]*(?:\u00b1|30%)/, 'the hidden jitter must not become UI copy');
});

test('automatic UI describes only camerawork and anchor behavior', () => {
  const panel = html.match(/<section class="tab-panel" id="autoPanel"[\s\S]*?<\/section>/)[0];
  assert.match(panel, /每次回到锚点后停留（秒）/);
  assert.match(panel, /回到锚点后才开始计时；返回过程不占用停留时间/);
  assert.doesNotMatch(panel, /四区域|四个水平|区域\s*\d|象限|(?:\u00b1|±)\s*30%/);
  assert.doesNotMatch(source, /· 区域 /, 'internal target buckets must not leak into live status');
});

test('zero percent is quadrants-only and one hundred percent is anchor-only', () => {
  const noAnchorUi = page({ randomDraw: 0.5 });
  const noAnchor = noAnchorUi.createSchedule({ anchorDwellSeconds: 5, anchorTimePercent: 0 }, 1000);
  assert.equal(noAnchor.phase, 'quadrants');
  assert.equal(noAnchor.deadline, Infinity);
  noAnchorUi.advanceSchedule(noAnchor, { anchorDwellSeconds: 5, anchorTimePercent: 0 }, 1e12);
  assert.equal(noAnchor.phase, 'quadrants');

  const onlyAnchorUi = page({ randomDraw: 0 });
  const onlyAnchor = onlyAnchorUi.createSchedule({ anchorDwellSeconds: 5, anchorTimePercent: 100 }, 1000);
  assert.equal(onlyAnchor.phase, 'anchor');
  assert.equal(onlyAnchor.deadline, 4500);
  onlyAnchorUi.advanceSchedule(onlyAnchor, { anchorDwellSeconds: 5, anchorTimePercent: 100 }, onlyAnchor.deadline);
  assert.equal(onlyAnchor.phase, 'anchor');
  assert.equal(onlyAnchor.deadline, 8000, '100% must renew a complete hold from the boundary');
});

test('anchor travel completes before a fresh full hold starts', () => {
  const ui = page({ randomDraw: 0 });
  const config = { anchorDwellSeconds: 5, anchorTimePercent: 20 };
  const schedule = ui.createSchedule(config, 1000);
  assert.equal(schedule.phase, 'quadrants');
  assert.equal(schedule.deadline, 15000);
  ui.advanceSchedule(schedule, config, 15000);
  assert.equal(schedule.phase, 'anchor');
  assert.equal(schedule.deadline, null, 'no hold deadline may run during the return trip');
  const anchorSerial = schedule.serial;
  ui.advanceSchedule(schedule, config, 19000);
  assert.equal(schedule.deadline, null, 'elapsed travel time must not consume the hold');
  ui.beginAnchorHold(schedule, 20000);
  assert.equal(schedule.deadline, 23500, 'the complete randomized hold starts after arrival');
  ui.advanceSchedule(schedule, config, 23500);
  assert.equal(schedule.phase, 'quadrants');
  assert.equal(schedule.serial, anchorSerial);
  assert.equal(schedule.deadline, 37500);
});

test('simulated arrival only marks the base stationary and preserves the active plan', async () => {
  const ui = page({ randomDraw: 0 });
  const socket = ui.connect();
  socket.open();
  const schedule = { phase: 'quadrants', deadline: 123456, serial: 4, cycle: { anchorMs: 5000, roamMs: 20000 } };
  const config = { ...legacyConfig, anchorTimePercent: 20, anchorDwellSeconds: 5 };
  ui.prepareArrival(schedule, 2, config);
  const before = { ...ui.arrivalState() };
  const sentBefore = socket.sent.length;

  ui.get('autoArrive').onclick();
  await ui.drainMessages();

  const after = { ...ui.arrivalState() };
  assert.deepEqual(before, {
    running: true, busy: true, token: 7, stationary: false,
    phase: 'quadrants', deadline: 123456, serial: 4, quadrant: 2, hasConfig: true
  });
  assert.deepEqual(after, { ...before, stationary: true });
  assert.equal(socket.sent.length, sentBefore, 'arrival must not issue an anchor or replacement target');
  assert.equal(ui.get('autoArrive').disabled, true);
  assert.equal(ui.get('autoStop').disabled, false);
  assert.match(ui.get('autoStatus').textContent, /自动运镜.*继续/);
});

test('the actual send preserves a selected quadrant target and explicit anchor', async () => {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  const config = { yawMin: -60, yawMax: 60, pitchMin: -15, pitchMax: 15 };
  socket.message({ gimbal: { yaw: 30, pitch: 10 } });
  await ui.drainMessages();
  await ui.moveTarget(-36, -12, config);
  const first = socket.sent.at(-1).gimbal_control;
  assert.equal(first.yaw_end, -36);
  assert.equal(first.pitch_end, -12);

  socket.message({ gimbal: { yaw: 5, pitch: 4 } });
  await ui.drainMessages();
  await ui.moveTarget(0, 0, config);
  const anchor = socket.sent.at(-1).gimbal_control;
  assert.equal(anchor.yaw_end, 0);
  assert.equal(anchor.pitch_end, 0);
});

test('a non-anchor target ends immediately after its one arrival wait', async () => {
  const ui = page();
  const socket = ui.connect();
  socket.open();
  const config = { yawMin: -60, yawMax: 60, pitchMin: -15, pitchMax: 15 };
  const outcome = await ui.moveTarget(24, -8, config);
  assert.deepEqual({ ...outcome }, { result: 'reached', waitCalls: 1 });
  assert.equal(ui.timers.size, 0, 'non-anchor movement must not schedule an extra hold');
});

test('obsolete weighted and ping-pong planners are absent from the active console', () => {
  for (const token of ['MODE_WEIGHTS', 'OPPOSITE_PROBABILITY', 'adaptiveYawTarget', 'pingpongPoses']) {
    assert.equal(source.includes(token), false, token);
  }
});

test('the timed anchor uses the same deterministic return speed as production', () => {
  assert.match(
    source,
    /moveAutoTarget\(config\.anchorYaw, config\.anchorPitch, config\.speedMax,/,
  );
  assert.doesNotMatch(source, /anchorSpeed\s*=\s*randomInteger/);
});
