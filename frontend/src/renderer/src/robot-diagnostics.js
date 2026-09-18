export const HEARTBEAT_FRESH_MS = 5000

function finite(value) {
  if (value == null || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}

export function timestampAge(timestamp, now = Date.now()) {
  const then = Date.parse(timestamp || '')
  if (!Number.isFinite(then)) return null
  return Math.max(0, now - then)
}

export function ageLabel(timestamp, now = Date.now()) {
  const age = timestampAge(timestamp, now)
  if (age == null) return '时间未知'
  if (age < 1000) return '刚刚'
  return `${(age / 1000).toFixed(age < 10000 ? 1 : 0)} 秒前`
}

export function heartbeatFreshness(snapshot, connected, now = Date.now()) {
  if (!snapshot) {
    return {
      tone: connected ? 'warning' : 'muted',
      label: connected ? '等待本次连接心跳' : '尚未收到心跳',
      fresh: false,
    }
  }
  const age = timestampAge(snapshot.received_at, now)
  if (!connected) return { tone: 'danger', label: '机器人连接已断开', fresh: false }
  if (age == null) return { tone: 'warning', label: '心跳时间未知', fresh: false }
  if (age <= HEARTBEAT_FRESH_MS) {
    return { tone: 'success', label: `正常 · ${ageLabel(snapshot.received_at, now)}`, fresh: true }
  }
  if (age <= HEARTBEAT_FRESH_MS * 3) {
    return { tone: 'warning', label: `延迟 · ${ageLabel(snapshot.received_at, now)}`, fresh: false }
  }
  return { tone: 'danger', label: `已中断 · ${ageLabel(snapshot.received_at, now)}`, fresh: false }
}

export function hasFreshHeartbeatPose(snapshot, connected, now = Date.now()) {
  if (!snapshot || !connected || finite(snapshot.yaw) == null || finite(snapshot.pitch) == null) return false
  const yawAge = timestampAge(snapshot.yaw_received_at, now)
  const pitchAge = timestampAge(snapshot.pitch_received_at, now)
  return yawAge != null && pitchAge != null
    && yawAge <= HEARTBEAT_FRESH_MS && pitchAge <= HEARTBEAT_FRESH_MS
}

export function heartbeatMotion(snapshot, now = Date.now()) {
  const payload = snapshot?.payload || {}
  const taskStatus = snapshot?.task_goal_status ?? payload.task?.goal_status
  const navigation = payload.naviagtion || payload.navigation || {}
  const navigationStatus = snapshot?.navigation_goal_status ?? navigation.goal_status
  const normalize = (status) => status === 'faild' ? 'failed' : status
  const statusIsFresh = (status, timestamp) => {
    if (!status) return false
    if (!timestamp) return true // Compatibility with snapshots from older app versions.
    const age = timestampAge(timestamp, now)
    return age != null && age <= HEARTBEAT_FRESH_MS
  }
  const taskFresh = statusIsFresh(taskStatus, snapshot?.task_goal_status_received_at)
  const navigationFresh = statusIsFresh(
    navigationStatus,
    snapshot?.navigation_goal_status_received_at,
  )
  const taskTimestamp = snapshot?.task_goal_status_received_at || ''
  const navigationTimestamp = snapshot?.navigation_goal_status_received_at || ''
  const taskAt = Date.parse(taskTimestamp)
  const navigationAt = Date.parse(navigationTimestamp)
  const sameRawFrame = payload.task?.goal_status != null && navigation.goal_status != null
  // Keep the backend's full ISO timestamp precision. Date.parse truncates microseconds, so
  // equality of parsed milliseconds is not enough to prove these came from the same frame.
  const sameTimedFrame = Boolean(taskTimestamp && navigationTimestamp
    && taskTimestamp === navigationTimestamp)
  if (taskFresh && navigationFresh && normalize(taskStatus) !== normalize(navigationStatus)
      && (sameRawFrame || sameTimedFrame)) {
    return {
      tone: 'warning', label: '任务 / 导航不一致', status: 'mismatch',
      taskStatus, navigationStatus, taskFresh, navigationFresh,
    }
  }
  let status = ''
  if (taskFresh && navigationFresh && normalize(taskStatus) !== normalize(navigationStatus)) {
    // A normal transition can arrive in two nearby frames. Prefer the newer physical report;
    // only a same-frame contradiction is classified as a protocol mismatch above.
    const navigationNewer = navigationAt > taskAt || (
      navigationAt === taskAt && navigationTimestamp > taskTimestamp
    )
    status = navigationNewer ? normalize(navigationStatus) : normalize(taskStatus)
  } else {
    status = taskFresh ? normalize(taskStatus) : navigationFresh ? normalize(navigationStatus) : ''
  }
  const common = { taskStatus, navigationStatus, taskFresh, navigationFresh }
  if (status === 'going') return { tone: 'moving', label: '行进中', status, ...common }
  if (status === 'done') return { tone: 'stationary', label: '已到点', status, ...common }
  if (status === 'failed') return { tone: 'danger', label: '任务失败', status, ...common }
  if (taskStatus || navigationStatus) {
    return { tone: 'warning', label: '行进状态未持续回报', status: 'stale', ...common }
  }
  return { tone: 'unknown', label: '未回报', status: '', ...common }
}

export function heartbeatObjectAlignment(snapshot, now = Date.now()) {
  const payloadStatus = snapshot?.payload?.task?.object_status
  const reportedStatus = snapshot?.object_status ?? payloadStatus
  const status = reportedStatus === 'faild' ? 'failed' : reportedStatus
  if (!status) {
    return { tone: 'unknown', label: '未回报', status: '', fresh: false, receivedAt: null }
  }

  const timestamp = snapshot?.object_status_received_at
    || (payloadStatus != null ? snapshot?.received_at : null)
  const age = timestampAge(timestamp, now)
  // Older snapshots did not retain a separate timestamp. A value in the latest raw frame is
  // still current because the frame itself already passed the heartbeat freshness check.
  const fresh = age != null && age <= HEARTBEAT_FRESH_MS
  if (!fresh) {
    return { tone: 'warning', label: '未持续回报', status, fresh: false, receivedAt: timestamp }
  }
  const common = { status, fresh: true, receivedAt: timestamp }
  if (status === 'going') return { tone: 'moving', label: '对准中', ...common }
  if (status === 'done') return { tone: 'success', label: '已完成', ...common }
  if (status === 'failed') return { tone: 'danger', label: '失败', ...common }
  return { tone: 'warning', label: String(status), ...common }
}

export function commandContextLabel(context) {
  return {
    manual: '手动镜头控制',
    camera_sweep: '镜头移动',
    framing_test: '取景测试',
    cruise_fixed_piece: '点位固定镜头',
    cruise_moving: '巡游行进运镜',
    cruise_stationary_camerawork: '自动运镜',
    cruise_stationary_anchor: '回到锚点',
    cruise_stationary_zoom: '锚点变焦',
    goal_object_alignment: '巡游目标物对准',
    cruise_navigation_goal: '巡游前往点位',
  }[context] || '镜头控制'
}

export function goalAttemptConfirmation(attempt) {
  if (!attempt) return { tone: 'muted', label: '—' }
  return {
    awaiting_reply: { tone: 'warning', label: '已写入，等待机器人确认' },
    accepted: { tone: 'success', label: '机器人已接受' },
    rejected: { tone: 'danger', label: '机器人已拒绝' },
    reply_timeout: { tone: 'danger', label: '已写入，机器人回复超时' },
    reply_mismatch: { tone: 'danger', label: '机器人回复点位不一致' },
    reply_error: { tone: 'danger', label: '机器人回复异常' },
  }[attempt.outcome] || { tone: 'warning', label: '确认状态未知' }
}

export function latestAppDiagnosticCommand({
  gimbalCommand = null,
  goalAttempt = null,
  goalCommand = null,
} = {}) {
  // last_goal_attempt is the transport fact the user wants to inspect. The accepted-only
  // command is just a compatibility fallback for a snapshot from an older backend.
  const goal = goalAttempt || goalCommand
  const goalSource = goalAttempt ? 'goal_attempt' : goal ? 'accepted_goal' : ''
  if (!gimbalCommand) return { source: goalSource, command: goal }
  if (!goal) return { source: 'gimbal', command: gimbalCommand }
  return timestampAtOrAfter(goal.sent_at, gimbalCommand.sent_at)
    ? { source: goalSource, command: goal }
    : { source: 'gimbal', command: gimbalCommand }
}

function goalAttemptPriorityVerdict(attempt) {
  return {
    awaiting_reply: { tone: 'warning', label: '底盘指令已写入，等待机器人确认' },
    rejected: { tone: 'danger', label: '机器人拒绝了最近底盘指令' },
    reply_timeout: { tone: 'danger', label: '底盘指令已写入，但机器人回复超时' },
    reply_mismatch: { tone: 'danger', label: '机器人回复的点位与最近底盘指令不一致' },
    reply_error: { tone: 'danger', label: '底盘指令已写入，但机器人回复异常' },
  }[attempt?.outcome] || null
}

export function gimbalCommandValues(snapshot) {
  const value = snapshot?.payload?.gimbal_control
  if (!value) return null
  return {
    mode: value.mode,
    yawStart: finite(value.yaw_start),
    yawEnd: finite(value.yaw_end),
    yawSpeed: finite(value.yaw_speed),
    pitchStart: finite(value.pitch_start),
    pitchEnd: finite(value.pitch_end),
    pitchSpeed: finite(value.pitch_speed),
    zoomStart: finite(value.zoom_start),
    zoomEnd: finite(value.zoom_end),
  }
}

export function gimbalCommandInFlight(snapshot, now = Date.now(), marginMs = 1200) {
  const values = gimbalCommandValues(snapshot)
  const age = timestampAge(snapshot?.sent_at, now)
  if (!values || age == null) return false
  const yawSeconds = values.yawSpeed > 0 && values.yawStart != null && values.yawEnd != null
    ? Math.abs(values.yawEnd - values.yawStart) / values.yawSpeed
    : 0
  const pitchSeconds = values.pitchSpeed > 0 && values.pitchStart != null && values.pitchEnd != null
    ? Math.abs(values.pitchEnd - values.pitchStart) / values.pitchSpeed
    : 0
  return age <= Math.max(yawSeconds, pitchSeconds) * 1000 + marginMs
}

export function poseDelta(command, heartbeat) {
  const values = gimbalCommandValues(command)
  const yaw = finite(heartbeat?.yaw)
  const pitch = finite(heartbeat?.pitch)
  if (!values || values.yawEnd == null || values.pitchEnd == null || yaw == null || pitch == null) return null
  return {
    yaw: Math.abs(values.yawEnd - yaw),
    pitch: Math.abs(values.pitchEnd - pitch),
  }
}

function hasOwn(value, key) {
  return value != null && Object.prototype.hasOwnProperty.call(value, key)
}

function goalIdentityMatch(command, identity) {
  const goal = command?.payload?.set_goal
  if (!goal || !identity || typeof identity !== 'object') return null
  let known = false
  let matches = true
  const reportedPath = identity.path_file ?? identity.path_name
  if (reportedPath != null && goal.path_name != null) {
    known = true
    matches = matches && String(reportedPath) === String(goal.path_name)
  }
  if (identity.goal_id != null && goal.goal_id != null) {
    known = true
    matches = matches && String(identity.goal_id) === String(goal.goal_id)
  }
  const requestedObject = String(goal.goal_object ?? '').trim()
  // A navigation-only goal does not own object alignment. Firmware may retain or autonomously
  // report an object name beside the same path/point; that field must not veto navigation
  // ownership. It becomes identity evidence only when this command explicitly requested it.
  if (requestedObject && hasOwn(identity, 'goal_object')) {
    known = true
    const reportedObject = String(identity.goal_object ?? '').trim()
    matches = matches && reportedObject === requestedObject
  }
  return known ? matches : null
}

function legacyRawIdentity(source, statusKey) {
  if (!source || source[statusKey] == null) return null
  return source.path_file != null || source.path_name != null || source.goal_id != null
    || hasOwn(source, 'goal_object')
    ? source
    : null
}

function goalFeedbackBelongsToCommand(command, heartbeat, receivedAt) {
  if (!command?.payload?.set_goal) return false
  const task = heartbeat?.payload?.task || {}
  // New snapshots retain identity from the exact frame that supplied object_status. Do not
  // replace it with the identity in a later gimbal-only/task-only frame: that would let an old
  // point's terminal status become evidence for a newly issued point.
  const identity = hasOwn(heartbeat, 'object_status_identity')
    ? heartbeat.object_status_identity
    : legacyRawIdentity(task, 'object_status')
  const identityMatches = goalIdentityMatch(command, identity)
  if (identityMatches === false) return false

  const feedbackAt = Date.parse(receivedAt || '')
  const commandAt = Date.parse(command?.sent_at || '')
  if (Number.isFinite(feedbackAt) && Number.isFinite(commandAt)) {
    return timestampAtOrAfter(receivedAt, command.sent_at)
  }
  return identityMatches === true
}

export function timestampAtOrAfter(timestamp, boundary) {
  const value = timestamp || ''
  const limit = boundary || ''
  const valueAt = Date.parse(value)
  const limitAt = Date.parse(limit)
  if (!Number.isFinite(valueAt) || !Number.isFinite(limitAt)) return false
  if (valueAt !== limitAt) return valueAt > limitAt
  // Date.parse truncates sub-millisecond precision; backend ISO strings preserve it.
  return value >= limit
}

export function objectAlignmentForCommand(goalCommand, heartbeat, now = Date.now()) {
  if (goalCommand?.context !== 'goal_object_alignment') {
    return { tone: 'unknown', label: '未要求', status: '', fresh: false }
  }
  const alignment = heartbeatObjectAlignment(heartbeat, now)
  if (!goalFeedbackBelongsToCommand(goalCommand, heartbeat, alignment.receivedAt)) {
    return { tone: 'warning', label: '等待当前点位回报', status: '', fresh: false }
  }
  return alignment
}

function goalMotionFeedbackOwnership(goalCommand, heartbeat, motion) {
  const empty = { current: false, identityConflict: false }
  if (!goalCommand || !motion?.status || ['stale', 'mismatch'].includes(motion.status)) return empty
  const normalize = (status) => status === 'faild' ? 'failed' : status
  const payload = heartbeat?.payload || {}
  const task = payload.task || {}
  const navigation = payload.naviagtion || payload.navigation || {}
  const taskStatus = normalize(heartbeat?.task_goal_status ?? task.goal_status)
  const navigationStatus = normalize(
    heartbeat?.navigation_goal_status ?? navigation.goal_status,
  )
  const candidates = []
  const addCandidate = (status, fresh, retainedAt, rawStatus, identityKey, legacyIdentity) => {
    // heartbeatMotion may choose one fresh source while the other source retains the same,
    // now-stale value. That stale value must not lend its identity to the fresh report.
    if (!fresh || status !== motion.status) return
    const timestamp = retainedAt || (rawStatus != null ? heartbeat?.received_at : null)
    const identity = hasOwn(heartbeat, identityKey)
      ? heartbeat[identityKey]
      : legacyIdentity
    candidates.push({ timestamp, rawStatus, identityMatches: goalIdentityMatch(goalCommand, identity) })
  }
  addCandidate(
    taskStatus,
    motion.taskFresh,
    heartbeat?.task_goal_status_received_at,
    task.goal_status,
    'task_goal_status_identity',
    legacyRawIdentity(task, 'goal_status'),
  )
  addCandidate(
    navigationStatus,
    motion.navigationFresh,
    heartbeat?.navigation_goal_status_received_at,
    navigation.goal_status,
    'navigation_goal_status_identity',
    legacyRawIdentity(navigation, 'goal_status')
      || (navigation.goal_status != null ? legacyRawIdentity(task, 'goal_status') : null),
  )
  const current = candidates.some((candidate) => {
    if (candidate.identityMatches === false) return false
    const feedbackAt = Date.parse(candidate.timestamp || '')
    const commandAt = Date.parse(goalCommand.sent_at || '')
    if (Number.isFinite(feedbackAt) && Number.isFinite(commandAt)) {
      return timestampAtOrAfter(candidate.timestamp, goalCommand.sent_at)
    }
    // Compatibility fallback for old snapshots without per-status timestamps. Only a raw
    // status carrying this goal's identity is strong enough to attribute it.
    return candidate.rawStatus != null && candidate.identityMatches === true
  })
  return {
    current,
    identityConflict: candidates.some((candidate) => candidate.identityMatches === false),
  }
}

export function shootingApplicationPhase({
  cruiseRunning = false,
  segments = [],
  manualCaptureActive = false,
  robotRecording = false,
} = {}) {
  if (cruiseRunning) {
    if (segments.some((segment) => segment.status === 'navigating')) {
      return { key: 'cruise_moving', label: '巡游行进（应用计划）', tone: 'moving' }
    }
    const parked = [...segments].reverse().find((segment) =>
      segment.status === 'arrived' && segment.departed_at_seconds == null
    )
    if (parked) return { key: 'cruise_stationary', label: '点位已到达（应用计划）', tone: 'stationary' }
    return { key: 'cruise_preparing', label: '巡游准备 / 切换点位', tone: 'warning' }
  }
  if (manualCaptureActive) {
    return robotRecording
      ? { key: 'fixed_capture', label: '原地拍摄（应用计划）', tone: 'stationary' }
      : { key: 'capture_save_pending', label: '原地拍摄已停止（等待保存）', tone: 'warning' }
  }
  if (robotRecording) {
    return { key: 'reported_recording', label: '机器人回报录制中（来源未确认）', tone: 'warning' }
  }
  return { key: 'not_cruising', label: '非巡游', tone: 'muted' }
}

export function cameraDiagnosticVerdict({
  connected,
  heartbeat,
  command,
  goalCommand = null,
  goalAttempt = null,
  applicationPhaseKey = '',
  now = Date.now(),
}) {
  // Goal commands are issued only by cruise. Keep them visible as history in the cards, but do
  // not let a completed cruise's old goal continue to control the current top-level verdict.
  const goalLifecycleActive = !applicationPhaseKey || applicationPhaseKey.startsWith('cruise_')
  const unacceptedAttemptSupersedesGoal = goalAttempt?.outcome !== 'accepted'
    && goalCommand
    && timestampAtOrAfter(goalAttempt?.sent_at, goalCommand.sent_at)
  const currentGoalCommand = goalLifecycleActive && !unacceptedAttemptSupersedesGoal
    ? goalCommand
    : null
  // Object alignment is robot-owned and has no numeric target. It remains visible in the
  // history/heartbeat row, but it must never become a shooting gate or a pose comparison.
  const currentCommand = command?.context === 'goal_object_alignment' ? null : command
  const latestCommand = latestAppDiagnosticCommand({
    gimbalCommand: command,
    goalAttempt,
    goalCommand,
  })
  const attemptVerdict = latestCommand.source === 'goal_attempt'
    ? goalAttemptPriorityVerdict(goalAttempt)
    : null
  const freshness = heartbeatFreshness(heartbeat, connected, now)
  const motion = heartbeatMotion(heartbeat, now)
  const poseFresh = hasFreshHeartbeatPose(heartbeat, connected, now)
  const hasCommand = Boolean(currentCommand || currentGoalCommand || goalAttempt)
  if (!connected) return { tone: 'danger', label: '机器人连接未建立' }
  if (attemptVerdict) return attemptVerdict
  if (!heartbeat) {
    return hasCommand
      ? { tone: 'warning', label: '指令已下发，但尚未收到机器人心跳' }
      : { tone: 'warning', label: '等待机器人心跳' }
  }
  if (!freshness.fresh) {
    return hasCommand
      ? { tone: 'danger', label: '指令已下发，但机器人心跳不是实时的' }
      : { tone: 'warning', label: '机器人心跳不是实时的' }
  }
  // A same-frame contradiction is a property of the robot report itself. It stays actionable
  // even if the report names an unexpected/previous point.
  if (motion.status === 'mismatch') {
    return { tone: 'warning', label: '机器人心跳中的任务与导航状态不一致' }
  }
  const motionOwnership = currentGoalCommand
    ? goalMotionFeedbackOwnership(currentGoalCommand, heartbeat, motion)
    : { current: true, identityConflict: false }
  const motionIsCurrent = motionOwnership.current
  const motionStatus = motionIsCurrent ? motion.status : ''
  if (motionStatus === 'failed') {
    return { tone: 'danger', label: '机器人心跳回报任务失败' }
  }
  if (motion.status === 'failed' && motionOwnership.identityConflict) {
    return { tone: 'danger', label: '机器人回报其他点位任务失败' }
  }
  if (applicationPhaseKey === 'cruise_moving' && !motionIsCurrent) {
    return { tone: 'warning', label: '巡游指令已下发，等待本次点位的行进回报' }
  }
  if (applicationPhaseKey === 'cruise_moving' && motionStatus !== 'going') {
    return { tone: 'warning', label: '应用正在巡游，但机器人心跳未持续回报行进' }
  }
  if (applicationPhaseKey === 'cruise_stationary' && motionStatus === 'going') {
    return { tone: 'warning', label: '应用已到达点位，但机器人仍回报行进' }
  }
  if (applicationPhaseKey === 'cruise_stationary' && motionStatus !== 'done') {
    return { tone: 'warning', label: '应用已到达点位，但机器人心跳未持续回报到点' }
  }
  if (applicationPhaseKey === 'fixed_capture' && motionStatus === 'going') {
    return { tone: 'warning', label: '应用正在原地拍摄，但机器人仍回报行进' }
  }
  if (!currentCommand && currentGoalCommand) {
    if (motionStatus === 'going') {
      return { tone: 'moving', label: '机器人回报正在前往点位' }
    }
    if (motionStatus === 'done') {
      return { tone: 'success', label: '机器人回报已到达点位' }
    }
    return { tone: 'warning', label: '前往点位指令已下发，等待机器人回报' }
  }
  if (!currentCommand) {
    return poseFresh
      ? { tone: 'success', label: '心跳与实测角度正常，等待镜头指令' }
      : { tone: 'warning', label: '心跳存在，但实测角度没有持续更新' }
  }
  if (!poseFresh) return { tone: 'warning', label: '心跳存在，但实测角度没有持续更新' }

  const poseAfterCommand = timestampAtOrAfter(heartbeat.yaw_received_at, currentCommand.sent_at)
    && timestampAtOrAfter(heartbeat.pitch_received_at, currentCommand.sent_at)
  if (!poseAfterCommand) return { tone: 'warning', label: '连接正常，等待命令后的实测角度' }
  const delta = poseDelta(currentCommand, heartbeat)
  if (delta && delta.yaw <= 2 && delta.pitch <= 2) {
    return { tone: 'success', label: '最近目标与实测角度基本一致' }
  }
  if (gimbalCommandInFlight(currentCommand, now)) {
    return { tone: 'moving', label: '已下发，仍在预计运镜时间内' }
  }
  return { tone: 'warning', label: '心跳正常，实测角度尚未到达最近目标' }
}

export function prettyDiagnostic(value) {
  return value ? JSON.stringify(value, null, 2) : ''
}
