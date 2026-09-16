import assert from 'node:assert/strict'
import test from 'node:test'

import {
  cameraDiagnosticVerdict,
  commandContextLabel,
  gimbalCommandInFlight,
  gimbalCommandValues,
  goalAttemptConfirmation,
  hasFreshHeartbeatPose,
  heartbeatFreshness,
  heartbeatMotion,
  heartbeatObjectAlignment,
  latestAppDiagnosticCommand,
  objectAlignmentForCommand,
  poseDelta,
  shootingApplicationPhase,
  timestampAtOrAfter,
} from '../src/renderer/src/robot-diagnostics.js'

test('heartbeat freshness does not confuse a connected socket with a live heartbeat', () => {
  const now = Date.parse('2026-09-15T08:00:10Z')
  assert.deepEqual(heartbeatFreshness(null, true, now), {
    tone: 'warning', label: '等待本次连接心跳', fresh: false,
  })
  assert.equal(heartbeatFreshness({ received_at: '2026-09-15T08:00:08Z' }, true, now).fresh, true)
  assert.equal(heartbeatFreshness({ received_at: '2026-09-15T08:00:04Z' }, true, now).tone, 'warning')
  assert.equal(heartbeatFreshness({ received_at: '2026-09-15T07:59:40Z' }, true, now).tone, 'danger')
  assert.equal(heartbeatFreshness({ received_at: '2026-09-15T08:00:09Z' }, false, now).fresh, false)

  const poseStopped = {
    received_at: '2026-09-15T08:00:09Z',
    yaw: 3,
    pitch: -2,
    yaw_received_at: '2026-09-15T07:59:50Z',
    pitch_received_at: '2026-09-15T07:59:50Z',
  }
  assert.equal(heartbeatFreshness(poseStopped, true, now).fresh, true)
  assert.equal(hasFreshHeartbeatPose(poseStopped, true, now), false)
})

test('robot movement comes only from the actual task or navigation heartbeat', () => {
  assert.equal(heartbeatMotion({ payload: { task: { goal_status: 'going' } } }).label, '行进中')
  assert.equal(heartbeatMotion({ payload: { navigation: { goal_status: 'done' } } }).label, '已到点')
  assert.equal(heartbeatMotion({ payload: {
    task: { goal_status: 'done' }, navigation: { goal_status: 'going' },
  } }).label, '任务 / 导航不一致')
  assert.equal(heartbeatMotion({ payload: {} }).label, '未回报')

  const now = Date.parse('2026-09-15T08:00:10Z')
  const retained = {
    payload: { gimbal: { yaw: 1 } },
    task_goal_status: 'going',
    task_goal_status_received_at: '2026-09-15T08:00:09Z',
    navigation_goal_status: 'going',
    navigation_goal_status_received_at: '2026-09-15T08:00:09Z',
  }
  assert.equal(heartbeatMotion(retained, now).label, '行进中')
  retained.task_goal_status_received_at = '2026-09-15T07:59:40Z'
  retained.navigation_goal_status_received_at = '2026-09-15T07:59:40Z'
  assert.equal(heartbeatMotion(retained, now).label, '行进状态未持续回报')

  const transitioning = {
    payload: {},
    task_goal_status: 'done',
    task_goal_status_received_at: '2026-09-15T08:00:08Z',
    navigation_goal_status: 'going',
    navigation_goal_status_received_at: '2026-09-15T08:00:09Z',
  }
  assert.equal(heartbeatMotion(transitioning, now).label, '行进中')
  transitioning.task_goal_status_received_at = '2026-09-15T08:00:09Z'
  assert.equal(heartbeatMotion(transitioning, now).label, '任务 / 导航不一致')

  transitioning.task_goal_status_received_at = '2026-09-15T08:00:09.123100Z'
  transitioning.navigation_goal_status_received_at = '2026-09-15T08:00:09.123900Z'
  assert.equal(heartbeatMotion(transitioning, now).label, '行进中')
})

