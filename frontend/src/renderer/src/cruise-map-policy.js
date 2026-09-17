function mapName(value) {
  return typeof value === 'string' ? value.trim() : ''
}

function pathName(value) {
  return typeof value === 'string' ? value.trim() : ''
}

export function isValidCruiseGoalId(value) {
  if (!['number', 'string'].includes(typeof value)) return false
  if (typeof value === 'string' && !value.trim()) return false
  const goalId = Number(value)
  return Number.isInteger(goalId) && goalId >= 0
}

export function cruiseMapContext(currentMap, cruiseMap) {
  const current = mapName(currentMap)
  const target = mapName(cruiseMap)

  if (!target) {
    return {
      kind: 'muted',
      text: `机器人当前：${current || '未回报'} · 请选择本次巡游地图`
    }
  }
  if (!current) {
    return {
      kind: 'muted',
      text: `机器人当前：未回报 · 本次巡游：${target} · 开始时核对地图`
    }
  }
  if (current === target) {
    return {
      kind: 'success',
      text: `机器人当前：${current} · 本次巡游：${target}`
    }
  }
  return {
    kind: 'muted',
    text: `机器人当前：${current} · 本次巡游：${target} · 开始时自动切换`
  }
}

export function isCurrentMapRequest(requestedMap, selectedMap, requestId, latestRequestId) {
  return requestId === latestRequestId && mapName(requestedMap) === mapName(selectedMap)
}

export function cruiseMapChangeDecision(previousMap, nextMap, pointCount) {
  const previous = mapName(previousMap)
  const next = mapName(nextMap)
  const hasPoints = Number(pointCount) > 0
  const assigningLegacyMap = !previous && Boolean(next) && hasPoints

  return {
    preservePoints: assigningLegacyMap,
    requiresConfirmation: hasPoints && Boolean(previous) && previous !== next,
    clearPoints: hasPoints && !assigningLegacyMap
  }
}

export function inspectCruisePlan({ cruiseMap, mapsVerified = false, availableMaps, loadedMap, availablePaths, points }) {
  const targetMap = mapName(cruiseMap)
  const verifiedMap = mapName(loadedMap)
  const routePoints = Array.isArray(points) ? points : []

  if (!targetMap) {
    return { ready: false, code: 'missing-map', message: '请选择本次巡游地图。', stalePaths: [] }
  }
  if (mapsVerified && !(Array.isArray(availableMaps) ? availableMaps : []).map(mapName).includes(targetMap)) {
    return { ready: false, code: 'unavailable-map', message: `机器人未提供地图「${targetMap}」。`, stalePaths: [] }
  }
  if (verifiedMap !== targetMap) {
    return { ready: false, code: 'paths-unverified', message: '正在核对本次巡游地图的路径文件。', stalePaths: [] }
  }
  if (!routePoints.length) {
    return { ready: false, code: 'empty-points', message: '请先添加巡游点位。', stalePaths: [] }
  }

  const knownPaths = new Set((Array.isArray(availablePaths) ? availablePaths : []).map(pathName).filter(Boolean))
  const stalePaths = [...new Set(routePoints
    .map((point) => pathName(point?.path_name))
    .filter((name) => !name || !knownPaths.has(name)))]
  if (stalePaths.length) {
    return {
      ready: false,
      code: 'stale-paths',
      message: `清单含有不属于本次巡游地图的路径：${stalePaths.filter(Boolean).join('、') || '未命名路径'}。`,
      stalePaths
    }
  }

  const hasInvalidGoal = routePoints.some((point) => !isValidCruiseGoalId(point?.goal_id))
  if (hasInvalidGoal) {
    return { ready: false, code: 'invalid-goal', message: '清单含有无效的目标点编号。', stalePaths: [] }
  }

  return { ready: true, code: 'ready', message: '', stalePaths: [] }
}

export function isSavedCruiseRequestReady(request) {
  if (!mapName(request?.map_name)) return false
  if (!Array.isArray(request?.points) || !request.points.length) return false
  return request.points.every((point) => {
    return Boolean(pathName(point?.path_name)) && isValidCruiseGoalId(point?.goal_id)
  })
}