test('gimbal diagnostics preserve context and compare target with physical pose', () => {
  const command = {
    context: 'cruise_stationary_anchor',
    payload: { gimbal_control: {
      mode: 1,
      yaw_start: -20,
      yaw_end: 0,
      yaw_speed: 4,
      pitch_start: -8,
      pitch_end: 0,
      pitch_speed: 3,
      zoom_start: 1.4,
      zoom_end: 1,
    } },
  }
  assert.equal(commandContextLabel(command.context), '点位回到锚点')
  assert.equal(gimbalCommandValues(command).mode, 1)
  assert.deepEqual(poseDelta(command, { yaw: -2, pitch: 1 }), { yaw: 2, pitch: 1 })
  assert.equal(poseDelta(command, { yaw: null, pitch: null }), null)
  assert.equal(gimbalCommandInFlight({ ...command, sent_at: '2026-09-15T08:00:00Z' }, Date.parse('2026-09-15T08:00:02Z')), true)
  assert.equal(gimbalCommandInFlight({ ...command, sent_at: '2026-09-15T08:00:00Z' }, Date.parse('2026-09-15T08:00:10Z')), false)
})

test('goal attempts expose confirmation separately from the accepted goal', () => {
  const acceptedGoal = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path-a', goal_id: 1 } },
  }
  const attempt = {
    ...acceptedGoal,
    sent_at: '2026-09-15T08:00:02Z',
    outcome: 'awaiting_reply',
    payload: { set_goal: { path_name: 'path-b', goal_id: 2 } },
  }
  const olderGimbal = {
    context: 'cruise_moving',
    sent_at: '2026-09-15T08:00:01Z',
    payload: { gimbal_control: {} },
  }
  const newerGimbal = { ...olderGimbal, sent_at: '2026-09-15T08:00:03Z' }

  assert.deepEqual(goalAttemptConfirmation(attempt), {
    tone: 'warning', label: '已写入，等待机器人确认',
  })
  assert.equal(goalAttemptConfirmation({ ...attempt, outcome: 'accepted' }).label,
    '机器人已接受')
  assert.equal(goalAttemptConfirmation({ ...attempt, outcome: 'rejected' }).label,
    '机器人已拒绝')
  assert.equal(goalAttemptConfirmation({ ...attempt, outcome: 'reply_timeout' }).label,
    '已写入，机器人回复超时')
  assert.equal(goalAttemptConfirmation({ ...attempt, outcome: 'reply_mismatch' }).label,
    '机器人回复点位不一致')
  assert.equal(goalAttemptConfirmation({ ...attempt, outcome: 'reply_error' }).label,
    '机器人回复异常')
  assert.equal(latestAppDiagnosticCommand({
    gimbalCommand: olderGimbal, goalAttempt: attempt, goalCommand: acceptedGoal,
  }).source, 'goal_attempt')
  assert.equal(latestAppDiagnosticCommand({
    gimbalCommand: newerGimbal, goalAttempt: attempt, goalCommand: acceptedGoal,
  }).source, 'gimbal')
  assert.equal(latestAppDiagnosticCommand({ goalCommand: acceptedGoal }).source, 'accepted_goal')
})

test('latest unresolved goal attempt is explicit but a newer gimbal supersedes its verdict', () => {
  const now = Date.parse('2026-09-15T08:00:04Z')
  const acceptedGoal = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path-a', goal_id: 1 } },
  }
  const attempt = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:02Z',
    payload: { set_goal: { path_name: 'path-b', goal_id: 2 } },
    outcome: 'awaiting_reply',
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:04Z',
    yaw: 0,
    pitch: 0,
    yaw_received_at: '2026-09-15T08:00:04Z',
    pitch_received_at: '2026-09-15T08:00:04Z',
    payload: { gimbal: { yaw: 0, pitch: 0 } },
  }

  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command: null, goalCommand: acceptedGoal,
    goalAttempt: attempt, applicationPhaseKey: 'cruise_preparing', now,
  }).label, '底盘指令已写入，等待机器人确认')
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command: null, goalCommand: acceptedGoal,
    goalAttempt: { ...attempt, outcome: 'rejected' },
    applicationPhaseKey: 'cruise_preparing', now,
  }).label, '机器人拒绝了最近底盘指令')
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command: null, goalCommand: acceptedGoal,
    goalAttempt: { ...attempt, outcome: 'reply_timeout' },
    applicationPhaseKey: 'cruise_preparing', now,
  }).label, '底盘指令已写入，但机器人回复超时')
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command: null, goalCommand: acceptedGoal,
    goalAttempt: { ...attempt, outcome: 'reply_mismatch' },
    applicationPhaseKey: 'cruise_preparing', now,
  }).label, '机器人回复的点位与最近底盘指令不一致')
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command: null, goalCommand: acceptedGoal,
    goalAttempt: { ...attempt, outcome: 'reply_error' },
    applicationPhaseKey: 'cruise_preparing', now,
  }).label, '底盘指令已写入，但机器人回复异常')

  const newerGimbal = {
    context: 'cruise_stationary_anchor',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { gimbal_control: {
      yaw_start: 0, yaw_end: 0, yaw_speed: 2,
      pitch_start: 0, pitch_end: 0, pitch_speed: 2,
    } },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: newerGimbal,
    goalCommand: acceptedGoal,
    goalAttempt: { ...attempt, outcome: 'reply_timeout' },
    applicationPhaseKey: 'cruise_preparing',
    now,
  }).label, '最近目标与实测角度基本一致')
})

test('object alignment failure remains diagnostic and never changes arrival verdict', () => {
  const now = Date.parse('2026-09-15T08:00:02Z')
  const goal = {
    context: 'goal_object_alignment',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 3, goal_object: 'car' } },
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    yaw: 0,
    pitch: 0,
    yaw_received_at: '2026-09-15T08:00:01Z',
    pitch_received_at: '2026-09-15T08:00:01Z',
    task_goal_status: 'done',
    task_goal_status_received_at: '2026-09-15T08:00:01Z',
    task_goal_status_identity: { path_file: 'path1', goal_id: 3, goal_object: 'car' },
    object_status: 'failed',
    object_status_received_at: '2026-09-15T08:00:01Z',
    object_status_identity: { path_file: 'path1', goal_id: 3, goal_object: 'car' },
    payload: {},
  }

  assert.equal(objectAlignmentForCommand(goal, heartbeat, now).label, '失败')
  assert.deepEqual(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: goal,
    goalCommand: goal,
    goalAttempt: { ...goal, outcome: 'accepted' },
    applicationPhaseKey: 'cruise_stationary',
    now,
  }), { tone: 'success', label: '机器人回报已到达点位' })
})

test('goal-object diagnostics use robot status instead of inventing a numeric target', () => {
  const now = Date.parse('2026-09-15T08:00:02Z')
  const command = {
    context: 'goal_object_alignment',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 3, goal_object: 'car' } },
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    yaw: 12,
    pitch: -3,
    yaw_received_at: '2026-09-15T08:00:01Z',
    pitch_received_at: '2026-09-15T08:00:01Z',
    task_goal_status: 'done',
    task_goal_status_received_at: '2026-09-15T08:00:01Z',
    object_status: 'going',
    object_status_received_at: '2026-09-15T08:00:01Z',
    payload: {},
  }

  assert.equal(commandContextLabel(command.context), '巡游目标物对准')
  assert.equal(gimbalCommandValues(command), null)
  assert.equal(heartbeatObjectAlignment(heartbeat, now).label, '对准中')
  assert.equal(heartbeatObjectAlignment(heartbeat, now).fresh, true)
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command, goalCommand: command, now,
  }).label,
    '机器人回报已到达点位')

  heartbeat.object_status = 'done'
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command, goalCommand: command, now,
  }).label,
    '机器人回报已到达点位')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command,
    goalCommand: command,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '应用正在巡游，但机器人心跳未持续回报行进')
  heartbeat.object_status = 'faild'
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command, goalCommand: command, now,
  }).label,
    '机器人回报已到达点位')

  heartbeat.object_status = null
  heartbeat.object_status_received_at = null
  heartbeat.task_goal_status = 'going'
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command, goalCommand: command, now,
  }).label,
    '机器人回报正在前往点位')
  heartbeat.task_goal_status = 'done'
  assert.equal(cameraDiagnosticVerdict({
    connected: true, heartbeat, command, goalCommand: command, now,
  }).label,
    '机器人回报已到达点位')

  const rawOnly = {
    received_at: '2026-09-15T08:00:01Z',
    payload: { task: { object_status: 'going' } },
  }
  assert.equal(heartbeatObjectAlignment(rawOnly, now).label, '对准中')
  assert.equal(heartbeatObjectAlignment(rawOnly, Date.parse('2026-09-15T08:00:20Z')).label,
    '未持续回报')

  const nextGoal = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: '' } },
  }
  const nextHeartbeat = {
    ...heartbeat,
    received_at: '2026-09-15T08:00:04Z',
    task_goal_status: 'going',
    task_goal_status_received_at: '2026-09-15T08:00:04Z',
    path_file: 'path1',
    goal_id: 4,
    object_status: 'done',
    object_status_received_at: '2026-09-15T08:00:01Z',
  }
  assert.equal(commandContextLabel(nextGoal.context), '巡游前往点位')
  assert.equal(objectAlignmentForCommand(nextGoal, nextHeartbeat, now).label, '未要求')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: nextHeartbeat,
    command: null,
    goalCommand: nextGoal,
    applicationPhaseKey: 'cruise_moving',
    now: Date.parse('2026-09-15T08:00:04Z'),
  }).label, '机器人回报正在前往点位')

  const oldMovement = {
    ...nextHeartbeat,
    task_goal_status: 'going',
    task_goal_status_received_at: '2026-09-15T08:00:01Z',
    payload: { gimbal: { yaw: 12, pitch: -3 } },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: oldMovement,
    command: null,
    goalCommand: nextGoal,
    applicationPhaseKey: 'cruise_moving',
    now: Date.parse('2026-09-15T08:00:04Z'),
  }).label, '巡游指令已下发，等待本次点位的行进回报')

  const splitHeartbeat = {
    ...heartbeat,
    received_at: '2026-09-15T08:00:04Z',
    path_file: 'path1',
    goal_id: 3,
    object_status: 'going',
    object_status_received_at: '2026-09-15T08:00:04Z',
    payload: { task: { object_status: 'going' } },
  }
  const nextObjectGoal = {
    context: 'goal_object_alignment',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: 'door' } },
  }
  assert.equal(objectAlignmentForCommand(nextObjectGoal, splitHeartbeat, now).label, '对准中')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: splitHeartbeat,
    command: nextObjectGoal,
    goalCommand: nextObjectGoal,
    now: Date.parse('2026-09-15T08:00:04Z'),
  }).label, '前往点位指令已下发，等待机器人回报')

  assert.equal(timestampAtOrAfter(
    '2026-09-15T08:00:09.123100Z',
    '2026-09-15T08:00:09.123900Z',
  ), false)

  const repeatedGoalHeartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    object_status: 'done',
    object_status_received_at: '2026-09-15T08:00:01Z',
    payload: { task: {
      path_file: 'path1', goal_id: 3, object_status: 'done',
    } },
  }
  const repeatedGoal = { ...command, sent_at: '2026-09-15T08:00:03Z' }
  assert.equal(
    objectAlignmentForCommand(repeatedGoal, repeatedGoalHeartbeat, now).label,
    '等待当前点位回报',
  )

  const staleTerminal = {
    ...heartbeat,
    received_at: '2026-09-15T08:00:10Z',
    object_status: 'done',
    object_status_received_at: '2026-09-15T08:00:01Z',
    payload: { gimbal: { yaw: 12, pitch: -3 } },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: staleTerminal,
    command,
    goalCommand: command,
    now: Date.parse('2026-09-15T08:00:10Z'),
  }).label, '前往点位指令已下发，等待机器人回报')
})

test('goal-object feedback belongs to the requested object when heartbeat supplies its identity', () => {
  const now = Date.parse('2026-09-15T08:00:02Z')
  const command = {
    context: 'goal_object_alignment',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 3, goal_object: 'car' } },
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    object_status: 'done',
    object_status_received_at: '2026-09-15T08:00:01Z',
    payload: { task: {
      path_file: 'path1', goal_id: 3, goal_object: 'door', object_status: 'done',
    } },
  }

  assert.equal(objectAlignmentForCommand(command, heartbeat, now).label, '等待当前点位回报')
})

test('retained status provenance cannot change ownership after a later gimbal-only frame', () => {
  const now = Date.parse('2026-09-15T08:00:05Z')
  const objectGoal = {
    context: 'goal_object_alignment',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: 'door' } },
  }
  const oldObjectFeedback = {
    received_at: '2026-09-15T08:00:05Z',
    yaw: 0,
    pitch: 0,
    yaw_received_at: '2026-09-15T08:00:05Z',
    pitch_received_at: '2026-09-15T08:00:05Z',
    object_status: 'done',
    object_status_received_at: '2026-09-15T08:00:04Z',
    object_status_identity: {
      path_file: 'path1', goal_id: 3, goal_object: 'car',
    },
    payload: { gimbal: { yaw: 0, pitch: 0 } },
  }
  assert.equal(objectAlignmentForCommand(objectGoal, oldObjectFeedback, now).label,
    '等待当前点位回报')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: oldObjectFeedback,
    command: objectGoal,
    goalCommand: objectGoal,
    applicationPhaseKey: 'cruise_preparing',
    now,
  }).label, '前往点位指令已下发，等待机器人回报')

  const currentObjectFeedback = {
    ...oldObjectFeedback,
    object_status_identity: {
      path_file: 'path1', goal_id: 4, goal_object: 'door',
    },
  }
  assert.equal(objectAlignmentForCommand(objectGoal, currentObjectFeedback, now).label, '已完成')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: currentObjectFeedback,
    command: objectGoal,
    goalCommand: objectGoal,
    applicationPhaseKey: 'cruise_preparing',
    now,
  }).label, '前往点位指令已下发，等待机器人回报')

  const navigationGoal = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: '' } },
  }
  const oldMovement = {
    ...oldObjectFeedback,
    object_status: null,
    object_status_received_at: null,
    object_status_identity: null,
    task_goal_status: 'going',
    task_goal_status_received_at: '2026-09-15T08:00:04Z',
    task_goal_status_identity: { path_file: 'path1', goal_id: 3 },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: oldMovement,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '巡游指令已下发，等待本次点位的行进回报')

  const currentMovement = {
    ...oldMovement,
    task_goal_status_identity: { path_file: 'path1', goal_id: 4 },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: currentMovement,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '机器人回报正在前往点位')

  const navigationWithAutonomousObject = {
    ...currentMovement,
    task_goal_status_identity: {
      path_file: 'path1', goal_id: 4, goal_object: 'car',
    },
    object_status: 'failed',
    object_status_received_at: '2026-09-15T08:00:04Z',
    object_status_identity: {
      path_file: 'path1', goal_id: 4, goal_object: 'car',
    },
  }
  assert.equal(objectAlignmentForCommand(navigationGoal, navigationWithAutonomousObject, now).label,
    '未要求')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: navigationWithAutonomousObject,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '机器人回报正在前往点位')

  const oldNavigationMovement = {
    ...oldMovement,
    task_goal_status: null,
    task_goal_status_received_at: null,
    task_goal_status_identity: null,
    navigation_goal_status: 'going',
    navigation_goal_status_received_at: '2026-09-15T08:00:04Z',
    navigation_goal_status_identity: { path_file: 'path1', goal_id: 3 },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: oldNavigationMovement,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '巡游指令已下发，等待本次点位的行进回报')

  const otherPointFailure = {
    ...oldMovement,
    task_goal_status: 'failed',
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: otherPointFailure,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '机器人回报其他点位任务失败')

  const freshOtherFailureWithStaleCurrentStatus = {
    ...oldMovement,
    received_at: '2026-09-15T08:00:10Z',
    task_goal_status: 'failed',
    task_goal_status_received_at: '2026-09-15T08:00:04Z',
    task_goal_status_identity: { path_file: 'path1', goal_id: 4 },
    navigation_goal_status: 'failed',
    navigation_goal_status_received_at: '2026-09-15T08:00:09Z',
    navigation_goal_status_identity: { path_file: 'path1', goal_id: 3 },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: freshOtherFailureWithStaleCurrentStatus,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'cruise_moving',
    now: Date.parse('2026-09-15T08:00:10Z'),
  }).label, '机器人回报其他点位任务失败')
})

test('same-frame protocol contradiction remains visible for an unexpected point', () => {
  const now = Date.parse('2026-09-15T08:00:04Z')
  const goalCommand = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:03Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: '' } },
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:04Z',
    task_goal_status: 'done',
    task_goal_status_received_at: '2026-09-15T08:00:04Z',
    task_goal_status_identity: { path_file: 'path1', goal_id: 3 },
    navigation_goal_status: 'going',
    navigation_goal_status_received_at: '2026-09-15T08:00:04Z',
    navigation_goal_status_identity: { path_file: 'path1', goal_id: 3 },
    payload: {
      task: { path_file: 'path1', goal_id: 3, goal_status: 'done' },
      navigation: { goal_status: 'going' },
    },
  }
  assert.equal(heartbeatMotion(heartbeat, now).status, 'mismatch')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: null,
    goalCommand,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '机器人心跳中的任务与导航状态不一致')
})

test('completed cruise goal remains history rather than controlling the current verdict', () => {
  const now = Date.parse('2026-09-15T08:00:10Z')
  const heartbeat = {
    received_at: '2026-09-15T08:00:10Z',
    yaw: 0,
    pitch: 0,
    yaw_received_at: '2026-09-15T08:00:10Z',
    pitch_received_at: '2026-09-15T08:00:10Z',
    payload: { gimbal: { yaw: 0, pitch: 0 } },
  }
  const navigationGoal = {
    context: 'cruise_navigation_goal',
    sent_at: '2026-09-15T08:00:00Z',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: '' } },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: null,
    goalCommand: navigationGoal,
    applicationPhaseKey: 'not_cruising',
    now,
  }).label, '心跳与实测角度正常，等待镜头指令')

  const objectGoal = {
    ...navigationGoal,
    context: 'goal_object_alignment',
    payload: { set_goal: { path_name: 'path1', goal_id: 4, goal_object: 'door' } },
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: objectGoal,
    goalCommand: objectGoal,
    applicationPhaseKey: 'not_cruising',
    now,
  }).label, '心跳与实测角度正常，等待镜头指令')
})

test('shooting application phase separates recording from pending local save', () => {
  assert.deepEqual(shootingApplicationPhase({
    manualCaptureActive: true,
    robotRecording: false,
  }), {
    key: 'capture_save_pending', label: '原地拍摄已停止（等待保存）', tone: 'warning',
  })
  assert.equal(shootingApplicationPhase({
    manualCaptureActive: true,
    robotRecording: true,
  }).key, 'fixed_capture')
  assert.equal(shootingApplicationPhase({ robotRecording: true }).key, 'reported_recording')
  assert.equal(shootingApplicationPhase({
    cruiseRunning: true,
    segments: [{ status: 'navigating' }],
    manualCaptureActive: true,
    robotRecording: false,
  }).key, 'cruise_moving')
})

test('camera verdict distinguishes missing telemetry, expected travel, and real mismatch', () => {
  const now = Date.parse('2026-09-15T08:00:02Z')
  const command = {
    sent_at: '2026-09-15T08:00:00Z',
    payload: { gimbal_control: {
      yaw_start: -20, yaw_end: 0, yaw_speed: 4,
      pitch_start: -8, pitch_end: 0, pitch_speed: 2,
    } },
  }
  const heartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    yaw: -12,
    pitch: -4,
    yaw_received_at: '2026-09-15T08:00:01Z',
    pitch_received_at: '2026-09-15T08:00:01Z',
    task_goal_status: 'going',
    task_goal_status_received_at: '2026-09-15T08:00:01Z',
  }

  assert.equal(cameraDiagnosticVerdict({ connected: true, heartbeat: null, command, now }).label,
    '指令已下发，但尚未收到机器人心跳')
  assert.equal(cameraDiagnosticVerdict({ connected: true, heartbeat, command, now }).label,
    '已下发，仍在预计运镜时间内')
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command,
    applicationPhaseKey: 'cruise_stationary',
    now,
  }).label, '应用已进入停留，但机器人仍回报行进')

  const staleMovement = {
    ...heartbeat,
    task_goal_status_received_at: '2026-09-15T07:59:40Z',
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: staleMovement,
    command,
    applicationPhaseKey: 'cruise_stationary',
    now,
  }).label, '应用已进入停留，但机器人心跳未持续回报到点')

  const stalePose = {
    ...heartbeat,
    received_at: '2026-09-15T08:00:09Z',
    yaw_received_at: '2026-09-15T08:00:00Z',
    pitch_received_at: '2026-09-15T08:00:00Z',
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: stalePose,
    command,
    now: Date.parse('2026-09-15T08:00:10Z'),
  }).label, '心跳存在，但实测角度没有持续更新')
})

test('camera verdict compares cruise phase even when automatic camerawork sent no command', () => {
  const now = Date.parse('2026-09-15T08:00:02Z')
  const heartbeat = {
    received_at: '2026-09-15T08:00:01Z',
    yaw: 0,
    pitch: 0,
    yaw_received_at: '2026-09-15T08:00:01Z',
    pitch_received_at: '2026-09-15T08:00:01Z',
    task_goal_status: 'done',
    task_goal_status_received_at: '2026-09-15T08:00:01Z',
  }

  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: null,
    applicationPhaseKey: 'cruise_moving',
    now,
  }).label, '应用正在巡游，但机器人心跳未持续回报行进')
  heartbeat.task_goal_status = 'going'
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: null,
    applicationPhaseKey: 'cruise_stationary',
    now,
  }).label, '应用已进入停留，但机器人仍回报行进')

  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat,
    command: null,
    applicationPhaseKey: 'fixed_capture',
    now,
  }).label, '应用正在原地拍摄，但机器人仍回报行进')

  const contradictory = {
    ...heartbeat,
    task_goal_status: 'done',
    navigation_goal_status: 'going',
    navigation_goal_status_received_at: '2026-09-15T08:00:01Z',
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: contradictory,
    command: null,
    now,
  }).label, '机器人心跳中的任务与导航状态不一致')

  const failed = {
    ...heartbeat,
    task_goal_status: 'failed',
  }
  assert.equal(cameraDiagnosticVerdict({
    connected: true,
    heartbeat: failed,
    command: null,
    now,
  }).label, '机器人心跳回报任务失败')
})
