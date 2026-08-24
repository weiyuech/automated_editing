<template>
  <div class="shell" :class="{ resizing: isResizingSidebar }" :style="{ gridTemplateColumns: `${sidebarWidth}px 6px 1fr` }">
    <aside class="sidebar">
      <div class="brand">
        <div>
          <div class="brand-title">自动剪辑</div>
          <div class="brand-subtitle">机器人拍摄剪辑台</div>
        </div>
      </div>
      <nav class="nav">
        <button v-for="item in nav" :key="item.key" class="nav-item" :class="{ active: active === item.key }" @click="active = item.key">
          <span>{{ item.label }}</span>
        </button>
      </nav>
      <div class="connection" :class="connected ? 'online' : 'offline'">
        <span class="dot"></span>
        <span>{{ connected ? '后端在线' : '后端离线' }}</span>
      </div>
    </aside>
    <div class="sidebar-resizer" title="拖动调整侧栏宽度" @pointerdown="beginSidebarResize"></div>

    <main class="main">
      <header
        class="topbar"
        :class="{ 'settings-topbar-locked': active === 'settings' && !settingsAdminUnlocked }"
      >
        <div>
          <h1>{{ current.label }}</h1>
          <p v-if="current.description">{{ current.description }}</p>
        </div>
        <button class="primary" :disabled="!bridgeReady" @click="pingBackend">测试连接</button>
      </header>

      <section v-if="active === 'dashboard'" class="grid dashboard-grid">
        <StatusCard title="机器人" :value="robotConnectionLabel" :accent="robot.connected ? 'green' : 'red'" />
        <StatusCard title="录制状态" :value="robot.recording ? '录制中' : '空闲'" :accent="robot.recording ? 'amber' : 'muted'" />
        <StatusCard title="媒体库占用" :value="formatBytes(storageReport?.total_bytes || 0)" accent="blue" />
        <StatusCard title="任务" :value="`${jobs.length} 个任务`" accent="violet" />
        <Panel title="最近事件" class="wide">
          <LogList :logs="logs" />
        </Panel>
      </section>

      <section v-else-if="active === 'robot'" class="grid two">
        <Panel title="硬件状态">
          <div class="settings-status-grid compact-status">
            <div><span>连接</span><strong>{{ robotConnectionLabel }}</strong><small>{{ robot.error ? humanError(robot.error) : robotUrlPreview }}</small></div>
            <div><span>地图</span><strong>{{ robot.map_name || '未选择地图' }}</strong><small>{{ robotStatusLabel(robot.map_status) }}</small></div>
            <div><span>电量</span><strong>{{ robot.battery == null ? '-' : `${robot.battery}%` }}</strong><small>{{ robot.updated_at ? formatDate(robot.updated_at) : '等待中' }}</small></div>
            <div><span>硬件</span><strong :class="{ 'state-bad': robotHardwareFault }">{{ robotStatusLabel(robot.system_status) }}</strong><small>{{ robotStatusLabel(robot.map_mode) }}</small></div>
          </div>
          <p v-if="robotDriveBlocker" class="inline-status danger">{{ robotDriveBlocker }}</p>
          <div class="button-row">
            <button class="primary" :disabled="isRobotBusy" @click="connectRobot">连接机器人</button>
            <button :disabled="isRobotBusy" @click="refreshRobotMaps">刷新地图</button>
          </div>
          <p v-if="robotCommandStatus" class="inline-status" :class="robotCommandStatusKind">{{ robotCommandStatus }}</p>
        </Panel>

        <Panel title="地图与路径文件">
          <div class="form-stack">
            <select v-model="selectedRobotMap" class="field">
              <option value="">选择地图</option>
              <option v-for="name in robotMaps" :key="name" :value="name">{{ name }}</option>
            </select>
            <div class="button-row">
              <button class="primary" :disabled="!selectedRobotMap || isRobotBusy" @click="switchRobotMap">切换地图</button>
              <button :disabled="!selectedRobotMap || isRobotBusy" @click="refreshRobotPaths">刷新路径文件</button>
            </div>
            <p class="form-hint">
              路径文件：{{ robotPaths.length ? robotPaths.join('、') : '尚未加载' }}
            </p>
            <p class="form-hint">点位的添加、试跑和成批运行都在「拍摄」页。</p>
          </div>
        </Panel>

        <Panel title="云台与成片取景" class="wide">
          <div class="framing-layout">
            <div class="form-stack framing-controls">
              <label>偏航角：{{ cameraAngle }}</label>
              <input v-model.number="cameraAngle" type="range" min="-90" max="90" />
              <div class="button-row">
                <button :disabled="isRobotBusy || framingTestBusy" @click="setCameraAngle">设置云台偏航</button>
              </div>

              <div class="settings-group">取景测试</div>
              <p class="form-hint">录一段临时测试：云台从 -45° 缓慢扫到 45°，约等 10–15 秒。测试视频不会进入媒体库。</p>
              <div class="button-row">
                <button class="primary" :disabled="framingTestBusy || robot.recording || cruiseRunning" @click="startFramingTest">
                  {{ framingTestBusy ? '测试中，请稍候…' : (framingPreviewReady ? '重新测试' : '开始取景测试') }}
                </button>
                <button v-if="framingPreviewReady" :disabled="framingTestBusy" @click="discardFramingTest">取消并清除</button>
              </div>
              <p v-if="framingTestStatus" class="inline-status" :class="framingTestStatusKind">{{ framingTestStatus }}</p>

              <div class="settings-group">成片画幅</div>
              <p v-if="savedFramingConfigured" class="form-hint framing-saved-summary">{{ savedFramingSummary }}</p>
              <div class="framing-choice-group">
                <strong>默认居中 <small>无需测试画面</small></strong>
                <div class="segmented compact-segmented">
                  <button :class="{ active: framingMode === 'center' && framingAspectRatio === '16:9' }" @click="chooseCenterFraming('16:9')">16:9 横屏</button>
                  <button :class="{ active: framingMode === 'center' && framingAspectRatio === '9:16' }" @click="chooseCenterFraming('9:16')">9:16 竖屏</button>
                </div>
              </div>
              <div class="framing-choice-group">
                <strong>自定义位置 <small>{{ framingPreviewReady ? '拖动预览中的亮框' : '需要先完成取景测试' }}</small></strong>
                <div class="segmented compact-segmented">
                  <button :disabled="!framingPreviewReady" :class="{ active: framingMode === 'custom' && framingAspectRatio === '16:9' }" @click="chooseCustomFraming('16:9')">16:9 横屏</button>
                  <button :disabled="!framingPreviewReady" :class="{ active: framingMode === 'custom' && framingAspectRatio === '9:16' }" @click="chooseCustomFraming('9:16')">9:16 竖屏</button>
                </div>
              </div>
              <div v-if="!framingConfirming" class="button-row">
                <button class="primary" :disabled="!framingAspectRatio || !framingDraftDirty || framingSaving" @click="framingConfirming = true">保存取景偏好</button>
                <button v-if="savedFramingConfigured || framingAspectRatio" :disabled="framingSaving" @click="clearFramingPreference">清除选择</button>
              </div>
              <div v-else class="framing-confirm">
                <strong>确认替换取景偏好吗？</strong>
                <div class="button-row">
                  <button class="primary" :disabled="framingSaving" @click="confirmFramingPreference">确认保存</button>
                  <button :disabled="framingSaving" @click="framingConfirming = false">再调整一下</button>
                </div>
              </div>
            </div>

            <div v-if="framingPreviewReady" class="framing-preview-column">
              <div class="framing-preview-shell">
                <div ref="framingStageEl" class="framing-preview-stage" :style="framingStageStyle">
                  <video
                    ref="framingVideoEl"
                    :src="framingPreviewUrl"
                    autoplay
                    loop
                    muted
                    playsinline
                    @loadedmetadata="onFramingVideoMeta"
                  ></video>
                  <div v-if="framingAspectRatio" class="framing-shade" :style="framingShadeStyles.left"></div>
                  <div v-if="framingAspectRatio" class="framing-shade" :style="framingShadeStyles.right"></div>
                  <div v-if="framingAspectRatio" class="framing-shade" :style="framingShadeStyles.top"></div>
                  <div v-if="framingAspectRatio" class="framing-shade" :style="framingShadeStyles.bottom"></div>
                  <div
                    v-if="framingAspectRatio"
                    class="framing-crop-frame"
                    :class="{ dragging: framingDragging }"
                    :style="framingCropStyle"
                    @pointerdown="beginFramingDrag"
                  >
                    <span class="framing-grid vertical one"></span>
                    <span class="framing-grid vertical two"></span>
                    <span class="framing-grid horizontal one"></span>
                    <span class="framing-grid horizontal two"></span>
                    <strong>{{ framingAspectRatio }}</strong>
                  </div>
                </div>
              </div>
              <p v-if="framingAspectRatio" class="framing-preview-caption">完整测试画面仍然可见；只有亮框内会进入成片。</p>
            </div>

            <div v-else class="framing-empty-preview">
              <strong>默认居中不需要预览</strong>
              <span>可直接选择 16:9 或 9:16。只有自定义位置需要测试画面和拖动裁切框。</span>
            </div>
          </div>
        </Panel>

        <Panel title="镜头控制" class="wide">
          <div class="form-stack">
            <p class="form-hint">从起始角扫到目标角。偏航：负=右、正=左；俯仰：负=上、正=下。</p>
            <div class="gimbal-axis">
              <span class="axis-name">偏航</span>
              <label><small>起始 (−90~90)</small><input v-model.number="gimbalForm.yaw_start" class="field" type="number" min="-90" max="90" step="1" /></label>
              <label><small>目标 (−90~90)</small><input v-model.number="gimbalForm.yaw_end" class="field" type="number" min="-90" max="90" step="1" /></label>
              <label><small>速度 (2~5)</small><input v-model.number="gimbalForm.yaw_speed" class="field" type="number" min="2" max="5" step="1" /></label>
            </div>
            <div class="gimbal-axis">
              <span class="axis-name">俯仰</span>
              <label><small>起始 (−60~15)</small><input v-model.number="gimbalForm.pitch_start" class="field" type="number" min="-60" max="15" step="1" /></label>
              <label><small>目标 (−60~15)</small><input v-model.number="gimbalForm.pitch_end" class="field" type="number" min="-60" max="15" step="1" /></label>
              <label><small>速度 (2~5)</small><input v-model.number="gimbalForm.pitch_speed" class="field" type="number" min="2" max="5" step="1" /></label>
            </div>
            <div class="gimbal-axis">
              <span class="axis-name">变焦</span>
              <label><small>起始 (1~3.5)</small><input v-model.number="gimbalForm.zoom_start" class="field" type="number" min="1" max="3.5" step="0.1" /></label>
              <label><small>目标 (1~3.5)</small><input v-model.number="gimbalForm.zoom_end" class="field" type="number" min="1" max="3.5" step="0.1" /></label>
            </div>
            <div class="button-row">
              <button class="primary" :disabled="isRobotBusy || framingTestBusy" @click="sendGimbal">发送镜头控制</button>
            </div>
          </div>
        </Panel>
      </section>

      <section v-else-if="active === 'shoot'" class="grid two">
        <Panel title="原地采集" class="wide">
          <div class="form-stack">
            <div class="record-strip">
              <span class="record-dot" :class="{ live: robot.recording }"></span>
              <strong>{{ robot.recording ? '录制中' : '未录制' }}</strong>
              <span v-if="recordingElapsed" class="record-time">{{ recordingElapsed }}</span>
              <span class="record-source">{{ recordingSourceLabel }}</span>
              <div class="button-row">
                <button :disabled="robot.recording || cruiseRunning" @click="captureStart">开始原地采集</button>
                <button class="danger" :disabled="!robot.recording || cruiseRunning" @click="captureStop">停止采集</button>
                <button :disabled="cruiseRunning" @click="capturePhoto">拍照</button>
              </div>
            </div>
            <input v-model="captureTitle" class="field compact-field" placeholder="采集标题" />
            <p v-if="cruiseRunning" class="form-hint">巡游进行中，录制由巡游控制，原地采集按钮已停用。</p>
            <p v-if="robot.media_local_path" class="inline-status success">已保存到本地：{{ shortPath(robot.media_local_path) }}</p>
            <p v-else-if="robot.media_url" class="inline-status muted">机器人媒体地址：{{ robot.media_url }}</p>
            <p v-if="robot.media_sync_error" class="inline-status danger">媒体同步失败：{{ humanError(robot.media_sync_error) }}</p>
            <p v-if="captureStatus" class="inline-status" :class="captureStatusKind">{{ captureStatus }}</p>
          </div>
        </Panel>

        <Panel :title="cruiseListTitle" class="wide">
          <div class="form-stack">
            <div class="settings-pair">
              <select v-model="cruiseMap" class="field">
                <option value="">地图</option>
                <option v-for="name in robotMaps" :key="name" :value="name">{{ name }}</option>
              </select>
              <div class="button-row">
                <button :disabled="isCruiseBusy" @click="refreshCruiseMaps">刷新地图</button>
                <button :disabled="!cruiseMap || isCruiseBusy" @click="loadCruisePaths">刷新路径文件</button>
              </div>
            </div>

            <p class="form-hint">
              路径文件是设置的坐标表，清单是您自己排的拍摄顺序。
            </p>

            <div class="cruise-add-block">
              <div class="cruise-add-label">往清单里加一个点位</div>
              <div class="cruise-add">
                <select v-model="cruiseNewPath" class="field compact-field">
                  <option value="">选择路径文件</option>
                  <option v-for="name in cruisePaths" :key="name" :value="name">{{ name }}</option>
                </select>
                <input v-model.number="cruiseNewGoalId" class="field compact-field" type="number" min="0" step="1" placeholder="目标点编号" />
                <input v-model="cruiseNewGoalObject" class="field compact-field" placeholder="对准物体（留空＝不对准）" />
                <button class="primary" :disabled="!canAddCruisePoint" @click="addCruisePoint">加入清单</button>
              </div>
            </div>
            <p class="form-hint">
              目标点编号需手动填写。用「试跑」单独跑一个点、不录制，可以确认编号是否有效。留空「对准物体」表示只导航、不等待云台对准。
            </p>

            <div v-if="cruisePoints.length === 0" class="empty">
              清单还是空的。上面每按一次「加入清单」就往下面这张表里追加一行，机器人会按这个顺序依次拍。
            </div>
            <div v-else class="table cruise-table scroll-list">
              <div class="table-row header"><span>顺序</span><span>路径文件 / 目标点</span><span>对准物体</span><span>操作</span></div>
              <div v-for="(point, index) in cruisePoints" :key="index" class="table-row">
                <span>{{ index + 1 }}</span>
                <span>{{ point.path_name }} · #{{ point.goal_id }}</span>
                <span>{{ point.goal_object || '不对准（仅导航）' }}</span>
                <span class="asset-actions">
                  <button :disabled="isCruiseBusy || cruiseRunning" @click="testCruisePoint(point)">试跑</button>
                  <button :disabled="index === 0" @click="moveCruisePoint(index, -1)">上移</button>
                  <button :disabled="index === cruisePoints.length - 1" @click="moveCruisePoint(index, 1)">下移</button>
                  <button @click="removeCruisePoint(index)">删除</button>
                </span>
              </div>
            </div>
          </div>
        </Panel>

        <Panel title="巡游设置">
          <div class="form-stack settings-form">
            <p class="form-hint">开始巡游即开始录制，全程只录一次，途中不中断。</p>
            <div class="settings-pair">
              <input v-model.number="cruiseDwellMin" class="field" type="number" min="0" max="120" step="0.5" placeholder="停留最短秒数" />
              <input v-model.number="cruiseDwellMax" class="field" type="number" min="0" max="120" step="0.5" placeholder="停留最长秒数" />
            </div>
            <p class="form-hint">每到达一个点位随机停留 {{ cruiseDwellMin }}–{{ cruiseDwellMax }} 秒，避免刚到就转身、拍不到可用画面。</p>
            <input v-model.number="cruiseArrivalTimeout" class="field" type="number" min="5" max="1800" step="5" placeholder="单点到达超时秒数" />

            <label class="toggle-row"><input v-model="cruiseAutoCamerawork" type="checkbox" />自动运镜（巡游全程缓慢移动云台，避免画面太静；默认关闭）</label>
            <label class="toggle-row"><input v-model="cruiseScanEnabled" type="checkbox" />停留时云台缓慢扫视（可选，默认关闭）</label>
            <template v-if="cruiseScanEnabled">
              <div class="settings-pair">
                <select v-model="cruiseScanDirection" class="field">
                  <option value="right">向右扫视</option>
                  <option value="left">向左扫视</option>
                </select>
                <input v-model.number="cruiseScanOffset" class="field" type="number" min="1" max="60" step="1" placeholder="扫视角度" />
              </div>
              <input v-model.number="cruiseScanSpeed" class="field" type="number" min="1" max="30" step="1" placeholder="扫视速度（度 / 秒）" />
              <p class="form-hint">只在机器人停稳后扫视，去而复返回到原角度，不会边走边转。停留时间会自动不少于 {{ cruiseScanBudget }} 秒，避免扫到一半被打断。</p>
            </template>
            <p v-if="cruiseDwellWarning" class="inline-status danger">{{ cruiseDwellWarning }}</p>
          </div>
        </Panel>

        <Panel title="已保存清单">
          <div class="form-stack">
            <div class="cruise-save">
              <input v-model="cruiseRouteName" class="field compact-field" placeholder="清单名称" />
              <button class="primary" :disabled="!canSaveCruiseRoute" @click="saveCruiseRoute">保存当前</button>
            </div>
            <p class="form-hint">保存点位与设置，不含录像。最多保存 10 条，同名覆盖</p>

            <div v-if="cruiseRoutes.length === 0" class="empty">还没有保存的清单。</div>
            <div v-for="route in cruiseRoutes" :key="route.id" class="cruise-route">
              <div class="cruise-route-head">
                <strong>{{ route.name }}</strong>
                <small>{{ route.request.points.length }} 个点位 · {{ route.request.map_name || '未指定地图' }}</small>
              </div>
              <div class="asset-actions">
                <button @click="loadCruiseRoute(route)">载入</button>
                <button :disabled="isCruiseBusy" @click="validateCruiseRoute(route)">校验</button>
                <button :disabled="isCruiseBusy || cruiseRunning" @click="startCruiseRoute(route)">直接开始</button>
                <button @click="deleteCruiseRoute(route)">删除</button>
              </div>
            </div>

            <div v-if="cruiseIssues.length" class="cruise-issues">
              <p v-for="(issue, index) in cruiseIssues" :key="index" class="inline-status" :class="issue.level === 'error' ? 'danger' : 'muted'">
                {{ cruiseIssueText(issue) }}
              </p>
            </div>
          </div>
        </Panel>

        <Panel title="运行状态" class="wide">
          <div class="form-stack">
            <div class="button-row">
              <button class="primary" :disabled="!canStartCruise" @click="startCruise">开始巡游</button>
              <button :disabled="!cruiseRunning || isCruiseBusy" @click="cancelCruise">取消巡游</button>
            </div>
            <p class="form-hint">本次录制名称：{{ cruiseRouteName.trim() || '巡游' }} + 开始时间</p>
            <p v-if="manualCaptureActive" class="inline-status danger">
              原地采集进行中。巡游会自己开录，请先在上面「停止采集」再开始巡游。
            </p>
            <p v-if="cruiseStatus" class="inline-status" :class="cruiseStatusKind">{{ cruiseStatus }}</p>

            <div v-if="!cruiseRun" class="empty">尚未运行巡游。</div>
            <template v-else>
              <div class="settings-status-grid compact-status">
                <div><span>状态</span><strong>{{ cruiseRunStatusLabel }}</strong><small>{{ cruiseRun.recording ? '全程录制' : '试跑（不录制）' }}</small></div>
                <div><span>已到达</span><strong>{{ cruiseArrivedCount }} / {{ cruiseRun.segments.length }}</strong><small>{{ cruiseFailedCount }} 个失败</small></div>
                <div><span>录制文件</span><strong>{{ cruiseRun.media_url ? '已返回' : '等待中' }}</strong><small>{{ cruiseRun.media_url || '—' }}</small></div>
              </div>

              <div class="table cruise-run-table scroll-list">
                <div class="table-row header"><span>顺序</span><span>路径文件 / 目标点</span><span>状态</span><span>到达 / 停留</span></div>
                <div v-for="segment in cruiseRun.segments" :key="segment.index" class="table-row">
                  <span>{{ segment.index + 1 }}</span>
                  <span>{{ segment.path_name }} · #{{ segment.goal_id }}</span>
                  <span>{{ cruiseSegmentLabel(segment.status) }}{{ segment.scanned ? ' · 已扫视' : '' }}</span>
                  <span>{{ cruiseSegmentTiming(segment) }}</span>
                </div>
              </div>

              <p v-if="cruiseRun.error" class="inline-status danger">{{ humanError(cruiseRun.error) }}</p>
              <p v-for="note in cruiseRun.warnings || []" :key="note" class="inline-status warn">{{ note }}</p>

              <div v-if="cruiseRun.markers && cruiseRun.markers.length" class="cruise-spans">
                <strong>点位标记</strong>
                <span v-for="marker in cruiseRun.markers" :key="marker.id">{{ marker.label }} @ {{ marker.timestamp.toFixed(1) }}s</span>
              </div>
            </template>
          </div>
        </Panel>

        <Panel title="画面匹配备注" class="wide">
          <div class="form-stack">
            <textarea v-model="captureNote" class="field text" placeholder="例如：点位1：产品展示区；点位2：仓库，存放成品与货物"></textarea>
            <div class="button-row">
              <button :disabled="!captureNote.trim() || !activeSession" @click="captureNoteSend">添加备注</button>
            </div>
            <p class="form-hint">用于自动匹配旁白与点位画面，不会写入口播。</p>
            <p v-if="!activeSession" class="form-hint">先开始录制或巡游，备注会随当前素材保存。</p>
            <pre v-else>{{ activeSessionSummary }}</pre>
          </div>
        </Panel>
      </section>

      <section v-else-if="active === 'media'" class="grid vault-grid">
        <Panel title="媒体导入" class="wide">
          <div class="media-actions">
            <button class="primary" :disabled="!bridgeReady" @click="importMedia">导入本地媒体</button>
            <input v-model="downloadUrl" class="field url-field" :disabled="!bridgeReady || isDownloading" placeholder="视频或音乐直链" />
            <button :disabled="!bridgeReady || !downloadUrl.trim() || isDownloading" @click="downloadMedia">{{ isDownloading ? '导入中...' : '导入链接' }}</button>
            <button @click="refreshAll">重新扫描</button>
          </div>
          <p v-if="downloadStatus" class="inline-status" :class="downloadStatusKind">{{ downloadStatus }}</p>
          <div v-if="storageReport" class="storage-strip" :class="{ warning: storageReport.over_threshold }">
            <div>
              <strong>{{ formatBytes(storageReport.total_bytes) }}</strong> 已存于托管媒体目录
              <span>上限：{{ formatBytes(storageReport.threshold_bytes) }}</span>
            </div>
            <div class="meter"><span :style="{ width: storagePercent }"></span></div>
          </div>
        </Panel>

        <Panel title="媒体库视图" class="wide">
          <div class="segmented">
            <button :class="{ active: vaultView === 'calendar' }" @click="vaultView = 'calendar'">日历</button>
            <button :class="{ active: vaultView === 'list' }" @click="vaultView = 'list'">列表</button>
            <button :class="{ active: vaultView === 'storage' }" @click="vaultView = 'storage'">存储</button>
          </div>

          <div v-if="vaultView === 'calendar'" class="calendar-shell">
            <div class="calendar-toolbar">
              <button :disabled="!calendarCanPrev" @click="moveCalendar(-1)">上一页</button>
              <div>
                <strong>{{ calendarMonthTitle }}</strong>
                <span>{{ calendarStartLabel }} 至 {{ calendarMaxLabel }}</span>
              </div>
              <button :disabled="!calendarCanNext" @click="moveCalendar(1)">下一页</button>
            </div>
            <div class="calendar-layout">
              <div class="calendar-grid-panel">
                <div class="calendar-weekday" v-for="name in weekdayNames" :key="name">{{ name }}</div>
                <button
                  v-for="cell in calendarCells"
                  :key="cell.key"
                  class="calendar-cell"
                  :class="{ disabled: !cell.inRange, today: cell.isToday, selected: cell.dateKey === selectedCalendarDate }"
                  :disabled="!cell.inRange"
                  @click="selectedCalendarDate = cell.dateKey"
                >
                  <span>{{ cell.day }}</span>
                  <small v-if="cell.assetCount">{{ cell.assetCount }} 个素材</small>
                </button>
              </div>
              <div class="day-detail">
                <div class="day-header compact">
                  <span>{{ selectedCalendarDate }}</span>
                  <span>{{ selectedCalendarAssets.length }} 个素材</span>
                </div>
                <div v-if="selectedCalendarAssets.length === 0" class="empty">这一天暂无媒体。</div>
                <div v-else class="asset-list scroll-list">
                  <div v-for="asset in selectedCalendarAssets" :key="asset.id" class="asset-row compact">
                    <span class="role-pill" :class="asset.role">{{ roleLabel(asset.role) }}</span>
                    <span class="asset-name">{{ asset.name }}</span>
                    <span>{{ formatBytes(asset.size_bytes) }}</span>
                    <span v-if="renameTarget === asset.path" class="asset-actions">
                      <input v-model="renameValue" class="field compact-field rename-input" placeholder="新名称"
                             @keyup.enter="confirmRename" @keyup.esc="cancelRename" />
                      <button class="primary" :disabled="!renameValue.trim() || isRenaming" @click="confirmRename">确认</button>
                      <button @click="cancelRename">取消</button>
                    </span>
                    <span v-else class="asset-actions">
                      <button v-if="asset.can_rename" @click="startRename(asset.path, asset.name)">重命名</button>
                      <button :disabled="!asset.can_preview" @click="openAsset(asset)">打开</button>
                      <button @click="revealAsset(asset)">定位</button>
                      <button v-if="asset.can_forget" @click="forgetAsset(asset)">移出</button>
                      <button v-else class="danger" :disabled="!asset.can_delete" @click="trashAsset(asset)">删除</button>
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <template v-else-if="vaultView === 'list'">
            <div v-if="userVaultAssets.length > 8" class="list-filter">
              <input v-model="vaultFilter" class="field compact-field" placeholder="筛选名称或类型" />
              <small>{{ filteredVaultAssets.length }} / {{ userVaultAssets.length }}</small>
            </div>
            <div class="table vault-table scroll-list">
            <div class="table-row header"><span>类型</span><span>名称</span><span>大小</span><span>修改时间</span><span>操作</span></div>
            <div v-if="vaultRows.length === 0" class="table-row"><span>—</span><span>没有匹配的素材</span><span></span><span></span><span></span></div>
            <template v-for="row in vaultRows" :key="row.key">
              <!-- 一个成片有两个文件（带字幕的成片和无字幕的母版）时收成一行，点开才展开 -->
              <div v-if="row.type === 'group'" class="table-row group-row" @click="toggleVaultGroup(row.key)">
                <span><span class="role-pill export">{{ roleLabel('export') }}</span></span>
                <span class="group-name">
                  <span class="group-caret" :class="{ open: expandedVaultGroups[row.key] }">▸</span>
                  {{ row.name }}
                </span>
                <span>{{ formatBytes(row.size_bytes) }}</span>
                <span>{{ formatDate(row.modified_at) }}</span>
                <span class="asset-actions">
                  <span class="group-count">{{ row.members.length }} 个视频</span>
                  <button class="danger" @click.stop="trashVaultGroup(row)">全部删除</button>
                </span>
              </div>
              <template v-if="row.type === 'group' && expandedVaultGroups[row.key]">
                <div v-for="member in row.members" :key="member.id" class="table-row group-child">
                  <span></span>
                  <span class="group-child-name">{{ member.variant_label || member.name }}</span>
                  <span>{{ formatBytes(member.size_bytes) }}</span>
                  <span class="group-child-file">{{ member.name }}</span>
                  <span class="asset-actions">
                    <button :disabled="!member.can_preview" @click="openAsset(member)">打开</button>
                    <button @click="revealAsset(member)">定位</button>
                    <button class="danger" :disabled="!member.can_delete" @click="trashAsset(member)">删除</button>
                  </span>
                </div>
              </template>
              <div v-else-if="row.type === 'asset'" class="table-row">
                <span><span class="role-pill" :class="row.asset.role">{{ roleLabel(row.asset.role) }}</span></span>
                <span>{{ row.asset.name }}</span>
                <span>{{ formatBytes(row.asset.size_bytes) }}</span>
                <span>{{ formatDate(row.asset.modified_at) }}</span>
                <span v-if="renameTarget === row.asset.path" class="asset-actions">
                  <input v-model="renameValue" class="field compact-field rename-input" placeholder="新名称"
                         @keyup.enter="confirmRename" @keyup.esc="cancelRename" />
                  <button class="primary" :disabled="!renameValue.trim() || isRenaming" @click="confirmRename">确认</button>
                  <button @click="cancelRename">取消</button>
                </span>
                <span v-else class="asset-actions">
                  <button v-if="row.asset.can_rename" @click="startRename(row.asset.path, row.asset.name)">重命名</button>
                  <button :disabled="!row.asset.can_preview" @click="openAsset(row.asset)">打开</button>
                  <button @click="revealAsset(row.asset)">定位</button>
                  <button v-if="row.asset.can_forget" @click="forgetAsset(row.asset)">移出</button>
                  <button v-else class="danger" :disabled="!row.asset.can_delete" @click="trashAsset(row.asset)">删除</button>
                </span>
              </div>
            </template>
            </div>
          </template>

          <div v-else class="storage-layout">
            <div class="table storage-table">
              <div class="table-row header"><span>目录</span><span>文件</span><span>大小</span><span>路径</span></div>
              <div v-for="bucket in storageReport?.buckets || []" :key="bucket.key" class="table-row">
                <span>{{ bucketLabel(bucket.label) }}</span>
                <span>{{ bucket.file_count }}</span>
                <span>{{ formatBytes(bucket.size_bytes) }}</span>
                <span>{{ bucket.path }}</span>
              </div>
            </div>
            <div class="cleanup-block">
              <div class="cleanup-title cleanup-title-row">
                <span>可清理素材</span>
                <button :disabled="cleanupCandidates.length === 0" @click="cleanupSafeMedia">一键清理缓存/预览</button>
              </div>
              <p v-if="cleanupStatus" class="inline-status cleanup-status" :class="cleanupStatusKind">{{ cleanupStatus }}</p>
              <div v-if="cleanupCandidates.length === 0" class="empty">暂无可清理素材。</div>
              <div v-else class="scroll-list-sm">
              <div v-for="asset in cleanupCandidates" :key="asset.id" class="cleanup-row">
                <span>{{ asset.name }}</span>
                <span>{{ roleLabel(asset.role) }} · {{ formatBytes(asset.size_bytes) }}</span>
                <button class="danger" @click="trashAsset(asset)">删除</button>
              </div>
              </div>
            </div>
          </div>
        </Panel>
      </section>

      <section v-else-if="active === 'studio' || active === 'assets'" class="grid two">
        <Panel v-if="active === 'studio'" title="源视频素材">
          <div class="source-toolbar">
            <span>已选择 {{ selectedSourceIds.length }} / {{ MAX_SOURCE_VIDEOS }} 个</span>
            <button @click="selectAllSources">选择导入视频</button>
            <button @click="selectedSourceIds = []">清空</button>
          </div>
          <div v-if="sourceVideoItems.length > 8" class="list-filter">
            <input v-model="sourceFilter" class="field compact-field" placeholder="筛选文件名" />
            <small>{{ filteredSourceVideos.length }} / {{ sourceVideoItems.length }}</small>
          </div>
          <div v-if="sourceVideoItems.length === 0" class="empty">暂无导入视频。请先导入本地视频或视频直链。</div>
          <div v-else-if="filteredSourceVideos.length === 0" class="empty">没有匹配「{{ sourceFilter }}」的视频。</div>
          <div v-else class="scroll-list">
          <div v-for="item in filteredSourceVideos" :key="item.id" class="source-row">
            <template v-if="renameTarget === item.path">
              <input v-model="renameValue" class="field compact-field rename-input" placeholder="新名称"
                     @keyup.enter="confirmRename" @keyup.esc="cancelRename" />
              <span class="asset-actions">
                <button class="primary" :disabled="!renameValue.trim() || isRenaming" @click="confirmRename">确认</button>
                <button @click="cancelRename">取消</button>
              </span>
            </template>
            <template v-else>
              <label class="source-pick">
                <input
                  type="checkbox"
                  :checked="selectedSourceIds.includes(item.id)"
                  :disabled="!selectedSourceIds.includes(item.id) && selectedSourceIds.length >= MAX_SOURCE_VIDEOS"
                  @change="toggleSourceSelection(item.id, $event.target.checked)"
                />
                <span>{{ shortPath(item.path) }}</span>
              </label>
              <span class="asset-actions hover-actions">
                <button @click="startRename(item.path, item.path.split('/').pop())">重命名</button>
                <button @click="openPath(item.path)">打开</button>
                <button @click="revealPath(item.path)">定位</button>
                <button @click="forgetSourceItem(item)">移出</button>
              </span>
            </template>
          </div>
          </div>
        </Panel>

        <Panel v-if="active === 'studio'" title="自动剪辑工作台">
          <div class="form-stack">
            <!-- Everything up to the button scrolls; the button itself does not, so the action
                 is always reachable without scrolling back down to find it. -->
            <div class="workbench-scroll">
            <input v-model="editTitle" class="field" placeholder="剪辑标题" />
            <div class="duration-control">
              <span>目标时长</span>
              <div class="segmented compact-segmented">
                <button :class="{ active: targetDurationMode === '15' }" @click="targetDurationMode = '15'">15秒</button>
                <button :class="{ active: targetDurationMode === '30' }" @click="targetDurationMode = '30'">30秒</button>
                <button :class="{ active: targetDurationMode === 'custom' }" @click="targetDurationMode = 'custom'">自定义</button>
              </div>
              <input v-if="targetDurationMode === 'custom'" v-model.number="customTargetDuration" class="field" type="number" min="1" max="180" step="1" placeholder="最多180秒" />
            </div>
            <div class="form-stack">
              <div class="settings-pair">
                <input v-model.number="automationOutputCount" class="field" type="number" min="1" max="100" step="1" placeholder="输出数量（最多100）" />
                <input class="field" :value="automationPairingSummary" disabled />
              </div>
              <div class="automation-summary">
                <div><span>源视频素材</span><strong>{{ selectedSourceIds.length }} / {{ MAX_SOURCE_VIDEOS }}</strong></div>
                <div><span>音乐</span><strong>{{ selectedAutomationMusicIds.length }} / {{ MAX_AUTOMATION_ITEMS }}</strong></div>
                <div><span>旁白</span><strong>{{ selectedAutomationVoiceoverIds.length }} / {{ MAX_AUTOMATION_ITEMS }}</strong></div>
              </div>
              <div class="pool-block">
                <div class="pool-title">
                  <span>音乐池</span>
                  <button @click="selectAllMusicPool">选择全部音乐</button>
                  <button @click="selectedAutomationMusicIds = []">清空</button>
                </div>
                <div v-if="audioItems.length === 0" class="empty compact-empty">暂无音乐素材。</div>
                <div v-else class="pool-list">
                  <label v-for="item in audioItems" :key="item.id" class="pool-row">
                    <input
                      type="checkbox"
                      :checked="selectedAutomationMusicIds.includes(item.id)"
                      :disabled="!selectedAutomationMusicIds.includes(item.id) && selectedAutomationMusicIds.length >= MAX_AUTOMATION_ITEMS"
                      @change="toggleAutomationMusic(item.id, $event.target.checked)"
                    />
                    <span>{{ shortPath(item.path) }}</span>
                  </label>
                </div>
              </div>
              <div class="pool-block">
                <div class="pool-title">
                  <span>旁白池</span>
                  <button @click="selectAllVoiceoverPool">选择全部旁白</button>
                  <button @click="selectedAutomationVoiceoverIds = []">清空</button>
                </div>
                <div v-if="voiceoverItems.length === 0" class="empty compact-empty">暂无旁白素材。</div>
                <div v-else class="pool-list">
                  <label v-for="item in voiceoverItems" :key="item.id" class="pool-row">
                    <input
                      type="checkbox"
                      :checked="selectedAutomationVoiceoverIds.includes(item.id)"
                      :disabled="!selectedAutomationVoiceoverIds.includes(item.id) && selectedAutomationVoiceoverIds.length >= MAX_AUTOMATION_ITEMS"
                      @change="toggleAutomationVoiceover(item.id, $event.target.checked)"
                    />
                    <span>{{ shortPath(item.path) }}</span>
                  </label>
                </div>
              </div>
              <div class="effect-pool">
                <div class="effect-pool-head">特效池</div>
                <div class="effect-zones">
                  <div class="effect-zone intro">
                    <div class="effect-zone-head"><span>片头特效</span><button @click="selectedIntroEffectIds = []">清空</button></div>
                    <div v-if="effectItems.length === 0" class="empty compact-empty">暂无特效素材。</div>
                    <div v-else class="pool-list">
                      <label v-for="item in effectItems" :key="'in-' + item.id" class="pool-row">
                        <input
                          type="checkbox"
                          :checked="selectedIntroEffectIds.includes(item.id)"
                          :disabled="!selectedIntroEffectIds.includes(item.id) && selectedIntroEffectIds.length >= MAX_AUTOMATION_ITEMS"
                          @change="toggleIntroEffect(item.id, $event.target.checked)"
                        />
                        <span>{{ shortPath(item.path) }}</span>
                      </label>
                    </div>
                  </div>
                  <div class="effect-zone outro">
                    <div class="effect-zone-head"><span>片尾特效</span><button @click="selectedOutroEffectIds = []">清空</button></div>
                    <div v-if="effectItems.length === 0" class="empty compact-empty">暂无特效素材。</div>
                    <div v-else class="pool-list">
                      <label v-for="item in effectItems" :key="'out-' + item.id" class="pool-row">
                        <input
                          type="checkbox"
                          :checked="selectedOutroEffectIds.includes(item.id)"
                          :disabled="!selectedOutroEffectIds.includes(item.id) && selectedOutroEffectIds.length >= MAX_AUTOMATION_ITEMS"
                          @change="toggleOutroEffect(item.id, $event.target.checked)"
                        />
                        <span>{{ shortPath(item.path) }}</span>
                      </label>
                    </div>
                  </div>
                </div>
                <div class="effect-controls">
                  <div class="effect-scope"><span>应用范围</span>
                    <div class="segmented effect-scope-seg">
                      <button type="button" :class="{ active: effectScope === 'auto' }" @click="effectScope = 'auto'">由系统挑选</button>
                      <button type="button" :class="{ active: effectScope === 'all' }" @click="effectScope = 'all'">应用到全部</button>
                    </div>
                  </div>
                  <label class="effect-cover"><input v-model="effectCoverAudio" type="checkbox" /><span>旁白/音乐盖住特效</span></label>
                </div>
              </div>
            </div>
            <div class="editing-mode-switch" role="tablist" aria-label="剪辑方式">
              <button
                role="tab"
                :aria-selected="editingMode === 'smart'"
                :class="{ active: editingMode === 'smart' }"
                @click="editingMode = 'smart'"
              >
                <span>智能剪辑</span>
                <small>选择成片方向，系统自动完成</small>
              </button>
              <button
                role="tab"
                :aria-selected="editingMode === 'professional'"
                :class="{ active: editingMode === 'professional' }"
                @click="editingMode = 'professional'"
              >
                <span>专业剪辑</span>
                <small>调整剪辑策略，系统自动执行</small>
              </button>
            </div>
            <div v-if="editingMode === 'smart'" class="policy-block editorial-direction-block">
              <div class="pool-title">
                <span>成片方向</span>
                <small>只选结果倾向，具体剪法由系统完成</small>
              </div>
              <div class="editorial-presets">
                <button
                  v-for="preset in EDITORIAL_PRESETS"
                  :key="preset.value"
                  class="editorial-preset"
                  :class="{ active: editorialPreset === preset.value }"
                  @click="editorialPreset = preset.value"
                >
                  <span>{{ preset.label }}</span>
                  <small>{{ preset.description }}</small>
                </button>
              </div>
              <div class="capability-panel" aria-live="polite">
                <div class="capability-head">
                  <span>本次素材识别</span>
                  <small v-if="editingCapabilitiesLoading">分析中…</small>
                </div>
                <div class="capability-row">
                  <i :class="selectedSourceIds.length ? 'success' : 'muted'"></i>
                  <span class="capability-name">画面</span>
                  <span>{{ sourceCapabilityMessage }}</span>
                </div>
                <div class="capability-row">
                  <i :class="capabilityTone(editingCapabilities.points?.evidence)"></i>
                  <span class="capability-name">点位</span>
                  <span>{{ editingCapabilities.points?.message }}</span>
                </div>
                <div class="capability-row">
                  <i :class="capabilityTone(editingCapabilities.semantic?.evidence)"></i>
                  <span class="capability-name">旁白匹配</span>
                  <span>{{ editingCapabilities.semantic?.message }}</span>
                </div>
                <div class="capability-row">
                  <i :class="capabilityTone(editingCapabilities.music?.evidence)"></i>
                  <span class="capability-name">音乐</span>
                  <span>{{ editingCapabilities.music?.message }}</span>
                </div>
              </div>
            </div>
            <div v-else class="policy-block professional-policy-block">
              <div class="pool-title">
                <span>剪辑风格</span>
                <button @click="resetPolicies">全部自动</button>
              </div>
              <div
                v-for="(group, groupIndex) in policyGroups"
                :key="group.needs"
                class="policy-group"
                :class="{ dim: !group.live }"
                :style="{ '--tint': `var(--policy-tint-${groupIndex})` }"
              >
                <div class="policy-group-head">
                  <span class="policy-group-name">{{ group.label }}</span>
                  <span class="policy-group-hint">{{ group.status }}</span>
                </div>
                <div
                  v-for="(axis, axisIndex) in group.axes"
                  :key="axis.key"
                  class="policy-row"
                  :style="{ '--step': (axisIndex + 1) / group.axes.length }"
                >
                  <span class="policy-name">{{ axis.label }}</span>
                  <div class="segmented compact-segmented policy-choices">
                    <button
                      v-for="option in axis.options"
                      :key="option.value"
                      :class="{ active: policies[axis.key] === option.value, auto: option.value === 'auto' }"
                      :disabled="!optionApplies(axis, option)"
                      :title="policyOptionHint(axis, option)"
                      @click="policies[axis.key] = option.value"
                    >{{ option.label }}</button>
                  </div>
                </div>
              </div>
            </div>
            <label class="check-row"><input v-model="muteOriginalAudio" type="checkbox" /> 静音原视频噪声</label>
            <div class="subtitle-block">
              <label
                class="check-row"
                :class="{ disabled: !canUseSubtitles }"
                title="字幕会压进画面，导出后不能单独关掉"
              >
                <input v-model="subtitlesOn" type="checkbox" :disabled="!canUseSubtitles" />
                自动生成字幕
              </label>
              <p v-if="subtitleBlocker" class="form-hint subtitle-blocker">{{ subtitleBlocker }}</p>
              <template v-if="subtitlesOn && canUseSubtitles">
                <div class="policy-row">
                  <span class="policy-name">字体</span>
                  <div class="segmented compact-segmented policy-choices">
                    <button
                      v-for="font in installedSubtitleFonts"
                      :key="font.key"
                      :class="{ active: subtitleFont === font.key }"
                      :title="font.note"
                      :style="font.preview ? { fontFamily: `'ave-${font.key}', sans-serif` } : null"
                      @click="subtitleFont = font.key"
                    >{{ font.label }}</button>
                  </div>
                </div>
                <div v-if="subtitleFontPreview" class="subtitle-preview">
                  <span class="subtitle-preview-text" :style="subtitlePreviewStyle">机器人从大厅出发</span>
                </div>
                <div class="policy-row">
                  <span class="policy-name">字号</span>
                  <div class="segmented compact-segmented policy-choices">
                    <button
                      v-for="option in SUBTITLE_SIZES"
                      :key="option.value"
                      :class="{ active: subtitleSize === option.value }"
                      @click="subtitleSize = option.value"
                    >{{ option.label }}</button>
                  </div>
                </div>
              </template>
            </div>
            </div>
            <button
              class="primary"
              :disabled="selectedSourceIds.length === 0 || isCreatingJob"
              @click="createAutomationJobs"
            >
              {{ isCreatingJob ? '创建中...' : '创建剪辑任务' }}
            </button>
            <p v-if="jobStatus" class="inline-status" :class="jobStatusKind">{{ jobStatus }}</p>
          </div>
        </Panel>
        <Panel v-if="active === 'assets'" title="旁白制作" class="wide">
          <div class="voiceover-layout">
            <div class="form-stack">
              <input v-model="voiceoverTitle" class="field" placeholder="旁白素材名称" />
              <textarea v-model="voiceoverText" class="field text voice-text" placeholder="脚本文本或大模型提示词"></textarea>
              <div class="voice-options">
                <label class="check-row"><input v-model="voiceoverUseLlm" type="checkbox" /> 大模型辅助</label>
                <label class="check-row"><span>预计时长（秒）</span>
                  <input v-model.number="voiceoverSeconds" class="field compact-field" type="number" min="1" max="600" step="1" placeholder="可留空" :disabled="!voiceoverUseLlm" />
                </label>
              </div>
              <p v-if="voiceoverSeconds > 0 && !voiceoverUseLlm" class="form-hint">预计时长需配合「大模型辅助」使用。</p>
              <div class="voice-actions">
                <button class="primary" :disabled="!canGenerateVoiceover || isGeneratingVoiceover" @click="generateVoiceover">{{ isGeneratingVoiceover ? '生成中...' : '生成旁白' }}</button>
                <span class="quota-hint">{{ ttsQuotaText }}</span>
              </div>
              <p v-if="voiceoverStatus" class="inline-status" :class="voiceoverStatusKind">{{ voiceoverStatus }}</p>
            </div>
            <div class="voice-asset-list">
              <div v-if="ttsAssets.length === 0" class="empty compact-empty">暂无旁白素材。</div>
              <div v-for="asset in ttsAssets" :key="asset.id" class="voice-asset">
                <template v-if="renameTarget === asset.audio_path">
                  <input v-model="renameValue" class="field compact-field rename-input" placeholder="新名称"
                         @keyup.enter="confirmRename" @keyup.esc="cancelRename" />
                  <div class="voice-asset-actions">
                    <button class="primary" :disabled="!renameValue.trim() || isRenaming" @click="confirmRename">确认</button>
                    <button @click="cancelRename">取消</button>
                  </div>
                </template>
                <template v-else>
                  <div class="voice-select">
                    <span>{{ asset.name }}</span>
                    <small>{{ formatMs(asset.duration_ms) }}</small>
                  </div>
                  <div class="voice-asset-actions hover-actions">
                    <button @click="startRename(asset.audio_path, asset.name)">重命名</button>
                    <button @click="openPath(asset.audio_path)">打开</button>
                    <button @click="revealPath(asset.audio_path)">定位</button>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </Panel>
        <Panel v-if="active === 'assets'" title="特效制作" class="wide">
          <div class="effect-layout">
            <div class="form-stack">
              <div class="segmented compact-segmented">
                <button :class="{ active: seedanceOutput === 'video' }" @click="seedanceOutput = 'video'">生成视频</button>
                <button :class="{ active: seedanceOutput === 'image' }" @click="seedanceOutput = 'image'">生成图片</button>
              </div>
              <p class="form-hint">{{ seedanceOutputHint }}</p>
              <div class="segmented compact-segmented">
                <button :class="{ active: seedanceSourceMode === 'image' }" @click="seedanceSourceMode = 'image'">来源：图片</button>
                <button :class="{ active: seedanceSourceMode === 'stamp' }" @click="seedanceSourceMode = 'stamp'">来源：视频帧</button>
              </div>
              <input v-model="seedanceTitle" class="field" :placeholder="seedanceOutput === 'image' ? '图片名称（可留空）' : '视频名称（可留空）'" />
              <select v-if="seedanceSourceMode === 'image'" v-model="selectedSeedanceImageId" class="field">
                <option value="">不选来源（只用提示词生成）</option>
                <option v-for="item in imageItems" :key="item.id" :value="item.id">{{ shortPath(item.path) }}</option>
              </select>
              <template v-else>
                <select v-model="selectedSeedanceVideoId" class="field">
                  <option value="">不选来源（只用提示词生成）</option>
                  <option v-for="item in sourceVideoItems" :key="item.id" :value="item.id">{{ shortPath(item.path) }}</option>
                </select>
                <div v-if="selectedSeedanceVideoId" class="frame-picker">
                  <video
                    ref="frameVideoEl"
                    class="frame-video"
                    :src="frameVideoSrc"
                    controls
                    preload="metadata"
                    @loadedmetadata="onFrameMeta"
                    @timeupdate="onFrameTime"
                  ></video>
                  <div class="frame-row">
                    <button :disabled="!frameDuration" @click="stepFrame(-1)">◀</button>
                    <button :disabled="!frameDuration" @click="stepFrame(1)">▶</button>
                    <span class="frame-time">{{ frameHead.toFixed(2) }}s / {{ frameDuration.toFixed(2) }}s</span>
                    <button class="primary" :disabled="!frameDuration || isGrabbingFrame" @click="grabFrame">
                      {{ isGrabbingFrame ? '取帧中...' : '用这一帧' }}
                    </button>
                  </div>
                  <div v-if="grabbedFrame" class="frame-chosen">
                    <img :src="grabbedFrameSrc" alt="" />
                    <div>
                      <strong>已选 {{ Number(grabbedFrame.timestamp_seconds).toFixed(2) }}s</strong>
                      <button @click="clearGrabbedFrame">清除</button>
                    </div>
                  </div>
                  <p v-else class="form-hint">拖动进度条选好画面，再点「用这一帧」。</p>
                </div>
              </template>
              <textarea v-model="seedancePrompt" class="field text effect-text" placeholder="特效提示词"></textarea>
              <div class="settings-pair">
                <input v-if="seedanceOutput === 'video'" v-model.number="seedanceDuration" class="field" type="number" min="2" max="15" step="1" placeholder="特效秒数（2–15）" />
                <input class="field" :value="seedanceQuotaText" disabled />
              </div>
              <p v-if="seedanceOutput === 'video'" class="form-hint">按默认时长估算；实际可生成次数与每次生成时长有关。</p>
              <button class="primary" :disabled="!canGenerateSeedance" @click="generateSeedanceEffect">{{ isGeneratingSeedance ? '提交中...' : '生成特效' }}</button>
              <p v-if="seedanceStatus" class="inline-status" :class="seedanceStatusKind">{{ seedanceStatus }}</p>
            </div>
            <div class="effect-asset-list">
              <div v-if="seedanceAssets.length === 0 && effectItems.length === 0" class="empty compact-empty">暂无特效素材。</div>
              <div v-for="asset in seedanceAssets" :key="asset.id" class="effect-asset" :class="asset.status">
                <template v-if="renameTarget === asset.output_path">
                  <input v-model="renameValue" class="field compact-field rename-input" placeholder="新名称"
                         @keyup.enter="confirmRename" @keyup.esc="cancelRename" />
                  <div class="voice-asset-actions">
                    <button class="primary" :disabled="!renameValue.trim() || isRenaming" @click="confirmRename">确认</button>
                    <button @click="cancelRename">取消</button>
                  </div>
                </template>
                <template v-else>
                <div>
                  <span>{{ asset.name }}</span>
                  <small>{{ seedanceAssetStatusLabel(asset.status) }}{{ asset.kind === 'video' ? ' · ' + formatMs(asset.duration_seconds * 1000) : ' · 图片' }}</small>
                  <small v-if="asset.error" class="effect-error">{{ humanError(asset.error) }}</small>
                </div>
                <div class="voice-asset-actions hover-actions">
                  <button :disabled="asset.status !== 'succeeded'" @click="startRename(asset.output_path, asset.name)">重命名</button>
                  <button :disabled="asset.status !== 'succeeded'" @click="openPath(asset.output_path)">打开</button>
                  <button @click="revealPath(asset.output_path)">定位</button>
                  <button class="danger" @click="deleteSeedanceAsset(asset)">删除</button>
                </div>
                </template>
              </div>
            </div>
          </div>
        </Panel>

        <Panel v-if="active === 'studio'" title="手动微调" class="wide">
          <div class="tune">
            <div class="tune-row">
              <select v-model="tuneSourceId" class="field">
                <option value="">选择素材（导入、成片或特效）</option>
                <optgroup label="导入素材">
                  <option v-for="item in tuneImportedSources" :key="item.id" :value="item.id">{{ shortPath(item.path) }}</option>
                </optgroup>
                <optgroup label="已导出成片">
                  <option v-for="item in tuneExportSources" :key="item.id" :value="item.id">{{ shortPath(item.path) }}</option>
                </optgroup>
                <optgroup label="特效">
                  <option v-for="item in tuneEffectSources" :key="item.id" :value="item.id">{{ shortPath(item.path) }}</option>
                </optgroup>
              </select>
            </div>

            <div class="tune-stage">
              <video
                v-show="!tuneShowStill"
                ref="tuneVideoEl"
                class="tune-video"
                :src="tuneVideoSrc"
                controls
                preload="metadata"
                @loadedmetadata="onTuneMeta"
                @timeupdate="onTuneTime"
              ></video>
              <img v-if="tuneShowStill" class="tune-still" :src="tuneStillSrc" alt="" />
            </div>
            <p v-if="tuneModeLabel" class="form-hint">{{ tuneModeLabel }}</p>

            <template v-if="tuneMode === 'source' && tuneSourceId && !tuneSourceIsImage">
              <div class="tune-bar" @click="scrubTo($event)">
                <div class="tune-bar-sel" :style="tuneSelectionStyle"></div>
                <div class="tune-bar-head" :style="{ left: tuneHeadPercent + '%' }"></div>
              </div>
              <div class="tune-step">
                <span class="tune-step-label">选哪一段</span>
                <div class="tune-step-actions">
                  <button @click="markIn">起点 {{ tuneIn.toFixed(1) }}s</button>
                  <button @click="markOut">终点 {{ tuneOut.toFixed(1) }}s</button>
                  <button @click="markWhole">整段</button>
                  <span class="tune-len">已选 {{ tuneSelectionLength.toFixed(1) }} 秒</span>
                </div>
              </div>
              <p class="form-hint tune-step-hint">拖动进度条到想要的位置，再按「起点 / 终点」。</p>
            </template>
            <div v-if="tuneMode === 'source' && tuneSourceIsImage" class="tune-step">
              <span class="tune-step-label">图片时长</span>
              <div class="tune-step-actions">
                <input v-model.number="tuneImageDuration" class="field compact-field tune-dur" type="number" min="0.2" max="60" step="0.1" />
                <span class="tune-len">秒</span>
              </div>
            </div>

            <div v-if="tuneMode === 'source' && tuneSourceId" class="tune-step">
              <span class="tune-step-label">放到哪里</span>
              <div class="tune-step-actions">
                <button :disabled="!canPlaceTune" @click="placeAtStart">开头</button>
                <button :disabled="!canPlaceTune" @click="placeAtEnd">结尾</button>
                <button :disabled="!canPlaceTune || !tuneClips.length" @click="placeOverwrite">
                  从成片 {{ tunePlayhead.toFixed(1) }}s 覆盖
                </button>
              </div>
            </div>
            <div v-if="tuneClips.length" class="tune-step">
              <span class="tune-step-label">覆盖落点</span>
              <div class="tune-scrub" @click="setPlayhead($event)">
                <div class="tune-scrub-fill" :style="{ width: tunePlayheadPercent + '%' }"></div>
                <div class="tune-scrub-head" :style="{ left: tunePlayheadPercent + '%' }"></div>
              </div>
            </div>

            <div class="tune-timeline-head">
              <span>时间线</span>
              <span>{{ tuneClips.length }} 段 · 共 {{ tuneTotal.toFixed(1) }}s</span>
            </div>
            <div v-if="tuneClips.length === 0" class="empty">时间线是空的。选一段素材，按「加到结尾」。</div>
            <div v-else class="tune-track">
              <div
                v-for="(clip, index) in tuneClips"
                :key="clip.uid"
                class="tune-block"
                :class="{ active: tuneMode === 'result' && tunePreviewIndex === index, dragging: tuneDragIndex === index, 'is-effect': clip.is_effect || clip.kind === 'image' }"
                :style="{ flexGrow: clip.duration }"
                draggable="true"
                @dragstart="tuneDragIndex = index"
                @dragover.prevent
                @drop="dropTuneClip(index)"
                @dragend="tuneDragIndex = -1"
                :title="`${clip.name} ${clip.start.toFixed(1)}s 起 ${clip.duration.toFixed(1)}s`"
              >
                <span class="tune-block-name">{{ index + 1 }}. {{ clip.name }}</span>
                <span class="tune-block-time">{{ clip.duration.toFixed(1) }}s</span>
                <button class="tune-block-x" @click.stop="removeTuneClip(index)">×</button>
              </div>
            </div>
            <p v-if="tuneClips.length" class="form-hint">拖动方块可以调整顺序。</p>

            <div class="tune-row">
              <label class="check-row"><input v-model="tuneKeepSound" type="checkbox" /> 保留原声</label>
              <span v-if="tuneBed" class="tune-len">声音来自 {{ tuneBedName }}</span>
            </div>
            <div v-if="tuneHasVideoEffects" class="tune-effect-audio">
              <div class="tune-row">
                <label class="check-row"><input v-model="tuneKeepEffectSound" type="checkbox" /> 保留特效声音</label>
                <span class="tune-len">{{ tuneEffectAudioSummary }}</span>
              </div>
              <div v-if="tuneKeepEffectSound" class="segmented compact-segmented tune-audio-modes">
                <button :class="{ active: tuneEffectSoundMode === 'auto' }" @click="tuneEffectSoundMode = 'auto'">自动</button>
                <button :class="{ active: tuneEffectSoundMode === 'ducked' }" @click="tuneEffectSoundMode = 'ducked'">轻声 30%</button>
                <button :class="{ active: tuneEffectSoundMode === 'full' }" @click="tuneEffectSoundMode = 'full'">原音 100%</button>
              </div>
              <p class="form-hint">自动模式：保留的原声含旁白时，特效声音降到 30%；没有旁白或已关闭原声时，特效声音保持 100%。</p>
            </div>
            <p v-if="tuneSubtitleNote" class="inline-status tune-subtitle-note" :class="tuneSubtitleNoteKind">
              {{ tuneSubtitleNote }}
            </p>
            <p v-if="tuneSubtitles && !tuneKeepSound" class="inline-status warn tune-subtitle-note">
              关掉原声后字幕也会一起去掉——字幕是跟着声音走的，没有声音就没有对齐的依据。
            </p>
            <div class="tune-row">
              <button :disabled="!tuneClips.length" @click="playResult">▶ 预览成片</button>
              <button :disabled="tuneMode !== 'result'" @click="backToSource">回到原片</button>
              <button class="primary" :disabled="!tuneClips.length || isRenderingTimeline" @click="renderTune">{{ isRenderingTimeline ? '提交中...' : '渲染成 MP4' }}</button>
              <button :disabled="!tuneUndoStack.length" @click="undoTune">撤销</button>
              <button :disabled="!tuneClips.length" @click="clearTune">清空</button>
            </div>
          </div>
        </Panel>
      </section>

      <section v-else-if="active === 'queue'" class="grid">
        <Panel title="渲染队列" class="wide">
          <div class="table queue-table scroll-list">
            <div class="table-row header"><span>状态</span><span>进度</span><span>消息</span><span>输出</span></div>
            <div v-for="job in jobs" :key="job.id" class="table-row">
              <span>{{ jobStatusLabel(job.status) }}</span>
              <span>{{ Math.round(job.progress * 100) }}%</span>
              <span>
                {{ jobMessageLabel(job.error || job.message) }}
                <small v-for="note in job.warnings || []" :key="note" class="job-warning">{{ note }}</small>
              </span>
              <span>{{ job.result_path || '-' }}</span>
            </div>
          </div>
        </Panel>
      </section>

      <section v-else-if="active === 'settings'" class="settings-access-shell">
        <div v-if="settingsAdminUnlocked" class="settings-access-toolbar">
          <span>管理员模式</span>
          <button type="button" @click="lockSettings">锁定设置</button>
        </div>
        <div
          class="grid two settings-protected-content"
          :class="{ locked: !settingsAdminUnlocked }"
          :inert="!settingsAdminUnlocked"
          :aria-hidden="!settingsAdminUnlocked"
        >
        <Panel title="大模型服务">
          <div class="form-stack settings-form">
            <div class="settings-scroll">
            <label class="toggle-row"><input v-model="settingsForm.llm.enabled" type="checkbox" /> 启用大模型辅助</label>
            <label class="field-row"><span>服务商</span>
              <select v-model="settingsForm.llm.provider" class="field"><option value="doubao">豆包</option></select>
            </label>
            <label class="field-row"><span>模型 / 接入点 ID</span>
              <input v-model="settingsForm.llm.model" class="field" placeholder="例如 doubao-pro-32k-…" />
            </label>
            <label class="field-row"><span>API 密钥</span>
              <input v-model="settingsForm.llm.api_key" class="field" type="password" :placeholder="secretPlaceholder(settings?.llm?.api_key, '大模型接口密钥')" />
              <small class="secret-state" :class="{ ok: settings?.llm?.api_key?.configured }">{{ secretState(settings?.llm?.api_key) }}</small>
            </label>
            <label class="field-row"><span>超时时间（毫秒）</span>
              <input v-model.number="settingsForm.llm.timeout_ms" class="field" type="number" min="1000" max="120000" step="1000" />
            </label>
            </div>
            <div class="button-row">
              <button class="primary" :disabled="isSavingSettings" @click="saveSettings">保存设置</button>
              <button :disabled="isTestingLlm" @click="testLlm">{{ isTestingLlm ? '测试中...' : '测试大模型' }}</button>
            </div>
          </div>
        </Panel>
        <Panel title="语音合成服务">
          <div class="form-stack settings-form">
            <div class="settings-scroll">
            <label class="toggle-row"><input v-model="settingsForm.tts.enabled" type="checkbox" /> 启用定时语音合成</label>
            <label class="field-row"><span>服务商</span>
              <select v-model="settingsForm.tts.provider" class="field"><option value="volcengine_sync">火山引擎同步</option></select>
            </label>
            <label class="field-row"><span>应用 ID（App ID）</span>
              <input v-model="settingsForm.tts.app_id" class="field" type="password" :placeholder="secretPlaceholder(settings?.tts?.app_id, '火山引擎应用 ID')" />
              <small class="secret-state" :class="{ ok: settings?.tts?.app_id?.configured }">{{ secretState(settings?.tts?.app_id) }}</small>
            </label>
            <label class="field-row"><span>访问令牌（Access Token）</span>
              <input v-model="settingsForm.tts.access_token" class="field" type="password" :placeholder="secretPlaceholder(settings?.tts?.access_token, '火山引擎访问令牌')" />
              <small class="secret-state" :class="{ ok: settings?.tts?.access_token?.configured }">{{ secretState(settings?.tts?.access_token) }}</small>
            </label>
            <div class="settings-pair">
              <label class="field-row"><span>音色</span>
                <input v-model="settingsForm.tts.voice_type" class="field" placeholder="例如 BV001_streaming" />
              </label>
              <label class="field-row"><span>集群</span>
                <input v-model="settingsForm.tts.cluster" class="field" placeholder="例如 volcano_tts" />
              </label>
            </div>
            <div class="settings-pair">
              <label class="field-row"><span>音频格式</span>
                <select v-model="settingsForm.tts.encoding" class="field"><option value="mp3">MP3</option><option value="wav">WAV</option></select>
              </label>
              <label class="field-row"><span>语速（1 为正常）</span>
                <input v-model.number="settingsForm.tts.speed_ratio" class="field" type="number" min="0.2" max="3" step="0.05" />
              </label>
            </div>
            <label class="field-row"><span>每日旁白上限（次）</span>
              <input v-model.number="settingsForm.tts.daily_limit" class="field" type="number" min="1" max="1000" step="1" />
              <small class="secret-state">每天最多生成的旁白条数（含大模型辅助），达到后需到次日或调高上限</small>
            </label>
            </div>
            <div class="button-row">
              <button class="primary" :disabled="isSavingSettings" @click="saveSettings">保存设置</button>
              <button :disabled="isTestingTts" @click="testTts">{{ isTestingTts ? '测试中...' : '测试语音时间戳' }}</button>
            </div>
          </div>
        </Panel>
        <Panel title="特效服务">
          <div class="form-stack settings-form">
            <div class="settings-scroll">
            <label class="toggle-row"><input v-model="settingsForm.seedance.enabled" type="checkbox" /> 启用特效生成</label>

            <label class="field-row"><span>服务商</span>
              <select v-model="settingsForm.seedance.provider" class="field"><option value="volcengine_ark">火山方舟</option></select>
            </label>

            <div class="settings-group">模型</div>
            <label class="field-row"><span>视频模型（Seedance，图片→视频）</span>
              <input v-model="settingsForm.seedance.model" class="field" :placeholder="configuredPlaceholder(settings?.seedance?.model, '例如 doubao-seedance-2-0-mini-260615')" />
            </label>
            <label class="field-row"><span>图片模型（Seedream，生成 / 修改图片）</span>
              <input v-model="settingsForm.seedance.image_model" class="field" :placeholder="configuredPlaceholder(settings?.seedance?.image_model, '例如 doubao-seedream-5-0-260128')" />
            </label>
            <label class="field-row"><span>API 密钥</span>
              <input v-model="settingsForm.seedance.api_key" class="field" type="password" :placeholder="secretPlaceholder(settings?.seedance?.api_key, '方舟 API Key')" />
              <small class="secret-state" :class="{ ok: settings?.seedance?.api_key?.configured }">{{ secretState(settings?.seedance?.api_key) }}</small>
            </label>
            <label class="field-row"><span>方舟 API 地址</span>
              <input v-model="settingsForm.seedance.base_url" class="field" placeholder="https://ark.cn-beijing.volces.com/api/v3" />
            </label>

            <div class="settings-group">图片暂存（对象存储 TOS）</div>
            <div class="settings-pair">
              <label class="field-row"><span>桶名</span>
                <input v-model="settingsForm.seedance.tos_bucket" class="field" placeholder="例如 bucket-de-seedance" />
              </label>
              <label class="field-row"><span>地域</span>
                <input v-model="settingsForm.seedance.tos_region" class="field" placeholder="例如 cn-beijing" />
              </label>
            </div>
            <label class="field-row"><span>Endpoint</span>
              <input v-model="settingsForm.seedance.tos_endpoint" class="field" placeholder="例如 tos-cn-beijing.volces.com" />
            </label>
            <div class="settings-pair">
              <label class="field-row"><span>Access Key ID</span>
                <input v-model="settingsForm.seedance.tos_access_key_id" class="field" type="password" :placeholder="secretPlaceholder(settings?.seedance?.tos_access_key_id, 'AKLT…')" />
              <small class="secret-state" :class="{ ok: settings?.seedance?.tos_access_key_id?.configured }">{{ secretState(settings?.seedance?.tos_access_key_id) }}</small>
              </label>
              <label class="field-row"><span>Secret Access Key</span>
                <input v-model="settingsForm.seedance.tos_secret_access_key" class="field" type="password" :placeholder="secretPlaceholder(settings?.seedance?.tos_secret_access_key, '密钥')" />
              <small class="secret-state" :class="{ ok: settings?.seedance?.tos_secret_access_key?.configured }">{{ secretState(settings?.seedance?.tos_secret_access_key) }}</small>
              </label>
            </div>
            <label class="field-row"><span>临时安全令牌（可留空）</span>
              <input v-model="settingsForm.seedance.tos_security_token" class="field" type="password" :placeholder="secretPlaceholder(settings?.seedance?.tos_security_token, '一般不用填')" />
            </label>
            <div class="settings-pair">
              <label class="field-row"><span>暂存目录</span>
                <input v-model="settingsForm.seedance.tos_object_prefix" class="field" placeholder="seedance/staging" />
              </label>
              <label class="field-row"><span>签名有效期（秒）</span>
                <input v-model.number="settingsForm.seedance.tos_url_expires_seconds" class="field" type="number" min="60" max="2592000" step="60" />
              </label>
            </div>

            <div class="settings-group">生成参数</div>
            <label class="field-row"><span>图片尺寸</span>
              <input v-model="settingsForm.seedance.image_size" class="field" placeholder="留空＝跟随原图；或填 2k / 3k / 4k / 1024x768" />
            </label>
            <div class="settings-pair">
              <label class="field-row"><span>每日生成上限</span>
                <input v-model.number="settingsForm.seedance.daily_limit" class="field" type="number" min="1" max="100" step="1" />
              </label>
              <label class="field-row"><span>默认视频时长（秒）</span>
                <input v-model.number="settingsForm.seedance.default_duration_seconds" class="field" type="number" min="2" max="15" step="1" />
              </label>
            </div>
            <p class="form-hint">默认时长用于估算每日额度；单条视频越长，可生成次数越少。</p>
            <div class="settings-pair">
              <label class="field-row"><span>视频分辨率</span>
                <input v-model="settingsForm.seedance.resolution" class="field" placeholder="720p" />
              </label>
              <label class="field-row"><span>视频比例</span>
                <input v-model="settingsForm.seedance.ratio" class="field" placeholder="16:9" />
              </label>
            </div>
            </div>
            <div class="button-row">
              <button class="primary" :disabled="isSavingSettings" @click="saveSettings">保存设置</button>
              <button :disabled="isTestingSeedance" @click="testSeedance">{{ isTestingSeedance ? '测试中...' : '测试特效连接' }}</button>
            </div>
          </div>
        </Panel>
        <Panel title="机器人硬件">
          <div class="form-stack settings-form">
            <div class="settings-scroll">
            <label class="field-row"><span>机器人 WebSocket 地址</span>
              <input v-model="settingsForm.robot.websocket_url" class="field" placeholder="ws://10.73.2.199:8765" />
            </label>
            <label class="field-row"><span>每日自动剪辑上限（条）</span>
              <input v-model.number="settingsForm.automation.daily_output_limit" class="field" type="number" min="1" max="10000" step="1" />
            </label>
            </div>
            <div class="button-row">
              <button class="primary" :disabled="isSavingSettings" @click="saveSettings">保存设置</button>
              <button :disabled="isRobotBusy" @click="connectRobot">连接机器人</button>
            </div>
          </div>
        </Panel>
        <Panel title="服务状态" class="wide">
          <div class="settings-status-grid">
            <div><span>大模型</span><strong>{{ settings?.llm?.api_key?.configured ? '已配置' : '缺少密钥' }}</strong><small>{{ settings?.llm?.model || '暂无模型' }}</small></div>
            <div><span>语音合成</span><strong>{{ settings?.tts?.access_token?.configured ? '已配置' : '缺少令牌' }}</strong><small>{{ settings?.tts?.voice_type || '暂无音色' }}</small></div>
            <div><span>特效</span><strong>{{ seedanceConfiguredLabel }}</strong><small>{{ seedanceSettingsSubtitle }}</small></div>
            <div><span>机器人</span><strong>{{ robotConnectionLabel }}</strong><small>{{ settings?.robot?.websocket_url || '未填写硬件地址' }}</small></div>
            <div><span>生效</span><strong>无需重启</strong><small>服务和机器人设置会立即生效</small></div>
          </div>
          <p v-if="settingsStatus" class="inline-status" :class="settingsStatusKind">{{ settingsStatus }}</p>
        </Panel>
        </div>
        <div v-if="!settingsAdminUnlocked" class="settings-unlock-layer">
          <form class="settings-unlock-card" @submit.prevent="unlockSettings">
            <strong>管理员验证</strong>
            <p>请输入管理员账号和密码以查看设置。</p>
            <label class="field-row">
              <span>管理员账号</span>
              <input
                v-model.trim="settingsAdminUsername"
                class="field"
                autocomplete="username"
                placeholder="管理员账号"
                autofocus
              />
            </label>
            <label class="field-row">
              <span>密码</span>
              <input
                v-model="settingsAdminPassword"
                class="field"
                type="password"
                autocomplete="current-password"
                placeholder="密码"
              />
            </label>
            <button
              class="primary"
              type="submit"
              :disabled="isUnlockingSettings || !settingsAdminUsername || !settingsAdminPassword"
            >
              {{ isUnlockingSettings ? '验证中...' : '进入设置' }}
            </button>
            <p v-if="settingsAdminStatus" class="inline-status" :class="settingsAdminStatusKind">
              {{ settingsAdminStatus }}
            </p>
          </form>
        </div>
      </section>
    </main>
    <div v-if="showFramingSetupPrompt" class="modal-backdrop" @click.self="showFramingSetupPrompt = false">
      <div class="modal-card framing-setup-modal">
        <strong>尚未选择固定画幅</strong>
        <p>可到「镜头设置」直接选择居中 16:9 / 9:16；只有自定义位置需要取景测试。</p>
        <p class="form-hint">如果继续，当前这批会保持原始画面和尺寸，不会自动裁成 16:9。</p>
        <div class="button-row">
          <button class="primary" @click="goToFramingSetup">去镜头设置</button>
          <button @click="continueWithoutFramingPreference">仍然继续</button>
          <button @click="showFramingSetupPrompt = false">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import StatusCard from './components/StatusCard.vue'
import Panel from './components/Panel.vue'
import LogList from './components/LogList.vue'

const nav = [
  { key: 'dashboard', label: '总览', icon: '01', description: '查看系统状态和最近活动。' },
  { key: 'robot', label: '镜头设置', icon: '02', description: '' },
  { key: 'shoot', label: '拍摄', icon: '03', description: '原地采集，或按清单顺序巡游拍摄。' },
  { key: 'media', label: '媒体库', icon: '04', description: '查看日历、存储、导入、导出和清理。' },
  { key: 'assets', label: '资产制作', icon: '05', description: '制作旁白和图片、视频特效。' },
  { key: 'studio', label: '剪辑台', icon: '06', description: '选择素材，创建自动剪辑或进行手动微调。' },
  { key: 'queue', label: '渲染队列', icon: '07', description: '跟踪导出任务和 FFmpeg 进度。' },
  { key: 'settings', label: '设置', icon: '08', description: '配置服务、剪辑能力和机器人硬件连接。' }
]

const MAX_SOURCE_VIDEOS = 20
const MAX_AUTOMATION_ITEMS = 100

const active = ref('dashboard')
const current = computed(() => nav.find((item) => item.key === active.value) || nav[0])
const sidebarWidth = ref(initialSidebarWidth())
const isResizingSidebar = ref(false)
const connected = ref(false)
const apiConfig = ref(null)
const bridgeReady = computed(() => Boolean(apiConfig.value))
const ws = ref(null)
const logs = ref([])
const robot = ref({ connected: false, recording: false, camera_angle: 0 })
const robotMaps = ref([])
const robotPaths = ref([])
const selectedRobotMap = ref('')
const robotCommandStatus = ref('')
const robotCommandStatusKind = ref('muted')
const isRobotBusy = ref(false)
const cruiseMap = ref('')
const cruisePaths = ref([])
const cruisePoints = ref([])
const cruiseNewPath = ref('')
const cruiseNewGoalId = ref(1)
const cruiseNewGoalObject = ref('')
const cruiseDwellMin = ref(5)
const cruiseDwellMax = ref(10)
const cruiseArrivalTimeout = ref(180)
const cruiseAutoCamerawork = ref(false)
const cruiseScanEnabled = ref(false)
const cruiseScanDirection = ref('right')
const cruiseScanOffset = ref(15)
const cruiseScanSpeed = ref(5)
const cruiseRoutes = ref([])
const cruiseRouteName = ref('')
const cruiseRun = ref(null)
const cruiseIssues = ref([])
const cruiseStatus = ref('')
const cruiseStatusKind = ref('muted')
const isCruiseBusy = ref(false)
const MAX_CRUISE_POINTS = 200
const media = ref([])
const jobs = ref([])
const vaultAssets = ref([])
const calendarDays = ref([])
const storageReport = ref(null)
const settings = ref(null)
const ttsAssets = ref([])
const seedanceAssets = ref([])
const seedanceQuota = ref(null)
const ttsQuota = ref(null)
const vaultView = ref('calendar')
const todayDate = startOfDay(new Date())
const calendarStartDate = startOfMonth(todayDate)
const calendarMaxDate = endOfMonth(addYears(calendarStartDate, 10))
const calendarCursor = ref(calendarStartDate)
const selectedCalendarDate = ref(formatDateKey(todayDate))
const activeSession = ref(null)
const captureTitle = ref('')
const captureNote = ref('')
const captureStatus = ref('')
const captureStatusKind = ref('muted')
const cameraAngle = ref(0)
const gimbalForm = ref({
  yaw_start: 0, yaw_end: 0, yaw_speed: 5,
  pitch_start: 0, pitch_end: 0, pitch_speed: 5,
  zoom_start: 1, zoom_end: 1
})
const framingTest = ref({ running: false, ready: false, preview_id: '' })
const framingTestBusy = ref(false)
const framingTestStatus = ref('')
const framingTestStatusKind = ref('muted')
const framingAspectRatio = ref(null)
const framingMode = ref('center')
const framingCropX = ref(0.5)
const framingCropY = ref(0.5)
const framingVideoEl = ref(null)
const framingStageEl = ref(null)
const framingSourceWidth = ref(16)
const framingSourceHeight = ref(9)
const framingDragging = ref(false)
const framingConfirming = ref(false)
const framingSaving = ref(false)
const framingSelectionLoaded = ref(false)
const framingDraftDirty = ref(false)
const showFramingSetupPrompt = ref(false)
let framingDragStart = null
const editTitle = ref('')
const automationOutputCount = ref(10)
const targetDurationMode = ref('30')
const customTargetDuration = ref(60)
const tuneSourceId = ref('')
const tuneVideoEl = ref(null)
const tuneDuration = ref(0)
const tuneHead = ref(0)
const tuneIn = ref(0)
const tuneOut = ref(0)
const tuneClips = ref([])
const tuneMode = ref('source')
const tunePreviewIndex = ref(-1)
const tuneDragIndex = ref(-1)
const tuneImageDuration = ref(3)
// Where 覆盖 lands, in seconds along the assembled edit.
const tunePlayhead = ref(0)
// The soundtrack, pinned when the first real clip lands. It deliberately does not follow
// later edits: 覆盖 must leave the sound exactly where it was, so this cannot be derived
// from the clip list.
const tuneBed = ref(null)
const tuneKeepSound = ref(true)
const tuneKeepEffectSound = ref(true)
const tuneEffectSoundMode = ref('auto')
const tuneUndoStack = ref([])
const TUNE_UNDO_LIMIT = 50
let tuneUid = 0
let tuneStillTimer = null

const isRenderingTimeline = ref(false)
const downloadUrl = ref('')
const isDownloading = ref(false)
const downloadStatus = ref('')
const downloadStatusKind = ref('muted')
const cleanupStatus = ref('')
const cleanupStatusKind = ref('muted')
const selectedSourceIds = ref([])
const sourceFilter = ref('')
const vaultFilter = ref('')
const selectedAutomationMusicIds = ref([])
const selectedAutomationVoiceoverIds = ref([])
const selectedIntroEffectIds = ref([])
const selectedOutroEffectIds = ref([])
const effectScope = ref('auto')
const effectCoverAudio = ref(false)
const isCreatingJob = ref(false)
const jobStatus = ref('')
const jobStatusKind = ref('muted')
const muteOriginalAudio = ref(true)
const editingMode = ref('smart')
const editorialPreset = ref('smart')

// Subtitles come from the narration's own per-word timestamps, so there is nothing to write
// without a voiceover. The control is disabled rather than hidden, with the reason spelled out —
// a checkbox that quietly does nothing is how someone ships a hundred videos expecting text.
const SUBTITLE_SIZES = [
  { value: 'small', label: '小' },
  { value: 'medium', label: '中' },
  { value: 'large', label: '大' }
]
const subtitlesOn = ref(false)
const subtitleFont = ref('noto_sans_sc')
const subtitleSize = ref('medium')
const subtitleFonts = ref([])
// Four states, not two. 'unknown' before asking, true/false for an answer the backend actually
// gave, and 'unreachable' when the question could not be put at all. Collapsing the last into
// false is what made an out-of-date backend report itself as an FFmpeg without libass — a
// specific, plausible and completely wrong diagnosis that sends you to fix the wrong thing.
const subtitleCanBurn = ref('unknown')

const installedSubtitleFonts = computed(() => subtitleFonts.value.filter((font) => font.installed))

const subtitleFontPreview = computed(() =>
  installedSubtitleFonts.value.some((font) => font.key === subtitleFont.value && font.preview)
)

// Real size fractions from the backend, so 小/中/大 are shown in their true proportions.
const subtitleSizeFractions = ref({})

// The preview cannot show the size the subtitle will really be — that depends on the export
// frame, and this panel is a few hundred pixels wide. What it can show honestly is the ratio
// between the three settings, so 大 looks as much bigger than 中 as it actually will.
const PREVIEW_BASE_PX = 26
const subtitlePreviewStyle = computed(() => {
  const fractions = subtitleSizeFractions.value
  const chosen = fractions[subtitleSize.value]
  const middle = fractions.medium
  const scale = chosen && middle ? chosen / middle : 1
  const px = PREVIEW_BASE_PX * scale
  return {
    fontFamily: `'ave-${subtitleFont.value}', sans-serif`,
    fontSize: `${px.toFixed(1)}px`,
    // The outline is a fraction of the font size in the export too, so it has to grow with it,
    // or 大 would come out looking thinner-edged than 小.
    '--subtitle-outline': `${Math.max(1, px * 0.09).toFixed(2)}px`
  }
})

// The picker names three fonts, and a name tells you nothing about what a typeface looks like.
// The backend sends a subset of each — a few kilobytes, only the glyphs shown here — which is
// registered as a real @font-face so the labels and the sample line draw in the actual font.
function applySubtitleFontFaces(fonts) {
  const id = 'ave-subtitle-fonts'
  document.getElementById(id)?.remove()
  const rules = fonts
    .filter((font) => font.preview)
    .map((font) => `@font-face{font-family:'ave-${font.key}';src:url(${font.preview}) format('woff2');font-display:swap;}`)
    .join('\n')
  if (!rules) return
  const style = document.createElement('style')
  style.id = id
  style.textContent = rules
  document.head.appendChild(style)
}

const canUseSubtitles = computed(
  () =>
    selectedAutomationVoiceoverIds.value.length > 0 &&
    subtitleCanBurn.value === true &&
    installedSubtitleFonts.value.length > 0
)

const subtitleBlocker = computed(() => {
  if (subtitleCanBurn.value === 'unreachable') {
    return '后端没有回应字幕接口，通常是后端还在跑改动之前的版本。请重启应用后再试。'
  }
  if (!selectedAutomationVoiceoverIds.value.length) return '先选一个旁白，字幕会跟着旁白自动生成。'
  if (subtitleCanBurn.value === false) {
    return '当前 FFmpeg 不能把字幕压进画面（缺少 libass）。运行 scripts/prepare_assets.py 获取可用版本。'
  }
  if (subtitleCanBurn.value !== 'unknown' && !installedSubtitleFonts.value.length) {
    return '没有找到内置字体。运行 scripts/prepare_assets.py 下载。'
  }
  return ''
})

// Turning subtitles on and then dropping the voiceover would otherwise leave the request asking
// for something the backend can only refuse, so the flag follows what is actually possible.
watch(canUseSubtitles, (usable) => {
  if (!usable) subtitlesOn.value = false
})

async function refreshSubtitleFonts() {
  try {
    const info = await api('/subtitles/fonts')
    subtitleFonts.value = info.fonts || []
    subtitleCanBurn.value = Boolean(info.can_burn)
    applySubtitleFontFaces(subtitleFonts.value)
    subtitleSizeFractions.value = Object.fromEntries(
      (info.sizes || []).map((size) => [size.key, size.fraction])
    )
    if (!info.fonts?.some((font) => font.key === subtitleFont.value && font.installed)) {
      const first = (info.fonts || []).find((font) => font.installed)
      if (first) subtitleFont.value = first.key
    }
  } catch (err) {
    // Not `false` — that would claim the backend answered, and answered no.
    subtitleCanBurn.value = 'unreachable'
    log(`字幕接口读取失败（后端可能是旧版本，重启应用即可）：${humanError(err.message)}`)
  }
}

// The customer chooses an editorial outcome. Pace, point allocation, music contour and all
// candidate-selection maths remain backend decisions and are written to the batch manifest.
const EDITORIAL_PRESETS = [
  { value: 'smart', label: '智能推荐', description: '根据素材结构自动分配' },
  { value: 'showcase', label: '完整展示', description: '优先覆盖点位与重要画面' },
  { value: 'dynamic', label: '动感巡游', description: '紧凑、流动，强调行进感' },
  { value: 'immersive', label: '沉浸参观', description: '长镜头、平稳，保留空间感' }
]

// 专业剪辑 brings back the earlier dimensions as a second way to describe the result. It
// does not create a second planner: these fields are the existing detailed policy contract
// accepted by the same batch endpoint and timeline engine used by the preset experience.
const POLICY_GROUPS = [
  { needs: 'any', label: '节奏', hint: '任何素材均适用' },
  { needs: 'cruise', label: '画面与点位', hint: '巡游素材专属' }
]

const POLICY_AXES = [
  {
    key: 'pace',
    label: '剪辑节奏',
    field: 'paces',
    needs: 'any',
    options: [
      { value: 'auto', label: '自动', hint: '每条自动选择' },
      { value: 'fast', label: '快切', hint: '平均约 2 秒一刀' },
      { value: 'normal', label: '常规', hint: '平均约 5 秒一刀' },
      { value: 'cinematic', label: '慢镜', hint: '平均约 8 秒一刀' }
    ]
  },
  {
    key: 'contour',
    label: '节奏起伏',
    field: 'contours',
    needs: 'any',
    options: [
      { value: 'auto', label: '自动', hint: '每条自动选择' },
      { value: 'flat', label: '平稳', hint: '全片保持一个节奏' },
      { value: 'accelerate', label: '渐快', hint: '剪辑逐渐加快' },
      { value: 'decelerate', label: '渐慢', hint: '剪辑逐渐放缓' },
      { value: 'arc', label: '弧线', hint: '两端舒缓，中段加快' },
      { value: 'follow_energy', label: '跟音乐', hint: '跟随音乐强弱变化' }
    ]
  },
  {
    key: 'footage_mix',
    label: '素材侧重',
    field: 'footage_mixes',
    needs: 'cruise',
    options: [
      { value: 'auto', label: '自动', hint: '每条自动选择' },
      { value: 'dwell_heavy', label: '多停留', hint: '偏向点位停留画面' },
      { value: 'balanced', label: '均衡', hint: '平衡停留与行进画面' },
      { value: 'transit_heavy', label: '多行进', hint: '偏向移动中的画面' }
    ]
  },
  {
    key: 'emphasis',
    label: '时长取舍',
    field: 'emphases',
    needs: 'cruise',
    options: [
      { value: 'auto', label: '自动', hint: '每条自动选择' },
      { value: 'target', label: '按比例', hint: '按可用素材分配时长' },
      { value: 'coverage', label: '保覆盖', hint: '尽量覆盖每个点位' }
    ]
  },
  {
    key: 'point_scope',
    label: '点位取用',
    field: 'point_scopes',
    needs: 'cruise',
    options: [
      { value: 'auto', label: '自动', hint: '自动选择点位范围' },
      { value: 'all', label: '全部', hint: '使用全部可容纳点位' }
    ]
  }
]

const policies = ref(Object.fromEntries(POLICY_AXES.map((axis) => [axis.key, 'auto'])))
const selectedCruisePoints = computed(() =>
  selectedSourceIds.value.reduce((total, id) => {
    const item = media.value.find((entry) => entry.id === id)
    return total + Number(item?.metadata?.cruise_points || 0)
  }, 0)
)
const hasCruisePoints = computed(() => selectedCruisePoints.value > 0)

function axisApplies(axis) {
  return axis.needs === 'any' || hasCruisePoints.value
}

const policyGroups = computed(() => POLICY_GROUPS.map((group) => ({
  ...group,
  live: group.needs === 'any' || hasCruisePoints.value,
  status: group.needs === 'any'
    ? group.hint
    : hasCruisePoints.value
      ? `已识别 ${selectedCruisePoints.value} 个点位`
      : `${group.hint} · 当前未识别点位`,
  axes: POLICY_AXES.filter((axis) => axis.needs === group.needs)
})))

function resetPolicies() {
  policies.value = Object.fromEntries(POLICY_AXES.map((axis) => [axis.key, 'auto']))
}

const editingCapabilities = ref({
  points: { evidence: 'none', message: '先选择视频素材' },
  semantic: { evidence: 'none', message: '先选择视频素材' },
  music: { evidence: 'none', message: '未添加音乐 · 将按画面节奏剪辑' }
})
const editingCapabilitiesLoading = ref(false)
const musicCanDriveRhythm = computed(() => (
  !editingCapabilitiesLoading.value
  && ['structured', 'mixed'].includes(editingCapabilities.value.music?.evidence)
))

function optionApplies(axis, option) {
  if (!axisApplies(axis)) return false
  if (option.value === 'follow_energy') return musicCanDriveRhythm.value
  return true
}

function policyOptionHint(axis, option) {
  if (option.value === 'follow_energy' && !musicCanDriveRhythm.value) {
    return editingCapabilitiesLoading.value ? '正在分析音乐节拍' : '需要先选择节拍清晰的音乐'
  }
  return option.hint
}

// Empty lists mean 自动; a one-item list pins that dimension. Preset mode never includes
// these fields, so switching views cannot leak a professional choice into a smart batch.
const policyFields = computed(() => Object.fromEntries(
  POLICY_AXES.map((axis) => {
    const selected = policies.value[axis.key]
    const option = axis.options.find((item) => item.value === selected)
    return [
      axis.field,
      selected === 'auto' || !option || !optionApplies(axis, option) ? [] : [selected]
    ]
  })
))

const editingPolicyPayload = computed(() => editingMode.value === 'smart'
  ? { editorial_preset: editorialPreset.value }
  : policyFields.value
)

watch(
  [editingCapabilitiesLoading, musicCanDriveRhythm],
  ([loading, usable]) => {
    if (!loading && !usable && policies.value.contour === 'follow_energy') {
      policies.value.contour = 'auto'
    }
  }
)

const sourceCapabilityMessage = computed(() => selectedSourceIds.value.length
  ? `已选 ${selectedSourceIds.value.length} 段 · 将自动识别镜头与画面质量`
  : '先选择视频素材'
)

function capabilityTone(evidence) {
  if (evidence === 'full' || evidence === 'structured') return 'success'
  if (['partial', 'markers_only', 'mixed', 'ambient'].includes(evidence)) return 'warning'
  if (evidence === 'invalid' || evidence === 'unreadable') return 'danger'
  return 'muted'
}
const settingsForm = ref({
  llm: { enabled: false, provider: 'doubao', api_key: '', model: '', timeout_ms: 20000 },
  tts: { enabled: false, provider: 'volcengine_sync', app_id: '', access_token: '', voice_type: 'BV001_streaming', cluster: 'volcano_tts', encoding: 'mp3', speed_ratio: 1.0, daily_limit: 100 },
  seedance: {
    enabled: false,
    provider: 'volcengine_ark',
    api_key: '',
    model: '',
    image_model: '',
    image_size: 'adaptive',
    base_url: 'https://ark.cn-beijing.volces.com/api/v3',
    tos_access_key_id: '',
    tos_secret_access_key: '',
    tos_security_token: '',
    tos_bucket: '',
    tos_region: 'cn-beijing',
    tos_endpoint: 'tos-cn-beijing.volces.com',
    tos_object_prefix: 'seedance/staging',
    tos_url_expires_seconds: 86400,
    daily_limit: 10,
    default_duration_seconds: 5,
    resolution: '720p',
    ratio: '16:9'
  },
  robot: { websocket_url: '' },
  automation: {
    daily_output_limit: 100,
    output_aspect_ratio: null,
    framing_configured: false,
    framing_mode: 'center',
    framing_crop_x: 0.5,
    framing_crop_y: 0.5
  }
})
const isSavingSettings = ref(false)
const isTestingLlm = ref(false)
const isTestingTts = ref(false)
const isTestingSeedance = ref(false)
const settingsStatus = ref('')
const settingsStatusKind = ref('muted')
const settingsAdminUnlocked = ref(false)
const settingsAdminToken = ref('')
const settingsAdminUsername = ref('')
const settingsAdminPassword = ref('')
const isUnlockingSettings = ref(false)
const settingsAdminStatus = ref('')
const settingsAdminStatusKind = ref('danger')
let settingsAdminExpiryTimer = null
const framingPreviewReady = computed(() => Boolean(
  framingTest.value?.ready && framingTest.value?.preview_id
))
const savedFramingConfigured = computed(() => Boolean(
  settings.value?.automation?.framing_configured
))
const savedFramingSummary = computed(() => {
  if (!savedFramingConfigured.value) return ''
  const automation = settings.value.automation
  const position = automation.framing_mode === 'custom' ? '自定义位置' : '默认居中'
  return `已保存：${automation.output_aspect_ratio} · ${position}`
})
const framingPreviewUrl = computed(() => {
  const cfg = apiConfig.value
  const id = framingTest.value?.preview_id
  if (!cfg || !id) return ''
  return `${cfg.baseUrl}/framing-test/media?preview_id=${encodeURIComponent(id)}&token=${encodeURIComponent(cfg.token)}`
})
const framingSourceAspect = computed(() => (
  Math.max(1, framingSourceWidth.value) / Math.max(1, framingSourceHeight.value)
))
const framingTargetAspect = computed(() => framingAspectRatio.value === '9:16' ? 9 / 16 : 16 / 9)
const framingGeometry = computed(() => {
  if (!framingAspectRatio.value) return { left: 0, top: 0, width: 100, height: 100 }
  const source = framingSourceAspect.value
  const target = framingTargetAspect.value
  if (source >= target) {
    const width = Math.min(100, (target / source) * 100)
    const left = (100 - width) * framingCropX.value
    return { left, top: 0, width, height: 100 }
  }
  const height = Math.min(100, (source / target) * 100)
  const top = (100 - height) * framingCropY.value
  return { left: 0, top, width: 100, height }
})
const framingStageStyle = computed(() => ({
  aspectRatio: `${Math.max(1, framingSourceWidth.value)} / ${Math.max(1, framingSourceHeight.value)}`,
  maxWidth: `${Math.min(820, 460 * framingSourceAspect.value).toFixed(0)}px`
}))
const framingCropStyle = computed(() => {
  const frame = framingGeometry.value
  return {
    left: `${frame.left}%`,
    top: `${frame.top}%`,
    width: `${frame.width}%`,
    height: `${frame.height}%`
  }
})
const framingShadeStyles = computed(() => {
  const frame = framingGeometry.value
  const right = frame.left + frame.width
  const bottom = frame.top + frame.height
  return {
    left: { left: 0, top: 0, width: `${frame.left}%`, height: '100%' },
    right: { left: `${right}%`, top: 0, width: `${100 - right}%`, height: '100%' },
    top: { left: `${frame.left}%`, top: 0, width: `${frame.width}%`, height: `${frame.top}%` },
    bottom: { left: `${frame.left}%`, top: `${bottom}%`, width: `${frame.width}%`, height: `${100 - bottom}%` }
  }
})
const savedOutputFrame = computed(() => (
  !savedFramingConfigured.value
    ? null
    : settings.value?.automation?.output_aspect_ratio === '9:16'
      ? { width: 720, height: 1280 }
      : { width: 1280, height: 720 }
))
const savedOutputCrop = computed(() => ({
  x: settings.value?.automation?.framing_configured
    ? Number(settings.value.automation.framing_crop_x ?? 0.5)
    : 0.5,
  y: settings.value?.automation?.framing_configured
    ? Number(settings.value.automation.framing_crop_y ?? 0.5)
    : 0.5
}))
const voiceoverTitle = ref('')
const voiceoverText = ref('')
const voiceoverUseLlm = ref(false)
const voiceoverSeconds = ref(null)
const canGenerateVoiceover = computed(() => Boolean(voiceoverText.value.trim()))
const isGeneratingVoiceover = ref(false)
const voiceoverStatus = ref('')
const voiceoverStatusKind = ref('muted')
const ttsQuotaText = computed(() => {
  if (!ttsQuota.value) return '今日旁白额度：--'
  return `今日剩余 ${ttsQuota.value.remaining} / ${ttsQuota.value.limit} 次`
})
const seedanceTitle = ref('')
const seedancePrompt = ref('')
const seedanceSourceMode = ref('image')
const seedanceOutput = ref('video')
const renameTarget = ref(null)
const renameOldName = ref('')
const renameValue = ref('')
const renameStatus = ref('')
const renameStatusKind = ref('muted')
const isRenaming = ref(false)
let seedancePollTimer = null

// Generation runs in the background on the server, so without this the row sits on
// 排队中 until something else happens to refresh the list.
const seedanceInFlight = computed(() =>
  seedanceAssets.value.some((asset) => asset.status === 'queued' || asset.status === 'running')
)

watch(seedanceInFlight, (busy) => {
  if (busy && !seedancePollTimer) {
    seedancePollTimer = setInterval(() => {
      refreshSeedanceAssets().catch(() => {})
    }, 4000)
  } else if (!busy && seedancePollTimer) {
    clearInterval(seedancePollTimer)
    seedancePollTimer = null
    refreshMedia().catch(() => {})
    refreshVault().catch(() => {})
  }
})
// Seedance and Seedream are different models on different endpoints, so the choice of
// output decides which one runs. Text-to-video is not offered yet.
const seedanceOutputHint = computed(() =>
  seedanceOutput.value === 'video'
    ? '用 Seedance 生成视频。可以选一张来源图片或一个视频帧作为首帧，也可以只写提示词凭空生成。'
    : '用 Seedream 生成图片。可以选一张来源图片做修改，也可以只写提示词凭空生成。'
)
const selectedSeedanceImageId = ref('')
const selectedSeedanceVideoId = ref('')
// The chosen frame is confirmed, not typed. A bare seconds box meant picking a picture you
// had never seen, and only finding out what you got after spending quota.
const frameVideoEl = ref(null)
const frameHead = ref(0)
const frameDuration = ref(0)
const grabbedFrame = ref(null)
const isGrabbingFrame = ref(false)
const FRAME_STEP_SECONDS = 1 / 30
const seedanceDuration = ref(5)
const seedanceStatus = ref('')
const seedanceStatusKind = ref('muted')
const isGeneratingSeedance = ref(false)

// Filtering beats scrolling once a list grows; scrolling is the fallback, not the tool.
const filteredSourceVideos = computed(() => {
  const needle = sourceFilter.value.trim().toLowerCase()
  if (!needle) return sourceVideoItems.value
  return sourceVideoItems.value.filter((item) => item.path.toLowerCase().includes(needle))
})
const sourceVideoItems = computed(() => media.value.filter((item) => item.kind === 'video' && itemRole(item) === 'raw_video'))
const audioItems = computed(() => media.value.filter((item) => item.kind === 'audio' && itemRole(item) === 'music'))
const voiceoverItems = computed(() => media.value.filter((item) => item.kind === 'audio' && itemRole(item) === 'tts_voice'))
const imageItems = computed(() => media.value.filter((item) => item.kind === 'image' && itemRole(item) === 'image'))
const effectItems = computed(() => media.value.filter((item) => item.kind === 'video' && itemRole(item) === 'seedance_effect'))
const frameVideoSrc = computed(() => {
  const item = sourceVideoItems.value.find((entry) => entry.id === selectedSeedanceVideoId.value)
  return item ? mediaFileUrl(item.path) : ''
})
const grabbedFrameSrc = computed(() =>
  grabbedFrame.value ? mediaFileUrl(grabbedFrame.value.frame_path) : ''
)
const usingGrabbedFrame = computed(() =>
  seedanceSourceMode.value === 'stamp'
  && Boolean(selectedSeedanceVideoId.value)
  && Boolean(grabbedFrame.value)
)
// A frame belongs to the video it came from, so changing the video must not leave the old
// thumbnail standing as though it were still the choice.
watch(selectedSeedanceVideoId, () => {
  grabbedFrame.value = null
  frameHead.value = 0
  frameDuration.value = 0
})
const canGenerateSeedance = computed(() => {
  // A prompt is required either way: with no source there would be nothing to work from.
  if (!seedancePrompt.value.trim() || isGeneratingSeedance.value) return false
  // Picking a video but never confirming a frame would quietly drop it and generate from the
  // prompt alone, so the chosen video would have had no effect on the result.
  if (seedanceSourceMode.value === 'stamp' && selectedSeedanceVideoId.value && !grabbedFrame.value) return false
  if (seedanceQuota.value?.count_remaining === 0) return false
  if (seedanceOutput.value === 'video') {
    const duration = Number(seedanceDuration.value)
    if (!Number.isFinite(duration) || duration < 2 || duration > 15) return false
    const billed = Math.max(duration, Number(seedanceQuota.value?.minimum_billable_seconds || 5))
    if (seedanceQuota.value && billed > seedanceQuota.value.remaining_seconds) return false
  }
  // Both outputs accept a prompt on its own; a source picture only steers the result.
  return true
})
const seedanceQuotaText = computed(() => {
  if (!seedanceQuota.value) return '今日额度：--'
  if (seedanceOutput.value === 'image') {
    return `今日剩余 ${seedanceQuota.value.count_remaining} / ${seedanceQuota.value.limit} 次`
  }
  return `今日剩余约 ${seedanceQuota.value.remaining} 次`
})
const seedanceConfiguredLabel = computed(() => {
  const cfg = settings.value?.seedance
  if (!cfg?.api_key?.configured) return '缺少密钥'
  if (!cfg?.model) return '缺少模型'
  if (!cfg?.tos_access_key_id?.configured || !cfg?.tos_secret_access_key?.configured || !cfg?.tos_bucket) return '缺少暂存'
  return '已配置'
})
const seedanceSettingsSubtitle = computed(() => {
  const cfg = settings.value?.seedance
  if (!cfg) return '暂无配置'
  return [cfg.model || '暂无模型', cfg.tos_bucket || '暂无TOS桶'].join(' · ')
})
const filteredVaultAssets = computed(() => {
  const needle = vaultFilter.value.trim().toLowerCase()
  if (!needle) return userVaultAssets.value
  return userVaultAssets.value.filter((asset) =>
    asset.name.toLowerCase().includes(needle) || roleLabel(asset.role).toLowerCase().includes(needle)
  )
})
const userVaultAssets = computed(() => vaultAssets.value.filter((asset) => asset.role !== 'cache'))

const expandedVaultGroups = ref({})
function toggleVaultGroup(key) {
  expandedVaultGroups.value = { ...expandedVaultGroups.value, [key]: !expandedVaultGroups.value[key] }
}

// A finished video with subtitles is saved twice — the delivered cut and a subtitle-free master
// to re-edit from. Listed flat that would double the length of the library for no new content,
// so the pair collapses into one line and only opens when asked. The delivered file leads,
// because that is the one people are usually looking for.
const VARIANT_ORDER = { subtitled: 0, single: 0, master: 1 }
const vaultRows = computed(() => {
  const groups = new Map()
  const rows = []
  for (const asset of filteredVaultAssets.value) {
    const key = asset.export_group
    if (!key) {
      rows.push({ type: 'asset', key: asset.id, asset })
      continue
    }
    let group = groups.get(key)
    if (!group) {
      group = { type: 'group', key, name: '', size_bytes: 0, modified_at: asset.modified_at, members: [] }
      groups.set(key, group)
      rows.push(group)
    }
    group.members.push(asset)
    group.size_bytes += asset.size_bytes || 0
    if (asset.modified_at > group.modified_at) group.modified_at = asset.modified_at
  }
  for (const group of groups.values()) {
    group.members.sort((a, b) => (VARIANT_ORDER[a.variant] ?? 9) - (VARIANT_ORDER[b.variant] ?? 9))
    const lead = group.members[0]
    group.name = lead.name.replace(/\.[^.]+$/, '')
  }
  // A group of one is just a file. Drawing it with a disclosure arrow would promise something
  // to open and then show a single row identical to the one above it.
  return rows.map((row) =>
    row.type === 'group' && row.members.length === 1
      ? { type: 'asset', key: row.members[0].id, asset: row.members[0] }
      : row
  )
})

const weekdayNames = ['日', '一', '二', '三', '四', '五', '六']
const calendarAssetMap = computed(() => {
  const map = new Map()
  for (const day of calendarDays.value) map.set(day.day, day.assets || [])
  return map
})
const selectedCalendarAssets = computed(() => calendarAssetMap.value.get(selectedCalendarDate.value) || [])
const calendarMonthTitle = computed(() => calendarCursor.value.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long' }))
const calendarStartLabel = computed(() => formatDateKey(calendarStartDate))
const calendarMaxLabel = computed(() => formatDateKey(calendarMaxDate))
const calendarCanPrev = computed(() => addMonths(calendarCursor.value, -1) >= calendarStartDate)
const calendarCanNext = computed(() => addMonths(calendarCursor.value, 1) <= startOfMonth(calendarMaxDate))
const calendarCells = computed(() => buildCalendarCells(calendarCursor.value))
const cleanupCandidates = computed(() => storageReport.value?.cleanup_candidates || [])
const storagePercent = computed(() => {
  const report = storageReport.value
  if (!report?.threshold_bytes) return '0%'
  return `${Math.min(100, (report.total_bytes / report.threshold_bytes) * 100).toFixed(1)}%`
})
const automationPairingSummary = computed(() => {
  const count = normalizedAutomationOutputCount.value
  const sources = selectedSourceIds.value.length
  const parts = []
  if (selectedAutomationMusicIds.value.length) parts.push('1 个音乐')
  if (selectedAutomationVoiceoverIds.value.length) parts.push('1 个旁白')
  let allocation = ''
  if (sources > 0 && count < sources) {
    allocation = `覆盖 ${count} / ${sources} 段素材`
  } else if (sources > 0 && count % sources === 0) {
    allocation = `${sources} 段素材各 ${count / sources} 条`
  } else if (sources > 0) {
    allocation = `${sources} 段素材均衡分配`
  }
  const audio = parts.length ? `每条 ${parts.join(' + ')}` : '无音频'
  return [ `${count} 条输出`, allocation, audio ].filter(Boolean).join(' · ')
})
const normalizedAutomationOutputCount = computed(() => Math.min(100, Math.max(1, Number(automationOutputCount.value || 1))))
const targetDurationSeconds = computed(() => {
  if (targetDurationMode.value === '15') return 15
  if (targetDurationMode.value === '30') return 30
  return Math.min(180, Math.max(1, Number(customTargetDuration.value || 30)))
})
const robotHardwareFault = computed(() =>
  Boolean(robot.value.system_status) && robot.value.system_status !== 'ready'
)
const robotDriveBlocker = computed(() => {
  if (robot.value.map_mode === 'mapping') return '机器人在扫图模式，无法导航。请先切回定位模式。'
  if (robotHardwareFault.value) return `机器人硬件状态异常（${robot.value.system_status}），先检查设备。`
  if (robot.value.map_status === 'failed') return '机器人定位失败，无法导航。请检查地图和当前位置。'
  return ''
})
const robotConnectionLabel = computed(() => {
  if (robot.value.connected) return '硬件已连接'
  if (settings.value?.robot?.websocket_url) return robotStatusLabel(robot.value.connection_status || 'disconnected')
  return '未填写机器人地址'
})
const robotUrlPreview = computed(() => settings.value?.robot?.websocket_url || '未填写硬件地址')
const activeSessionSummary = computed(() => {
  const session = activeSession.value
  if (!session) return '暂无进行中的采集'
  const lines = [
    `标题：${session.title || '未命名采集'}`,
    `状态：${session.active ? '进行中' : '已结束'}`,
    `开始时间：${session.started_at ? formatDate(session.started_at) : '未知'}`
  ]
  if (session.ended_at) lines.push(`结束时间：${formatDate(session.ended_at)}`)
  if (session.notes?.length) lines.push(`备注：${session.notes.join('\n')}`)
  return lines.join('\n')
})

function log(message) {
  logs.value.unshift({ time: new Date().toLocaleTimeString(), message })
  logs.value = logs.value.slice(0, 120)
}

function initialSidebarWidth() {
  const saved = Number(window.localStorage?.getItem('ave-sidebar-width')) || 202
  return Math.min(260, Math.max(166, saved))
}

function beginSidebarResize(event) {
  isResizingSidebar.value = true
  event.currentTarget?.setPointerCapture?.(event.pointerId)
  window.addEventListener('pointermove', resizeSidebar)
  window.addEventListener('pointerup', endSidebarResize, { once: true })
}

function resizeSidebar(event) {
  if (!isResizingSidebar.value) return
  sidebarWidth.value = Math.min(260, Math.max(166, event.clientX))
}

function endSidebarResize() {
  isResizingSidebar.value = false
  window.localStorage?.setItem('ave-sidebar-width', String(sidebarWidth.value))
  window.removeEventListener('pointermove', resizeSidebar)
}

async function api(path, options = {}) {
  const cfg = apiConfig.value
  if (!cfg) throw new Error('桌面桥接仍在启动中。如果一直显示，请重启 Electron 应用。')
  const res = await fetch(`${cfg.baseUrl}${path}`, {
    ...options,
    headers: {
      'content-type': 'application/json',
      'x-bridge-token': cfg.token,
      ...(options.headers || {})
    }
  })
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const payload = await res.json()
      if (payload && payload.detail !== undefined && payload.detail !== null) detail = payload.detail
    } catch {
      try {
        const text = await res.text()
        if (text) detail = text
      } catch {}
    }
    // Some errors carry a structured detail (e.g. cruise route validation); keep it on the
    // error so callers can render it instead of stringifying an object into the message.
    const message = typeof detail === 'string' ? detail : `${res.status} ${res.statusText}`
    const error = new Error(humanError(message))
    error.status = res.status
    error.detail = detail
    throw error
  }
  return res.json()
}

function connectWs() {
  const cfg = apiConfig.value
  if (!cfg) return
  const socket = new WebSocket(cfg.wsUrl, ['ave.bridge', `ave-token-${cfg.token}`])
  socket.onopen = () => { connected.value = true; log('WebSocket 已连接') }
  socket.onclose = () => { connected.value = false; setTimeout(() => { if (apiConfig.value) connectWs() }, 1500) }
  socket.onerror = () => { connected.value = false }
  socket.onmessage = (event) => {
    const msg = JSON.parse(event.data)
    if (msg.type === 'ROBOT_STATE') robot.value = msg.data
    if (msg.type === 'CAPTURE_STARTED') {
      activeSession.value = msg.data
      captureStatusKind.value = 'success'
      captureStatus.value = '录制已开始。'
    }
    if (msg.type === 'CAPTURE_STOPPED') {
      activeSession.value = null
      const localPath = msg.data?.media_local_path || robot.value.media_local_path
      const syncError = msg.data?.media_sync_error || robot.value.media_sync_error
      captureStatusKind.value = syncError ? 'danger' : 'success'
      captureStatus.value = syncError
        ? `机器人录制成功，但保存到 Windows 失败：${humanError(syncError)}`
        : localPath
          ? `视频已保存：${shortPath(localPath)}`
          : '机器人录制已停止。'
    }
    if (msg.type === 'JOB_UPDATED' || msg.type === 'JOB_CREATED') {
      refreshJobs()
      if (msg.data?.status === 'succeeded') {
        refreshMedia(); refreshVault()
      }
    }
    if (msg.type === 'ROBOT_PHOTO') {
      if (msg.data?.local_media_item) {
        captureStatusKind.value = 'success'
        captureStatus.value = `照片已保存：${shortPath(msg.data.local_media_item.path)}`
        log(`机器人照片已保存：${shortPath(msg.data.local_media_item.path)}`)
      } else if (!msg.data?.media_sync_error && !robot.value.media_sync_error) {
        captureStatusKind.value = 'success'
        captureStatus.value = '机器人已完成拍照。'
      }
      refreshMedia(); refreshVault()
    }
    if (msg.type === 'ROBOT_MEDIA_SYNCED') {
      log(`机器人媒体已保存：${shortPath(msg.data?.media_item?.path)}`)
      refreshMedia(); refreshVault()
    }
    if (msg.type === 'ROBOT_MEDIA_SYNC_FAILED') {
      const action = msg.data?.kind_hint === 'image' ? '拍照' : '录制'
      const error = humanError(msg.data?.error || '未知错误')
      captureStatusKind.value = 'danger'
      captureStatus.value = `机器人${action}成功，但保存到 Windows 失败：${error}`
      log(`机器人${action}成功，但保存到 Windows 失败：${error}`)
    }
    // Deliberately does not clear cruiseIssues: this event is published before the start
    // response returns, and clearing here would race away the validation warnings it carries.
    if (msg.type === 'CRUISE_STARTED') cruiseRun.value = msg.data
    if (['CRUISE_POINT_DISPATCHED', 'CRUISE_POINT_ARRIVED', 'CRUISE_POINT_DEPARTED', 'CRUISE_POINT_FAILED'].includes(msg.type)) {
      applyCruiseSegment(msg.data)
      if (msg.type === 'CRUISE_POINT_FAILED') {
        log(`巡游点位失败：#${msg.data?.segment?.goal_id} ${humanError(msg.data?.segment?.error || '')}`)
      }
    }
    if (['CRUISE_FINISHED', 'CRUISE_CANCELED', 'CRUISE_FAILED'].includes(msg.type)) {
      cruiseRun.value = msg.data
      if (msg.type === 'CRUISE_FINISHED') setCruiseStatus('success', `巡游结束：到达 ${cruiseArrivedCount.value} 个点位，失败 ${cruiseFailedCount.value} 个。`)
      if (msg.type === 'CRUISE_CANCELED') setCruiseStatus('muted', '巡游已取消。')
      if (msg.type === 'CRUISE_FAILED') setCruiseStatus('danger', `巡游失败：${humanError(msg.data?.error || '未知错误')}`)
      refreshMedia(); refreshVault()
    }
    if (msg.type === 'ERROR') {
      const message = humanError(msg.data?.message || '未知后端错误')
      if (['CAPTURE_START', 'CAPTURE_STOP', 'ROBOT_CAPTURE_PHOTO'].includes(msg.data?.command)) {
        captureStatusKind.value = 'danger'
        captureStatus.value = message
      }
      log(`错误：${message}`)
      return
    }
    if (!['PONG'].includes(msg.type)) log(eventLabel(msg.type))
  }
  ws.value = socket
}

function sendWs(type, data = {}) {
  ws.value?.send(JSON.stringify({ type, data }))
}

async function refreshRobot() { robot.value = await api('/robot/status') }
async function refreshJobs() { jobs.value = await api('/jobs') }
async function refreshMedia() {
  media.value = await api('/media')
  const validIds = new Set(sourceVideoItems.value.map((item) => item.id))
  selectedSourceIds.value = selectedSourceIds.value.filter((id) => validIds.has(id))
  const validMusicIds = new Set(audioItems.value.map((item) => item.id))
  const validVoiceoverIds = new Set(voiceoverItems.value.map((item) => item.id))
  selectedAutomationMusicIds.value = selectedAutomationMusicIds.value.filter((id) => validMusicIds.has(id))
  selectedAutomationVoiceoverIds.value = selectedAutomationVoiceoverIds.value.filter((id) => validVoiceoverIds.has(id))
  const validEffectIds = new Set(effectItems.value.map((item) => item.id))
  selectedIntroEffectIds.value = selectedIntroEffectIds.value.filter((id) => validEffectIds.has(id))
  selectedOutroEffectIds.value = selectedOutroEffectIds.value.filter((id) => validEffectIds.has(id))
  if (selectedSeedanceImageId.value && !imageItems.value.some((item) => item.id === selectedSeedanceImageId.value)) selectedSeedanceImageId.value = ''
  if (selectedSeedanceVideoId.value && !sourceVideoItems.value.some((item) => item.id === selectedSeedanceVideoId.value)) selectedSeedanceVideoId.value = ''
}

let editingCapabilityRequest = 0
let editingCapabilityTimer = null
async function refreshEditingCapabilities() {
  const requestId = ++editingCapabilityRequest
  if (!apiConfig.value) return
  editingCapabilitiesLoading.value = true
  try {
    const result = await api('/editing/capabilities', {
      method: 'POST',
      body: JSON.stringify({
        media_ids: selectedSourceIds.value.slice(0, MAX_SOURCE_VIDEOS),
        music_media_ids: selectedAutomationMusicIds.value.slice(0, MAX_AUTOMATION_ITEMS)
      })
    })
    if (requestId === editingCapabilityRequest) editingCapabilities.value = result
  } catch (err) {
    if (requestId === editingCapabilityRequest) {
      editingCapabilities.value = {
        points: { evidence: 'invalid', message: '素材识别暂时不可用' },
        semantic: { evidence: 'invalid', message: '旁白匹配识别暂时不可用' },
        music: { evidence: 'unreadable', message: '音乐分析暂时不可用' }
      }
      log(`素材识别失败：${humanError(err.message)}`)
    }
  } finally {
    if (requestId === editingCapabilityRequest) editingCapabilitiesLoading.value = false
  }
}

watch(
  [selectedSourceIds, selectedAutomationMusicIds],
  () => {
    if (editingCapabilityTimer) clearTimeout(editingCapabilityTimer)
    editingCapabilityTimer = setTimeout(
      () => { refreshEditingCapabilities().catch(() => {}) }, 250
    )
  },
  { deep: true }
)
async function refreshSettings() {
  settings.value = await api('/settings')
  settingsForm.value = {
    llm: {
      enabled: settings.value.llm.enabled,
      provider: settings.value.llm.provider,
      api_key: '',
      model: settings.value.llm.model || '',
      timeout_ms: settings.value.llm.timeout_ms || 20000
    },
    tts: {
      enabled: settings.value.tts.enabled,
      provider: settings.value.tts.provider,
      app_id: '',
      access_token: '',
      voice_type: settings.value.tts.voice_type || 'BV001_streaming',
      cluster: settings.value.tts.cluster || 'volcano_tts',
      encoding: settings.value.tts.encoding || 'mp3',
      speed_ratio: settings.value.tts.speed_ratio || 1.0,
      daily_limit: settings.value.tts.daily_limit || 100
    },
    seedance: {
      enabled: settings.value.seedance?.enabled || false,
      provider: settings.value.seedance?.provider || 'volcengine_ark',
      api_key: '',
      // Model IDs are configuration, not credentials. Show the exact active values so an
      // operator can tell Mini, Fast and Seedream apart without opening the JSON file.
      model: settings.value.seedance?.model || '',
      image_model: settings.value.seedance?.image_model || '',
      image_size: settings.value.seedance?.image_size || 'adaptive',
      base_url: settings.value.seedance?.base_url || 'https://ark.cn-beijing.volces.com/api/v3',
      tos_access_key_id: '',
      tos_secret_access_key: '',
      tos_security_token: '',
      tos_bucket: settings.value.seedance?.tos_bucket || '',
      tos_region: settings.value.seedance?.tos_region || 'cn-beijing',
      tos_endpoint: settings.value.seedance?.tos_endpoint || 'tos-cn-beijing.volces.com',
      tos_object_prefix: settings.value.seedance?.tos_object_prefix || 'seedance/staging',
      tos_url_expires_seconds: settings.value.seedance?.tos_url_expires_seconds || 86400,
      daily_limit: settings.value.seedance?.daily_limit || 10,
      default_duration_seconds: settings.value.seedance?.default_duration_seconds || 5,
      resolution: settings.value.seedance?.resolution || '720p',
      ratio: settings.value.seedance?.ratio || '16:9'
    },
    robot: {
      websocket_url: settings.value.robot?.websocket_url || ''
    },
    automation: {
      daily_output_limit: settings.value.automation?.daily_output_limit ?? 100,
      output_aspect_ratio: settings.value.automation?.output_aspect_ratio || null,
      framing_configured: Boolean(settings.value.automation?.framing_configured),
      framing_mode: settings.value.automation?.framing_mode || 'center',
      framing_crop_x: Number(settings.value.automation?.framing_crop_x ?? 0.5),
      framing_crop_y: Number(settings.value.automation?.framing_crop_y ?? 0.5)
    }
  }
  if (!framingSelectionLoaded.value || !framingDraftDirty.value) {
    loadFramingSelectionFromSettings()
  }
}
async function refreshTtsAssets() {
  const [assets, quota] = await Promise.all([api('/tts/assets'), api('/tts/quota')])
  ttsAssets.value = assets
  ttsQuota.value = quota
}
async function refreshSeedanceAssets() {
  const [assets, quota] = await Promise.all([
    api('/seedance/assets'),
    api('/seedance/quota')
  ])
  seedanceAssets.value = assets
  seedanceQuota.value = quota
}
async function refreshVault() {
  const [assets, days, storage] = await Promise.all([
    api('/media/vault'),
    api('/media/calendar'),
    api('/media/storage')
  ])
  vaultAssets.value = assets
  calendarDays.value = days
  storageReport.value = storage
}
async function refreshAll() {
  await Promise.all([
    refreshRobot(), refreshMedia(), refreshJobs(), refreshVault(), refreshSettings(),
    refreshTtsAssets(), refreshSeedanceAssets(), refreshCruiseRoutes(), refreshCruiseRun(),
    refreshSubtitleFonts(), refreshFramingTest()
  ])
}

// ── 巡游拍摄 ────────────────────────────────────────────────────────────────────

const CRUISE_SEGMENT_LABELS = {
  pending: '等待中',
  navigating: '前往中',
  arrived: '已到达',
  failed: '失败',
  skipped: '已跳过'
}

const CRUISE_RUN_LABELS = {
  running: '进行中',
  succeeded: '已完成',
  failed: '失败',
  canceled: '已取消'
}

const cruiseRunning = computed(() => cruiseRun.value?.status === 'running')
const cruiseRunStatusLabel = computed(() => CRUISE_RUN_LABELS[cruiseRun.value?.status] || '—')
const cruiseArrivedCount = computed(() => (cruiseRun.value?.segments || []).filter((item) => item.status === 'arrived').length)
const cruiseFailedCount = computed(() => (cruiseRun.value?.segments || []).filter((item) => item.status === 'failed').length)
const canAddCruisePoint = computed(() => {
  const goalId = Number(cruiseNewGoalId.value)
  return Boolean(cruiseNewPath.value) && Number.isInteger(goalId) && goalId >= 0 && cruisePoints.value.length < MAX_CRUISE_POINTS
})
const canSaveCruiseRoute = computed(() => cruiseRouteName.value.trim().length > 0 && cruisePoints.value.length > 0)
// The count belongs in the title so the panel reads as a list even when it is empty.
const cruiseListTitle = computed(() => `巡游清单（${cruisePoints.value.length} 个点位）`)

// Recording is one shared resource, driven either manually or by a cruise. The elapsed
// clock starts when this window first observes recording begin, so it stays blank if the
// app was opened mid-recording rather than showing an invented duration.
const recordingSince = ref(null)
const nowTs = ref(Date.now())
let recordingClock = null

watch(() => robot.value.recording, (isRecording, wasRecording) => {
  if (isRecording && !wasRecording) recordingSince.value = Date.now()
  if (!isRecording) recordingSince.value = null
})

const recordingElapsed = computed(() => {
  if (!robot.value.recording || !recordingSince.value) return ''
  const seconds = Math.max(0, Math.floor((nowTs.value - recordingSince.value) / 1000))
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
})

const recordingSourceLabel = computed(() => {
  if (cruiseRunning.value) return `来自巡游「${cruiseRun.value?.title || '巡游拍摄'}」`
  if (robot.value.recording) return '手动录制'
  return ''
})
const cruiseDwellWarning = computed(() =>
  Number(cruiseDwellMax.value) < Number(cruiseDwellMin.value) ? '停留最长秒数不能小于最短秒数。' : ''
)
// A cruise always records, so it needs sole ownership of the recording. activeSession is
// also set during a cruise, hence the cruiseRunning exclusion: this means a *manual* one.
const manualCaptureActive = computed(() => Boolean(activeSession.value) && !cruiseRunning.value)
const canStartCruise = computed(() =>
  cruisePoints.value.length > 0 && !cruiseRunning.value && !isCruiseBusy.value
  && !cruiseDwellWarning.value && !manualCaptureActive.value
)
const cruiseScanBudget = computed(() => {
  const speed = Math.max(1, Number(cruiseScanSpeed.value) || 1)
  return (2 * (Number(cruiseScanOffset.value || 0) / speed + 0.8)).toFixed(1)
})

function cruiseSegmentLabel(status) { return CRUISE_SEGMENT_LABELS[status] || status }

function cruiseSegmentTiming(segment) {
  if (segment.arrived_at_seconds == null) return segment.error ? humanError(segment.error) : '—'
  const arrived = `${segment.arrived_at_seconds.toFixed(1)}s`
  return segment.dwell_seconds == null ? arrived : `${arrived} · 停留 ${segment.dwell_seconds.toFixed(1)}s`
}

function cruiseIssueText(issue) {
  const rows = (issue.point_indexes || []).map((index) => index + 1).join('、')
  if (issue.field === 'robot') return '无法连接机器人，未能校验地图与路径文件。'
  if (issue.field === 'map_name') {
    return issue.level === 'error'
      ? `机器人没有名为「${issue.value}」的地图。`
      : `未能校验地图${issue.value ? `「${issue.value}」` : ''}。`
  }
  if (issue.field === 'path_name') {
    return issue.level === 'error'
      ? `机器人没有路径文件「${issue.value}」${rows ? `（第 ${rows} 行）` : ''}。`
      : '未能校验路径文件。'
  }
  return humanError(issue.message)
}

function setCruiseStatus(kind, message) {
  cruiseStatusKind.value = kind
  cruiseStatus.value = message
}

async function refreshCruiseRoutes() { cruiseRoutes.value = await api('/cruise/routes') }
async function refreshCruiseRun() { cruiseRun.value = await api('/cruise') }

// Loads the same map list as the hardware page, but reports into this page's status line.
// Reusing refreshRobotMaps here would write the result somewhere the operator cannot see.
async function refreshCruiseMaps() {
  if (isCruiseBusy.value) return
  isCruiseBusy.value = true
  setCruiseStatus('muted', '正在加载机器人地图...')
  try {
    robotMaps.value = await api('/robot/maps')
    setCruiseStatus('success', `已加载 ${robotMaps.value.length} 张机器人地图。`)
  } catch (err) {
    setCruiseStatus('danger', `地图加载失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

/** Run a single point with no recording: the only way to find out whether a goal_id is real. */
async function testCruisePoint(point) {
  if (isCruiseBusy.value || cruiseRunning.value) return
  isCruiseBusy.value = true
  cruiseIssues.value = []
  setCruiseStatus('muted', `正在试跑 ${point.path_name} · #${point.goal_id}（不录制）...`)
  try {
    cruiseRun.value = await api('/cruise/start', {
      method: 'POST',
      body: JSON.stringify({
        title: `试跑 ${point.path_name}#${point.goal_id}`,
        map_name: cruiseMap.value || null,
        points: [{ path_name: point.path_name, goal_id: point.goal_id, goal_object: point.goal_object || null }],
        record: false,
        dwell_min_seconds: 0,
        dwell_max_seconds: 0,
        arrival_timeout_seconds: Number(cruiseArrivalTimeout.value),
        gimbal_scan: { enabled: false }
      })
    })
    setCruiseStatus('muted', '试跑已开始，不录制。到达或失败会显示在「运行状态」。')
  } catch (err) {
    setCruiseStatus('danger', `试跑失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

async function loadCruisePaths() {
  if (!cruiseMap.value || isCruiseBusy.value) return
  isCruiseBusy.value = true
  try {
    cruisePaths.value = await api(`/robot/paths?map_name=${encodeURIComponent(cruiseMap.value)}`)
    if (!cruiseNewPath.value && cruisePaths.value.length) cruiseNewPath.value = cruisePaths.value[0]
    setCruiseStatus('success', `已加载 ${cruisePaths.value.length} 个路径文件。`)
  } catch (err) {
    setCruiseStatus('danger', `路径文件加载失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

// Opening 巡游拍摄 with no map chosen inherits whatever the robot is already on, so the
// map is not picked twice across the two pages. Still freely overridable.
watch(active, (page) => {
  if (page !== 'shoot' || cruiseMap.value) return
  const inherited = robot.value?.map_name || selectedRobotMap.value
  if (inherited) cruiseMap.value = inherited
})

// Settings access lasts for one visit only. Leaving immediately restores the lock and revokes
// the backend token; returning therefore always starts on the blurred verification screen.
watch(active, async (page, previousPage) => {
  if (previousPage === 'settings' && page !== 'settings') {
    await lockSettings()
    return
  }
  if (page !== 'settings' || !settingsAdminUnlocked.value || !settingsAdminToken.value) return
  try {
    const result = await api('/settings/admin/status', settingsAdminOptions())
    if (!result.unlocked) handleSettingsAdminError({ status: 403 })
  } catch {
    handleSettingsAdminError({ status: 403 })
  }
})

// A watch rather than @change on the select: with v-model both fire on 'change', and the
// handler could otherwise read the previous value.
watch(tuneSourceId, () => {
  backToSource()
  tuneDuration.value = 0
  tuneIn.value = 0
  tuneOut.value = 0
})

watch(cruiseMap, () => {
  cruisePaths.value = []
  cruiseNewPath.value = ''
  if (cruiseMap.value) loadCruisePaths()
})

function addCruisePoint() {
  if (!canAddCruisePoint.value) return
  cruisePoints.value.push({
    path_name: cruiseNewPath.value,
    goal_id: Number(cruiseNewGoalId.value),
    goal_object: cruiseNewGoalObject.value.trim() || null
  })
  cruiseNewGoalId.value = Number(cruiseNewGoalId.value) + 1
}

function removeCruisePoint(index) { cruisePoints.value.splice(index, 1) }

function moveCruisePoint(index, delta) {
  const target = index + delta
  if (target < 0 || target >= cruisePoints.value.length) return
  const points = cruisePoints.value
  ;[points[index], points[target]] = [points[target], points[index]]
}

function buildCruiseRequest() {
  return {
    title: cruiseRouteName.value.trim() || '巡游',
    map_name: cruiseMap.value || null,
    points: cruisePoints.value.map((point) => ({
      path_name: point.path_name,
      goal_id: point.goal_id,
      goal_object: point.goal_object || null
    })),
    record: true,
    dwell_min_seconds: Number(cruiseDwellMin.value),
    dwell_max_seconds: Number(cruiseDwellMax.value),
    arrival_timeout_seconds: Number(cruiseArrivalTimeout.value),
    gimbal_scan: {
      enabled: cruiseScanEnabled.value,
      direction: cruiseScanDirection.value,
      yaw_offset_deg: Number(cruiseScanOffset.value),
      yaw_speed_deg_s: Number(cruiseScanSpeed.value)
    },
    auto_camerawork: cruiseAutoCamerawork.value
  }
}

function applyCruiseRequest(request) {
  cruiseMap.value = request.map_name || ''
  cruisePoints.value = (request.points || []).map((point) => ({ ...point }))
  cruiseDwellMin.value = request.dwell_min_seconds
  cruiseDwellMax.value = request.dwell_max_seconds
  cruiseArrivalTimeout.value = request.arrival_timeout_seconds
  const scan = request.gimbal_scan || {}
  cruiseScanEnabled.value = Boolean(scan.enabled)
  cruiseScanDirection.value = scan.direction || 'right'
  cruiseScanOffset.value = scan.yaw_offset_deg ?? 15
  cruiseScanSpeed.value = scan.yaw_speed_deg_s ?? 5
}

async function saveCruiseRoute() {
  if (!canSaveCruiseRoute.value || isCruiseBusy.value) return
  isCruiseBusy.value = true
  try {
    await api('/cruise/routes', {
      method: 'POST',
      body: JSON.stringify({ name: cruiseRouteName.value.trim(), request: buildCruiseRequest() })
    })
    await refreshCruiseRoutes()
    setCruiseStatus('success', `清单「${cruiseRouteName.value.trim()}」已保存。`)
  } catch (err) {
    setCruiseStatus('danger', `保存失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

function loadCruiseRoute(route) {
  applyCruiseRequest(route.request)
  cruiseRouteName.value = route.name
  cruiseIssues.value = []
  // The cruiseMap watch reloads paths when the map actually changed; load here only when
  // it did not, so a route on the current map still gets its path list.
  if (cruiseMap.value && !cruisePaths.value.length) loadCruisePaths()
  setCruiseStatus('muted', `已载入清单「${route.name}」，共 ${route.request.points.length} 个点位。`)
}

async function deleteCruiseRoute(route) {
  if (isCruiseBusy.value) return
  isCruiseBusy.value = true
  try {
    await api(`/cruise/routes/${route.id}`, { method: 'DELETE' })
    await refreshCruiseRoutes()
    setCruiseStatus('muted', `已删除清单「${route.name}」。`)
  } catch (err) {
    setCruiseStatus('danger', `删除失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

async function validateCruiseRoute(route) {
  if (isCruiseBusy.value) return
  isCruiseBusy.value = true
  cruiseIssues.value = []
  try {
    const result = await api(`/cruise/routes/${route.id}/validate`, { method: 'POST', body: '{}' })
    cruiseIssues.value = result.issues || []
    if (!result.ok) setCruiseStatus('danger', '校验未通过，请先修正上面标出的问题。')
    else if (!result.checked) setCruiseStatus('muted', '机器人不可达，地图与路径文件未能校验。')
    else if (cruiseIssues.value.length) setCruiseStatus('muted', '校验通过，但有提示。')
    else setCruiseStatus('success', '校验通过：地图与路径文件都存在。')
  } catch (err) {
    setCruiseStatus('danger', `校验失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

async function startCruiseRoute(route) {
  if (manualCaptureActive.value) {
    setCruiseStatus('danger', '原地采集进行中，请先停止采集再开始巡游。')
    return
  }
  if (isCruiseBusy.value || cruiseRunning.value) return
  isCruiseBusy.value = true
  cruiseIssues.value = []
  try {
    const result = await api(`/cruise/routes/${route.id}/start`, { method: 'POST', body: '{}' })
    cruiseRun.value = result.run
    cruiseIssues.value = result.validation?.issues || []
    await refreshCruiseRoutes()
    setCruiseStatus('success', `清单「${route.name}」已开始。`)
  } catch (err) {
    // A 409 carries the validation payload, so the operator sees which rows are wrong.
    const issues = err.detail?.issues
    if (Array.isArray(issues)) {
      cruiseIssues.value = issues
      setCruiseStatus('danger', '校验未通过，机器人未移动。')
    } else {
      setCruiseStatus('danger', `启动失败：${humanError(err.message)}`)
    }
  } finally {
    isCruiseBusy.value = false
  }
}

async function startCruise() {
  if (manualCaptureActive.value) {
    setCruiseStatus('danger', '原地采集进行中，请先停止采集再开始巡游。')
    return
  }
  if (!canStartCruise.value) return
  isCruiseBusy.value = true
  cruiseIssues.value = []
  try {
    cruiseRun.value = await api('/cruise/start', { method: 'POST', body: JSON.stringify(buildCruiseRequest()) })
    setCruiseStatus('success', `巡游已开始，共 ${cruisePoints.value.length} 个点位。`)
  } catch (err) {
    setCruiseStatus('danger', `启动失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

async function cancelCruise() {
  if (!cruiseRunning.value || isCruiseBusy.value) return
  isCruiseBusy.value = true
  setCruiseStatus('muted', '正在取消巡游并停止录制...')
  try {
    cruiseRun.value = await api('/cruise/cancel', { method: 'POST', body: '{}' })
    setCruiseStatus('muted', '巡游已取消，录制已停止。')
  } catch (err) {
    setCruiseStatus('danger', `取消失败：${humanError(err.message)}`)
  } finally {
    isCruiseBusy.value = false
  }
}

function applyCruiseSegment(data) {
  const segment = data?.segment
  const segments = cruiseRun.value?.segments
  if (!segment || !segments) return
  const position = segments.findIndex((item) => item.index === segment.index)
  if (position >= 0) segments[position] = segment
}

async function connectRobot() {
  if (isRobotBusy.value) return
  isRobotBusy.value = true
  robotCommandStatusKind.value = 'muted'
  robotCommandStatus.value = '正在连接机器人硬件...'
  try {
    robot.value = await api('/robot/connect', { method: 'POST', body: '{}' })
    robotCommandStatusKind.value = robot.value.connected ? 'success' : 'danger'
    robotCommandStatus.value = robot.value.connected ? '机器人硬件已连接。' : (robot.value.error ? humanError(robot.value.error) : '机器人硬件尚未连接。')
    if (robot.value.connected) await refreshRobotMaps(true)
  } catch (err) {
    robotCommandStatusKind.value = 'danger'
    robotCommandStatus.value = `机器人连接失败：${humanError(err.message)}`
  } finally {
    isRobotBusy.value = false
  }
}

async function refreshRobotMaps(allowBusy = false) {
  if (isRobotBusy.value && !allowBusy) return
  const previousBusy = isRobotBusy.value
  isRobotBusy.value = true
  robotCommandStatusKind.value = 'muted'
  robotCommandStatus.value = '正在加载机器人地图...'
  try {
    robotMaps.value = await api('/robot/maps')
    if (!selectedRobotMap.value && robotMaps.value.length) selectedRobotMap.value = robotMaps.value[0]
    robotCommandStatusKind.value = 'success'
    robotCommandStatus.value = `已加载 ${robotMaps.value.length} 张机器人地图。`
    if (selectedRobotMap.value) await refreshRobotPaths(false, true)
  } catch (err) {
    robotCommandStatusKind.value = 'danger'
    robotCommandStatus.value = `地图加载失败：${humanError(err.message)}`
  } finally {
    isRobotBusy.value = previousBusy
  }
}

async function switchRobotMap() {
  if (!selectedRobotMap.value || isRobotBusy.value) return
  isRobotBusy.value = true
  robotCommandStatusKind.value = 'muted'
  robotCommandStatus.value = `正在切换到地图 ${selectedRobotMap.value}...`
  try {
    const result = await api('/robot/switch-map', {
      method: 'POST',
      body: JSON.stringify({ map_name: selectedRobotMap.value })
    })
    await refreshRobot()
    robotCommandStatusKind.value = result.ok ? 'success' : 'danger'
    robotCommandStatus.value = result.ok ? `机器人地图已切换到 ${selectedRobotMap.value}。` : `机器人拒绝切换到地图 ${selectedRobotMap.value}。`
    if (result.ok) await refreshRobotPaths(false, true)
  } catch (err) {
    robotCommandStatusKind.value = 'danger'
    robotCommandStatus.value = `地图切换失败：${humanError(err.message)}`
  } finally {
    isRobotBusy.value = false
  }
}

async function refreshRobotPaths(showStatus = true, allowBusy = false) {
  if (!selectedRobotMap.value || (isRobotBusy.value && !allowBusy)) return
  const previousBusy = isRobotBusy.value
  isRobotBusy.value = true
  if (showStatus) {
    robotCommandStatusKind.value = 'muted'
    robotCommandStatus.value = `正在加载 ${selectedRobotMap.value} 的路径...`
  }
  try {
    robotPaths.value = await api(`/robot/paths?map_name=${encodeURIComponent(selectedRobotMap.value)}`)
    if (showStatus) {
      robotCommandStatusKind.value = 'success'
      robotCommandStatus.value = `已加载 ${robotPaths.value.length} 条路径。`
    }
  } catch (err) {
    robotCommandStatusKind.value = 'danger'
    robotCommandStatus.value = `路径文件加载失败：${humanError(err.message)}`
  } finally {
    isRobotBusy.value = previousBusy
  }
}

function pingBackend() { sendWs('PING') }
function setCameraAngle() { sendWs('ROBOT_CAMERA_ANGLE', { angle: cameraAngle.value }) }
function sendGimbal() { sendWs('ROBOT_GIMBAL', { ...gimbalForm.value }) }
function captureStart() {
  captureStatusKind.value = 'muted'
  captureStatus.value = '正在开始录制…'
  sendWs('CAPTURE_START', { title: captureTitle.value })
}
function captureStop() {
  captureStatusKind.value = 'muted'
  captureStatus.value = '正在停止录制并保存到 Windows…'
  sendWs('CAPTURE_STOP')
}
function capturePhoto() {
  captureStatusKind.value = 'muted'
  captureStatus.value = '正在拍照并保存到 Windows…'
  sendWs('ROBOT_CAPTURE_PHOTO')
}
function captureNoteSend() { sendWs('CAPTURE_NOTE', { note: captureNote.value }); captureNote.value = '' }

async function importMedia() {
  if (!window.desktopApi) throw new Error('桌面桥接不可用。请从 Electron 应用打开，而不是直接访问浏览器地址。')
  const files = await window.desktopApi.selectMediaFiles()
  for (const file of files) await api('/media/import', { method: 'POST', body: JSON.stringify({ path: file }) })
  await Promise.all([refreshMedia(), refreshVault()])
}

async function downloadMedia() {
  const url = downloadUrl.value.trim()
  if (!url || isDownloading.value) return
  isDownloading.value = true
  downloadStatusKind.value = 'muted'
  downloadStatus.value = '正在导入媒体链接...'
  log('正在导入媒体链接')
  try {
    const item = await api('/media/download', { method: 'POST', body: JSON.stringify({ url }) })
    downloadUrl.value = ''
    await Promise.all([refreshMedia(), refreshVault()])
    downloadStatusKind.value = 'success'
    downloadStatus.value = `已导入${mediaKindLabel(item.kind)}：${shortPath(item.path)}`
    log('媒体链接导入完成')
  } catch (err) {
    downloadStatusKind.value = 'danger'
    downloadStatus.value = `导入失败：${humanError(err.message)}`
    log(`导入失败：${humanError(err.message)}`)
  } finally {
    isDownloading.value = false
  }
}

async function cleanupSafeMedia() {
  if (!window.confirm('一键清理只会删除缓存、预览和特效源图缓存；导出、特效、导入、音乐、旁白不会被批量删除。继续吗？')) return
  cleanupStatusKind.value = 'muted'
  cleanupStatus.value = '正在清理缓存和预览...'
  try {
    const result = await api('/media/cleanup-safe', { method: 'POST', body: '{}' })
    await Promise.all([refreshVault(), refreshMedia(), refreshSeedanceAssets()])
    cleanupStatusKind.value = 'success'
    cleanupStatus.value = `已清理 ${result.deleted_count} 个文件，释放 ${formatBytes(result.freed_bytes)}。`
  } catch (err) {
    cleanupStatusKind.value = 'danger'
    cleanupStatus.value = `清理失败：${humanError(err.message)}`
  }
}

async function createAutomationJobs(forceWithoutPreference = false) {
  const videoIds = selectedSourceIds.value.slice(0, MAX_SOURCE_VIDEOS)
  if (!videoIds.length || isCreatingJob.value) return
  if (forceWithoutPreference !== true && !savedFramingConfigured.value) {
    showFramingSetupPrompt.value = true
    return
  }
  isCreatingJob.value = true
  jobStatusKind.value = 'muted'
  jobStatus.value = '正在创建自动化任务...'
  try {
    const created = await api('/jobs/batch', {
      method: 'POST',
      body: JSON.stringify({
        title: editTitle.value,
        media_ids: videoIds,
        music_media_ids: selectedAutomationMusicIds.value.slice(0, MAX_AUTOMATION_ITEMS),
        voiceover_media_ids: selectedAutomationVoiceoverIds.value.slice(0, MAX_AUTOMATION_ITEMS),
        intro_effect_media_ids: selectedIntroEffectIds.value.slice(0, MAX_AUTOMATION_ITEMS),
        outro_effect_media_ids: selectedOutroEffectIds.value.slice(0, MAX_AUTOMATION_ITEMS),
        effect_scope: effectScope.value,
        effect_cover_audio: effectCoverAudio.value,
        output_count: normalizedAutomationOutputCount.value,
        target_duration_seconds: targetDurationSeconds.value,
        mute_original_audio: muteOriginalAudio.value,
        // Music structure is always used when it is reliable; ambient or absent music falls
        // back automatically, so the customer never has to understand a beat-detection switch.
        beat_sync: true,
        subtitles: subtitlesOn.value && canUseSubtitles.value,
        subtitle_font: subtitleFont.value,
        subtitle_size: subtitleSize.value,
        ...editingPolicyPayload.value
      })
    })
    active.value = 'queue'
    await Promise.all([refreshJobs(), refreshSettings()])
    jobStatusKind.value = 'success'
    const asked = normalizedAutomationOutputCount.value
    const outOfQuota = settings.value?.automation?.remaining_today === 0
    const capped = created.length < asked
      ? `（原定 ${asked} 条，${outOfQuota ? '已达今日上限' : '已按可用素材调整'}）`
      : ''
    jobStatus.value = `已创建 ${created.length} 条自动化剪辑任务${capped}。`
  } catch (err) {
    jobStatusKind.value = 'danger'
    jobStatus.value = `自动化任务创建失败：${humanError(err.message)}`
    log(`自动化任务创建失败：${humanError(err.message)}`)
  } finally {
    isCreatingJob.value = false
  }
}

function selectAllSources() {
  selectedSourceIds.value = sourceVideoItems.value.slice(0, MAX_SOURCE_VIDEOS).map((item) => item.id)
}

function selectAllMusicPool() {
  selectedAutomationMusicIds.value = audioItems.value.slice(0, MAX_AUTOMATION_ITEMS).map((item) => item.id)
}

function selectAllVoiceoverPool() {
  selectedAutomationVoiceoverIds.value = voiceoverItems.value.slice(0, MAX_AUTOMATION_ITEMS).map((item) => item.id)
}

function toggleSourceSelection(id, checked) {
  toggleLimitedSelection(selectedSourceIds, id, checked, MAX_SOURCE_VIDEOS)
}

function toggleAutomationMusic(id, checked) {
  toggleLimitedSelection(selectedAutomationMusicIds, id, checked, MAX_AUTOMATION_ITEMS)
}

function toggleAutomationVoiceover(id, checked) {
  toggleLimitedSelection(selectedAutomationVoiceoverIds, id, checked, MAX_AUTOMATION_ITEMS)
}
function toggleIntroEffect(id, checked) {
  toggleLimitedSelection(selectedIntroEffectIds, id, checked, MAX_AUTOMATION_ITEMS)
}
function toggleOutroEffect(id, checked) {
  toggleLimitedSelection(selectedOutroEffectIds, id, checked, MAX_AUTOMATION_ITEMS)
}

function toggleLimitedSelection(targetRef, id, checked, limit) {
  const current = targetRef.value
  if (checked) {
    if (!current.includes(id) && current.length < limit) targetRef.value = [...current, id]
    return
  }
  targetRef.value = current.filter((item) => item !== id)
}

// ── 微调 ──────────────────────────────────────────────────────────────────────
// A visual editor rather than a table of numbers: pick any clip, mark a stretch, drop it
// on the track. Clips always sit end to end, so dragging only ever reorders and no black
// frames can appear.

const tuneImportedSources = computed(() =>
  media.value.filter((item) => item.kind === 'video' && itemRole(item) === 'raw_video')
)
const tuneExportSources = computed(() =>
  media.value.filter((item) => item.kind === 'video' && itemRole(item) === 'export')
)
const tuneSource = computed(() =>
  media.value.find((item) => item.id === tuneSourceId.value) || null
)
const tuneVideoSrc = computed(() => {
  if (tuneMode.value === 'result') return tunePreviewSrc.value
  return tuneSource.value ? mediaFileUrl(tuneSource.value.path) : ''
})
const tunePreviewSrc = ref('')
const tuneTotal = computed(() => tuneClips.value.reduce((sum, clip) => sum + clip.duration, 0))
const tuneSelectionLength = computed(() => Math.max(0, tuneOut.value - tuneIn.value))
const tuneHeadPercent = computed(() =>
  tuneDuration.value > 0 ? Math.min(100, (tuneHead.value / tuneDuration.value) * 100) : 0
)
const tuneSelectionStyle = computed(() => {
  if (!tuneDuration.value) return { left: '0%', width: '0%' }
  return {
    left: `${(tuneIn.value / tuneDuration.value) * 100}%`,
    width: `${(tuneSelectionLength.value / tuneDuration.value) * 100}%`
  }
})
// Only the states the player itself cannot show. How to mark in and out belongs beside the
// buttons that do it, not stacked under the picture with everything else.
const tuneModeLabel = computed(() => {
  if (tuneMode.value === 'result') return '正在预览成片（未渲染，直接跳着播的效果预览）'
  if (!tuneSourceId.value) return '先选一个素材，上面就能播放原片。'
  return ''
})
const tuneEffectSources = computed(() =>
  media.value.filter((item) => itemRole(item) === 'seedance_effect')
)
const tuneSourceIsImage = computed(() => tuneSource.value?.kind === 'image')
const tuneIsEffect = computed(() => Boolean(tuneSource.value) && itemRole(tuneSource.value) === 'seedance_effect')
// One <img> serves two jobs: inspecting a still before placing it, and standing in for the
// video during 预览成片, which plays by seeking and has nothing to seek to on a still.
const tunePreviewClip = computed(() => tuneClips.value[tunePreviewIndex.value] || null)
const tuneShowStill = computed(() => (
  tuneMode.value === 'result'
    ? tunePreviewClip.value?.kind === 'image'
    : tuneSourceIsImage.value
))
const tuneStillSrc = computed(() => {
  if (tuneMode.value === 'result') {
    return tunePreviewClip.value ? mediaFileUrl(tunePreviewClip.value.source_path) : ''
  }
  return tuneSource.value ? mediaFileUrl(tuneSource.value.path) : ''
})
const tunePlaceDuration = computed(() => (
  tuneSourceIsImage.value
    ? Math.max(0.2, Number(tuneImageDuration.value) || 0)
    : tuneSelectionLength.value
))
const canPlaceTune = computed(() => Boolean(tuneSourceId.value) && tunePlaceDuration.value >= 0.2)
const tunePlayheadPercent = computed(() =>
  tuneTotal.value > 0 ? Math.min(100, (tunePlayhead.value / tuneTotal.value) * 100) : 0
)
const tuneBedName = computed(() => (
  tuneBed.value ? String(tuneBed.value.source_path).split('/').pop() : ''
))
const tuneHasVideoEffects = computed(() =>
  tuneClips.value.some((clip) => clip.is_effect && clip.kind !== 'image')
)
const tuneBaseHasVoiceover = computed(() => Boolean(
  tuneKeepSound.value
  && tuneBed.value
  && (tuneBed.value.has_voiceover || tuneSubtitles.value?.cues?.length)
))
const tuneEffectAudioVolume = computed(() => {
  if (tuneEffectSoundMode.value === 'ducked') return 0.3
  if (tuneEffectSoundMode.value === 'full') return 1.0
  return tuneBaseHasVoiceover.value ? 0.3 : 1.0
})
const tuneEffectAudioSummary = computed(() => {
  if (!tuneKeepEffectSound.value) return '特效声音已关闭'
  if (tuneEffectSoundMode.value === 'auto') {
    return tuneBaseHasVoiceover.value ? '检测到旁白 · 自动 30%' : '没有保留的旁白 · 自动 100%'
  }
  return tuneEffectSoundMode.value === 'ducked' ? '固定 30%' : '固定 100%'
})

function onTuneMeta() {
  const el = tuneVideoEl.value
  if (!el) return
  if (tuneMode.value === 'source') {
    tuneDuration.value = Number.isFinite(el.duration) ? el.duration : 0
    tuneIn.value = 0
    // An effect is generated to be used whole; a source clip is something you cut down.
    tuneOut.value = tuneIsEffect.value ? tuneDuration.value : Math.min(5, tuneDuration.value)
  }
}

function onTuneTime() {
  const el = tuneVideoEl.value
  if (!el) return
  if (tuneMode.value === 'source') {
    tuneHead.value = el.currentTime
    return
  }
  const clip = tuneClips.value[tunePreviewIndex.value]
  if (clip && el.currentTime >= clip.start + clip.duration - 0.05) advancePreview()
}

function scrubTo(event) {
  const el = tuneVideoEl.value
  if (!el || !tuneDuration.value) return
  const box = event.currentTarget.getBoundingClientRect()
  el.currentTime = Math.max(0, Math.min(tuneDuration.value, ((event.clientX - box.left) / box.width) * tuneDuration.value))
}

function markIn() {
  tuneIn.value = Number((tuneVideoEl.value?.currentTime || 0).toFixed(2))
  if (tuneOut.value <= tuneIn.value) tuneOut.value = Math.min(tuneDuration.value, tuneIn.value + 5)
}

function markOut() {
  const value = Number((tuneVideoEl.value?.currentTime || 0).toFixed(2))
  tuneOut.value = value > tuneIn.value ? value : Math.min(tuneDuration.value, tuneIn.value + 0.5)
}

function markWhole() {
  tuneIn.value = 0
  tuneOut.value = tuneDuration.value
}

/** Snapshot before every change, so nothing is a one-way door until you export. */
function pushTuneUndo() {
  tuneUndoStack.value.push({
    clips: tuneClips.value.map((clip) => ({ ...clip })),
    bed: tuneBed.value ? { ...tuneBed.value } : null,
    subtitles: tuneSubtitles.value,
    subtitleNote: tuneSubtitleNote.value,
    playhead: tunePlayhead.value
  })
  if (tuneUndoStack.value.length > TUNE_UNDO_LIMIT) tuneUndoStack.value.shift()
}

function undoTune() {
  const previous = tuneUndoStack.value.pop()
  if (!previous) return
  tuneClips.value = previous.clips
  tuneBed.value = previous.bed
  tuneSubtitles.value = previous.subtitles ?? null
  tuneSubtitleNote.value = previous.subtitleNote ?? ''
  tunePlayhead.value = previous.playhead
  if (tuneMode.value === 'result') backToSource()
}

function newTuneClip() {
  const source = tuneSource.value
  if (!source) return null
  const isImage = tuneSourceIsImage.value
  return {
    uid: ++tuneUid,
    media_id: source.id,
    source_path: source.path,
    name: shortPath(source.path).split('/').pop(),
    start: isImage ? 0 : Number(tuneIn.value.toFixed(2)),
    duration: Number(tunePlaceDuration.value.toFixed(2)),
    kind: isImage ? 'image' : 'video',
    is_effect: tuneIsEffect.value
  }
}

/** Pin the soundtrack to the first real clip, if it has not been pinned already. */
function adoptTuneBed(clip, offsetSeconds) {
  if (tuneBed.value || !clip || clip.is_effect || clip.kind === 'image') return
  const sourceItem = media.value.find((item) => item.id === clip.media_id)
  tuneBed.value = {
    source_path: clip.source_path,
    source_start: clip.start,
    timeline_start: offsetSeconds,
    has_voiceover: Boolean(sourceItem?.metadata?.has_voiceover)
  }
  // The words belong to the sound, so they come from wherever the sound came from. Fetched at
  // the moment the bed is pinned, because that is the only point at which we know which video
  // the narration is being taken out of.
  loadTuneSubtitles(clip.media_id)
}

// Cues carried over from the export the soundtrack was taken from, so re-cutting can put them
// back over whatever picture ends up underneath — a still, an effect clip, anything.
const tuneSubtitles = ref(null)
const tuneSubtitleNote = ref('')
const tuneSubtitleNoteKind = ref('muted')

function clearTuneSubtitles() {
  tuneSubtitles.value = null
  tuneSubtitleNote.value = ''
}

async function loadTuneSubtitles(mediaId) {
  tuneSubtitles.value = null
  tuneSubtitleNote.value = ''
  if (!mediaId) return
  try {
    const info = await api(`/subtitles/track?media_id=${encodeURIComponent(mediaId)}`)
    if (tuneBed.value) tuneBed.value.has_voiceover = Boolean(info.has_voiceover)
    if (info.has_burned_subtitles) {
      // Its subtitles are pixels now and cannot be moved. Re-cutting drags them to the wrong
      // times and slices them mid-word, so say so rather than quietly producing that.
      tuneSubtitleNoteKind.value = 'warn'
      tuneSubtitleNote.value = '这个成片已经把字幕压在画面里了，重新剪会把字幕一起剪乱。建议改用同一组里的「母版（无字幕）」。'
      return
    }
    if (info.problem) {
      tuneSubtitleNoteKind.value = 'warn'
      tuneSubtitleNote.value = info.problem
      return
    }
    if (info.track?.cues?.length) {
      tuneSubtitles.value = info.track
      tuneSubtitleNoteKind.value = 'muted'
      tuneSubtitleNote.value = `已带上原字幕 ${info.track.cues.length} 句，渲染时会重新压到新画面上。`
    }
  } catch (err) {
    tuneSubtitleNoteKind.value = 'warn'
    tuneSubtitleNote.value = `原字幕没读到：${humanError(err.message)}`
  }
}

function placeAtStart() {
  const clip = newTuneClip()
  if (!clip) return
  pushTuneUndo()
  // The sound moves with the picture, so nothing that follows can drift out of step.
  if (tuneBed.value) tuneBed.value.timeline_start += clip.duration
  tuneClips.value.unshift(clip)
  adoptTuneBed(clip, 0)
  afterTuneEdit()
}

function placeAtEnd() {
  const clip = newTuneClip()
  if (!clip) return
  pushTuneUndo()
  const offset = tuneTotal.value
  tuneClips.value.push(clip)
  adoptTuneBed(clip, offset)
  afterTuneEdit()
}

/** Paint over the picture from the playhead for the clip's own length.
 *
 * Nothing shifts, so the soundtrack keeps playing underneath exactly where it was. Anything
 * running past the end simply extends the edit; the length can only grow, which is why the
 * sound can never end up cut short. */
function placeOverwrite() {
  const clip = newTuneClip()
  if (!clip || !tuneClips.value.length) return
  const point = Math.max(0, Math.min(tuneTotal.value, tunePlayhead.value))
  const finish = point + clip.duration
  const before = []
  const after = []
  let cursor = 0

  for (const existing of tuneClips.value) {
    const start = cursor
    const end = cursor + existing.duration
    cursor = end
    if (end <= point + 0.001) { before.push(existing); continue }
    if (start >= finish - 0.001) { after.push(existing); continue }
    if (start < point - 0.001) {
      before.push({ ...existing, uid: ++tuneUid, duration: Number((point - start).toFixed(2)) })
    }
    if (end > finish + 0.001) {
      const consumed = finish - start
      after.push({
        ...existing,
        uid: ++tuneUid,
        // A still has no in-point to advance; only its remaining length changes.
        start: existing.kind === 'image' ? 0 : Number((existing.start + consumed).toFixed(2)),
        duration: Number((end - finish).toFixed(2))
      })
    }
  }

  pushTuneUndo()
  const keep = (item) => item.duration >= 0.05
  tuneClips.value = [...before.filter(keep), clip, ...after.filter(keep)]
  adoptTuneBed(clip, point)
  afterTuneEdit()
}

function afterTuneEdit() {
  tunePlayhead.value = Math.min(tunePlayhead.value, tuneTotal.value)
  if (tuneMode.value === 'result') backToSource()
}

function setPlayhead(event) {
  if (!tuneTotal.value) return
  const box = event.currentTarget.getBoundingClientRect()
  const ratio = (event.clientX - box.left) / box.width
  tunePlayhead.value = Number((Math.max(0, Math.min(1, ratio)) * tuneTotal.value).toFixed(2))
}

function removeTuneClip(index) {
  pushTuneUndo()
  tuneClips.value.splice(index, 1)
  if (!tuneClips.value.length) { tuneBed.value = null; clearTuneSubtitles() }
  afterTuneEdit()
}

function dropTuneClip(index) {
  const from = tuneDragIndex.value
  if (from < 0 || from === index) return
  pushTuneUndo()
  const [clip] = tuneClips.value.splice(from, 1)
  tuneClips.value.splice(index, 0, clip)
  tuneDragIndex.value = -1
}

function clearTune() {
  pushTuneUndo()
  tuneClips.value = []
  tuneBed.value = null
  clearTuneSubtitles()
  tunePlayhead.value = 0
  backToSource()
}

/** Play the assembled edit by jumping between segments. Nothing is rendered. */
async function playResult() {
  if (!tuneClips.value.length) return
  tuneMode.value = 'result'
  tunePreviewIndex.value = -1
  await advancePreview()
}

async function advancePreview() {
  const next = tunePreviewIndex.value + 1
  const clip = tuneClips.value[next]
  if (!clip) { backToSource(); return }
  tunePreviewIndex.value = next

  const el = tuneVideoEl.value
  if (clip.kind === 'image') {
    // There is nothing to seek on a still, so the preview holds it on a timer for exactly as
    // long as the export will. Skipping it here would make 预览成片 disagree with the file.
    el?.pause()
    tuneStillTimer = window.setTimeout(() => {
      if (tuneMode.value === 'result') advancePreview()
    }, Math.max(100, clip.duration * 1000))
    return
  }

  const url = mediaFileUrl(clip.source_path)
  if (!el) return
  if (tunePreviewSrc.value !== url) {
    tunePreviewSrc.value = url
    await nextTick()
    await new Promise((resolve) => {
      el.addEventListener('loadedmetadata', resolve, { once: true })
    })
  }
  el.currentTime = clip.start
  if (clip.is_effect) {
    el.muted = !tuneKeepEffectSound.value
    el.volume = tuneEffectAudioVolume.value
  } else {
    el.muted = false
    el.volume = 1
  }
  el.play().catch(() => {})
}

function backToSource() {
  if (tuneStillTimer) { window.clearTimeout(tuneStillTimer); tuneStillTimer = null }
  tuneVideoEl.value?.pause()
  tuneMode.value = 'source'
  tunePreviewIndex.value = -1
  tunePreviewSrc.value = ''
}

async function renderTune() {
  if (!tuneClips.value.length || isRenderingTimeline.value) return
  isRenderingTimeline.value = true
  jobStatusKind.value = 'muted'
  jobStatus.value = '正在提交渲染...'
  try {
    let cursor = 0
    const clips = tuneClips.value.map((clip) => {
      const entry = {
        media_id: clip.media_id,
        source_path: clip.source_path,
        start: clip.start,
        duration: clip.duration,
        timeline_start: cursor,
        kind: clip.kind || 'video',
        // Effect sound is a separate aligned layer. It never becomes the bed that carries
        // narration, so enabling it cannot move the voice or its subtitle timestamps.
        include_audio: Boolean(tuneKeepEffectSound.value && clip.is_effect && clip.kind !== 'image'),
        audio_volume: clip.is_effect ? tuneEffectAudioVolume.value : 1.0
      }
      cursor += clip.duration
      return entry
    })
    await api('/timeline/render', {
      method: 'POST',
      body: JSON.stringify({
        title: editTitle.value || '微调成片',
        clips,
        // Left blank on purpose: the backend names the export file.
        output_path: '',
        // With no saved choice the backend reads the first source's native frame and fits
        // later clips without cropping. An explicit preset keeps the selected cover crop.
        output_fit: savedFramingConfigured.value ? 'cover' : 'contain',
        output_width: savedOutputFrame.value?.width,
        output_height: savedOutputFrame.value?.height,
        output_crop_x: savedFramingConfigured.value ? savedOutputCrop.value.x : undefined,
        output_crop_y: savedFramingConfigured.value ? savedOutputCrop.value.y : undefined,
        target_duration_seconds: Math.max(1, Math.min(180, cursor)),
        // The soundtrack comes from the bed, laid unbroken under the cuts. Per-clip audio
        // would chop the sound at every edit, which is the thing this panel must not do.
        mute_original_audio: true,
        audio_bed: tuneKeepSound.value && tuneBed.value ? { ...tuneBed.value } : null,
        // Only alongside the sound they were timed against. Without the bed there is nothing
        // anchoring them, and cues placed by guesswork are worse than none.
        subtitles: tuneKeepSound.value && tuneBed.value && tuneSubtitles.value
          ? {
              cues: tuneSubtitles.value.cues,
              font: tuneSubtitles.value.font,
              size: tuneSubtitles.value.size,
              side_margin: tuneSubtitles.value.side_margin,
              bottom_margin: tuneSubtitles.value.bottom_margin,
              outline: tuneSubtitles.value.outline,
              shadow: tuneSubtitles.value.shadow,
              primary_colour: tuneSubtitles.value.primary_colour,
              outline_colour: tuneSubtitles.value.outline_colour,
              max_lines: tuneSubtitles.value.max_lines
            }
          : null,
        beat_sync: false
      })
    })
    await refreshJobs()
    jobStatusKind.value = 'success'
    jobStatus.value = '已提交渲染，进度在「渲染队列」。'
  } catch (err) {
    jobStatusKind.value = 'danger'
    jobStatus.value = `渲染提交失败：${humanError(err.message)}`
  } finally {
    isRenderingTimeline.value = false
  }
}

function mediaFileUrl(path) {
  const cfg = apiConfig.value
  if (!cfg || !path) return ''
  return `${cfg.baseUrl}/media/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(cfg.token)}`
}

/** Stop at the clip's out point instead of running on into the next one. */
/** Remove an effect, including the failed attempts that otherwise pile up in the list. */
async function deleteSeedanceAsset(asset) {
  try {
    await api(`/seedance/assets/${asset.id}`, { method: 'DELETE' })
    await Promise.all([refreshSeedanceAssets(), refreshMedia(), refreshVault()])
    seedanceStatusKind.value = 'muted'
    seedanceStatus.value = `已删除特效：${asset.name}`
  } catch (err) {
    seedanceStatusKind.value = 'danger'
    seedanceStatus.value = `删除失败：${humanError(err.message)}`
  }
}

/** Remove an imported clip from the library. The operator's own file is left alone. */
async function forgetSourceItem(item) {
  try {
    await api(`/media/${item.id}/forget`, { method: 'POST', body: '{}' })
    selectedSourceIds.value = selectedSourceIds.value.filter((id) => id !== item.id)
    await Promise.all([refreshMedia(), refreshVault()])
  } catch (err) {
    jobStatusKind.value = 'danger'
    jobStatus.value = `移出失败：${humanError(err.message)}`
  }
}

function startRename(path, currentName) {
  renameTarget.value = path
  renameOldName.value = currentName
  renameValue.value = currentName
  renameStatus.value = ''
}

function cancelRename() {
  renameTarget.value = null
  renameValue.value = ''
  renameStatus.value = ''
}

/** Renames the file on disk, so every screen and Finder agree on one name. */
async function confirmRename() {
  const path = renameTarget.value
  if (!path || !renameValue.value.trim() || isRenaming.value) return
  let item = media.value.find((entry) => entry.path === path)
  if (!item) {
    // 媒体库 lists files found by scanning, which may not be in the media list yet.
    await refreshMedia().catch(() => {})
    item = media.value.find((entry) => entry.path === path)
  }
  if (!item) {
    renameStatusKind.value = 'danger'
    renameStatus.value = '媒体库里找不到这个文件，先刷新一下。'
    return
  }
  isRenaming.value = true
  try {
    const updated = await api(`/media/${item.id}/rename`, {
      method: 'POST',
      body: JSON.stringify({ name: renameValue.value.trim() })
    })
    // Anything already placed on the manual timeline must follow the file.
    for (const clip of tuneClips.value) {
      if (clip.source_path === path) {
        clip.source_path = updated.path
        clip.name = updated.path.split('/').pop()
      }
    }
    await Promise.all([refreshMedia(), refreshVault(), refreshTtsAssets(), refreshSeedanceAssets()])
    cancelRename()
  } catch (err) {
    renameStatusKind.value = 'danger'
    renameStatus.value = humanError(err.message)
  } finally {
    isRenaming.value = false
  }
}

function onFrameMeta(event) {
  frameDuration.value = Number(event.target.duration) || 0
  frameHead.value = 0
}

function onFrameTime(event) {
  frameHead.value = Number(event.target.currentTime) || 0
}

function stepFrame(direction) {
  const player = frameVideoEl.value
  if (!player || !frameDuration.value) return
  const next = Math.min(frameDuration.value, Math.max(0, player.currentTime + direction * FRAME_STEP_SECONDS))
  player.currentTime = next
  frameHead.value = next
}

/** Cut the frame server-side and show it back, so what you approve is what gets sent. */
async function grabFrame() {
  if (!selectedSeedanceVideoId.value || isGrabbingFrame.value) return
  isGrabbingFrame.value = true
  seedanceStatusKind.value = 'muted'
  try {
    grabbedFrame.value = await api('/seedance/frame', {
      method: 'POST',
      body: JSON.stringify({
        source_video_media_id: selectedSeedanceVideoId.value,
        timestamp_seconds: Number(frameHead.value || 0)
      })
    })
    seedanceStatus.value = ''
  } catch (err) {
    seedanceStatusKind.value = 'danger'
    seedanceStatus.value = `取帧失败：${humanError(err.message)}`
  } finally {
    isGrabbingFrame.value = false
  }
}

function clearGrabbedFrame() {
  grabbedFrame.value = null
}

async function generateSeedanceEffect() {
  if (!seedancePrompt.value.trim() || isGeneratingSeedance.value) return
  isGeneratingSeedance.value = true
  seedanceStatusKind.value = 'muted'
  seedanceStatus.value = '正在提交特效生成任务...'
  try {
    const body = {
      title: seedanceTitle.value || (seedanceOutput.value === 'image' ? '图片特效' : '视频特效'),
      prompt: seedancePrompt.value.trim(),
      output: seedanceOutput.value,
      duration_seconds: Number(seedanceDuration.value || settings.value?.seedance?.default_duration_seconds || 5),
      source_image_media_id: seedanceSourceMode.value === 'image' ? (selectedSeedanceImageId.value || null) : null,
      // Only a confirmed frame counts. Picking no video means no source, never a guess.
      source_video_media_id: usingGrabbedFrame.value ? selectedSeedanceVideoId.value : null,
      timestamp_seconds: usingGrabbedFrame.value ? Number(grabbedFrame.value.timestamp_seconds) : null,
      reuse_existing: true
    }
    const result = await api('/seedance/generate', { method: 'POST', body: JSON.stringify(body) })
    await Promise.all([refreshSeedanceAssets(), refreshMedia(), refreshVault()])
    seedanceStatusKind.value = result.reused ? 'success' : 'muted'
    seedanceStatus.value = result.reused
      ? `已复用特效：${result.asset.name}`
      : result.asset.kind === 'video'
        ? `特效任务已提交，今日剩余约 ${result.quota.remaining} 次（实际次数与生成时长有关）。`
        : `特效任务已提交，今日剩余 ${result.quota.count_remaining} 次。`
  } catch (err) {
    seedanceStatusKind.value = 'danger'
    seedanceStatus.value = `特效生成失败：${humanError(err.message)}`
  } finally {
    isGeneratingSeedance.value = false
  }
}

async function openAsset(asset) {
  return openPath(asset.path)
}

async function openPath(path) {
  try {
    await window.desktopApi.openPath(path)
  } catch (err) {
    log(`打开失败：${humanError(err.message)}`)
  }
}

async function revealAsset(asset) {
  return revealPath(asset.path)
}

async function revealPath(path) {
  try {
    await window.desktopApi.revealPath(path)
  } catch (err) {
    log(`定位失败：${humanError(err.message)}`)
  }
}

/** Remove an imported clip from the library. The operator's own file is left alone. */
async function forgetAsset(asset) {
  try {
    await api(`/media/${asset.id}/forget`, { method: 'POST', body: '{}' })
    await Promise.all([refreshMedia(), refreshVault()])
    cleanupStatusKind.value = 'success'
    cleanupStatus.value = `已从媒体库移出「${asset.name}」，本地文件未删除。`
  } catch (err) {
    cleanupStatusKind.value = 'danger'
    cleanupStatus.value = `移出失败：${humanError(err.message)}`
  }
}

async function trashAsset(asset) {
  if (!window.confirm(`删除 ${asset.name}？文件会移入系统废纸篓。`)) return
  try {
    await window.desktopApi.trashPath(asset.path)
    log(`已删除 ${asset.name}`)
    await Promise.all([refreshMedia(), refreshVault()])
  } catch (err) {
    log(`删除失败：${humanError(err.message)}`)
  }
}

async function trashVaultGroup(group) {
  // Named individually in the prompt. "Delete 2 files" is not enough to decide by when one of
  // them is the only clean copy you have to re-edit from.
  const names = group.members.map((member) => `· ${member.variant_label || member.name}`).join('\n')
  if (!window.confirm(`删除「${group.name}」的全部 ${group.members.length} 个视频？\n${names}\n文件会移入系统废纸篓。`)) return
  const failed = []
  for (const member of group.members) {
    if (!member.can_delete) continue
    try {
      await window.desktopApi.trashPath(member.path)
    } catch (err) {
      failed.push(`${member.name}（${humanError(err.message)}）`)
    }
  }
  // Reported per file rather than as one success. A partial delete that says "done" leaves the
  // operator believing the space was freed and the master gone when one of them is still there.
  if (failed.length) log(`部分删除失败：${failed.join('；')}`)
  else log(`已删除「${group.name}」的 ${group.members.length} 个视频`)
  await Promise.all([refreshMedia(), refreshVault()])
}

function shortPath(path) {
  return String(path || '').split('/').pop() || path
}

function isExportPath(path) {
  return String(path || '').includes('/exports/') || String(path || '').includes('\\exports\\')
}

function isTtsPath(path) {
  return String(path || '').includes('/data/tts/') || String(path || '').includes('\\data\\tts\\')
}

function isSeedanceEffectPath(path) {
  return String(path || '').includes('/data/seedance/effects/') || String(path || '').includes('\\data\\seedance\\effects\\')
}

function itemRole(item) {
  if (item?.metadata?.role) return item.metadata.role
  if (item?.kind === 'video' && isSeedanceEffectPath(item.path)) return 'seedance_effect'
  if (item?.kind === 'video' && isExportPath(item.path)) return 'export'
  if (item?.kind === 'audio' && isTtsPath(item.path)) return 'tts_voice'
  if (item?.kind === 'video') return 'raw_video'
  if (item?.kind === 'audio') return 'music'
  if (item?.kind === 'image') return 'image'
  return 'unknown'
}

function secretState(status) {
  return status?.configured ? `已保存 ${status.masked || ''} · 留空＝不修改` : '未填写'
}

function secretPlaceholder(secret, fallback) {
  return secret?.configured ? `已配置：${secret.masked}` : fallback
}

function configuredPlaceholder(value, fallback) {
  return value ? '已配置' : fallback
}

function settingsAdminOptions(options = {}) {
  return {
    ...options,
    headers: {
      ...(options.headers || {}),
      'x-admin-token': settingsAdminToken.value
    }
  }
}

function handleSettingsAdminError(err) {
  if (err?.status !== 403) return false
  if (settingsAdminExpiryTimer) clearTimeout(settingsAdminExpiryTimer)
  settingsAdminExpiryTimer = null
  settingsAdminUnlocked.value = false
  settingsAdminToken.value = ''
  settingsAdminPassword.value = ''
  settingsAdminStatusKind.value = 'danger'
  settingsAdminStatus.value = '管理员验证已失效，请重新验证。'
  return true
}

async function unlockSettings() {
  if (!settingsAdminUsername.value || !settingsAdminPassword.value || isUnlockingSettings.value) return
  isUnlockingSettings.value = true
  settingsAdminStatus.value = ''
  try {
    const result = await api('/settings/admin/unlock', {
      method: 'POST',
      body: JSON.stringify({
        username: settingsAdminUsername.value,
        password: settingsAdminPassword.value
      })
    })
    // Navigation may happen while the request is in flight. Never let a late successful reply
    // unlock Settings in the background; revoke that just-issued token instead.
    if (active.value !== 'settings') {
      await api('/settings/admin/lock', {
        method: 'POST',
        body: '{}',
        headers: { 'x-admin-token': result.token }
      })
      return
    }
    settingsAdminToken.value = result.token
    settingsAdminUnlocked.value = true
    settingsAdminUsername.value = ''
    settingsAdminPassword.value = ''
    settingsAdminStatus.value = ''
    settingsStatus.value = ''
    if (settingsAdminExpiryTimer) clearTimeout(settingsAdminExpiryTimer)
    settingsAdminExpiryTimer = setTimeout(() => {
      settingsAdminUnlocked.value = false
      settingsAdminToken.value = ''
      settingsAdminStatusKind.value = 'danger'
      settingsAdminStatus.value = '管理员验证已失效，请重新验证。'
      settingsAdminExpiryTimer = null
    }, Number(result.expires_in_seconds || 0) * 1000)
    await refreshSettings()
  } catch (err) {
    settingsAdminPassword.value = ''
    settingsAdminStatusKind.value = 'danger'
    settingsAdminStatus.value = humanError(err.message)
  } finally {
    isUnlockingSettings.value = false
  }
}

async function lockSettings() {
  const token = settingsAdminToken.value
  if (settingsAdminExpiryTimer) clearTimeout(settingsAdminExpiryTimer)
  settingsAdminExpiryTimer = null
  settingsAdminUnlocked.value = false
  settingsAdminToken.value = ''
  settingsAdminUsername.value = ''
  settingsAdminPassword.value = ''
  settingsAdminStatus.value = ''
  settingsStatus.value = ''
  if (!token) return
  try {
    await api('/settings/admin/lock', {
      method: 'POST',
      body: '{}',
      headers: { 'x-admin-token': token }
    })
  } catch {
    // Local state is already locked; a stopped backend cannot keep its in-memory session alive.
  }
}

async function saveSettings() {
  isSavingSettings.value = true
  settingsStatusKind.value = 'muted'
  settingsStatus.value = '正在保存设置...'
  try {
    settings.value = await api('/settings', settingsAdminOptions({
      method: 'PUT',
      body: JSON.stringify(settingsForm.value)
    }))
    await Promise.all([refreshSettings(), refreshRobot(), refreshSeedanceAssets()])
    settingsStatusKind.value = 'success'
    settingsStatus.value = '设置已保存。服务和机器人连接会立即生效。'
    return true
  } catch (err) {
    handleSettingsAdminError(err)
    settingsStatusKind.value = 'danger'
    settingsStatus.value = `设置保存失败：${humanError(err.message)}`
    return false
  } finally {
    isSavingSettings.value = false
  }
}

async function refreshFramingTest() {
  try {
    framingTest.value = await api('/framing-test/status')
  } catch {
    framingTest.value = { running: false, ready: false, preview_id: '' }
  }
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value))
}

async function startFramingTest() {
  if (framingTestBusy.value) return
  framingTestBusy.value = true
  framingConfirming.value = false
  framingTestStatusKind.value = 'muted'
  framingTestStatus.value = '机器人正在准备并录制测试画面，预计等待 10–15 秒…'
  try {
    framingTest.value = await api('/framing-test/start', { method: 'POST', body: '{}' })
    if (!framingSelectionLoaded.value) loadFramingSelectionFromSettings()
    if (framingMode.value === 'center') {
      framingCropX.value = 0.5
      framingCropY.value = 0.5
    }
    framingTestStatusKind.value = 'success'
    framingTestStatus.value = '测试画面已返回。选择自定义位置后，可拖动亮框调整保留区域。'
  } catch (err) {
    framingTest.value = { running: false, ready: false, preview_id: '' }
    framingTestStatusKind.value = 'danger'
    framingTestStatus.value = `取景测试失败：${humanError(err.message)}。没有保存任何测试素材。`
  } finally {
    framingTestBusy.value = false
  }
}

async function discardFramingTest() {
  if (framingTestBusy.value) return
  try {
    framingTest.value = await api('/framing-test/discard', { method: 'POST', body: '{}' })
    framingConfirming.value = false
    framingTestStatusKind.value = 'muted'
    framingTestStatus.value = ''
  } catch (err) {
    framingTestStatusKind.value = 'danger'
    framingTestStatus.value = `清除失败：${humanError(err.message)}`
  }
}

function loadFramingSelectionFromSettings() {
  const automation = settings.value?.automation
  if (automation?.framing_configured && automation.output_aspect_ratio) {
    framingAspectRatio.value = automation.output_aspect_ratio
    framingMode.value = automation.framing_mode === 'custom' ? 'custom' : 'center'
    framingCropX.value = Number(automation.framing_crop_x ?? 0.5)
    framingCropY.value = Number(automation.framing_crop_y ?? 0.5)
  } else {
    framingAspectRatio.value = null
    framingMode.value = 'center'
    framingCropX.value = 0.5
    framingCropY.value = 0.5
  }
  framingSelectionLoaded.value = true
  framingDraftDirty.value = false
  framingConfirming.value = false
}

function chooseCenterFraming(ratio) {
  framingAspectRatio.value = ratio
  framingCropX.value = 0.5
  framingCropY.value = 0.5
  framingMode.value = 'center'
  framingDraftDirty.value = true
  framingConfirming.value = false
}

function chooseCustomFraming(ratio) {
  if (!framingPreviewReady.value) return
  framingAspectRatio.value = ratio
  framingMode.value = 'custom'
  framingDraftDirty.value = true
  framingConfirming.value = false
}

function onFramingVideoMeta() {
  const video = framingVideoEl.value
  if (!video) return
  framingSourceWidth.value = video.videoWidth || 16
  framingSourceHeight.value = video.videoHeight || 9
  video.play().catch(() => {})
}

function beginFramingDrag(event) {
  const stage = framingStageEl.value
  if (!stage) return
  event.preventDefault()
  framingDragging.value = true
  framingConfirming.value = false
  framingMode.value = 'custom'
  framingDraftDirty.value = true
  framingDragStart = {
    x: event.clientX,
    y: event.clientY,
    cropX: framingCropX.value,
    cropY: framingCropY.value,
    rect: stage.getBoundingClientRect(),
    geometry: { ...framingGeometry.value }
  }
  window.addEventListener('pointermove', moveFramingDrag)
  window.addEventListener('pointerup', endFramingDrag, { once: true })
}

function moveFramingDrag(event) {
  const start = framingDragStart
  if (!start) return
  const movableX = start.rect.width * ((100 - start.geometry.width) / 100)
  const movableY = start.rect.height * ((100 - start.geometry.height) / 100)
  if (movableX > 0.5) {
    framingCropX.value = clamp(start.cropX + (event.clientX - start.x) / movableX, 0, 1)
  }
  if (movableY > 0.5) {
    framingCropY.value = clamp(start.cropY + (event.clientY - start.y) / movableY, 0, 1)
  }
}

function endFramingDrag() {
  framingDragging.value = false
  framingDragStart = null
  window.removeEventListener('pointermove', moveFramingDrag)
}

async function confirmFramingPreference() {
  if (!framingAspectRatio.value || framingSaving.value) return
  framingSaving.value = true
  try {
    settings.value = await api('/framing-test/confirm', {
      method: 'POST',
      body: JSON.stringify({
        aspect_ratio: framingAspectRatio.value,
        mode: framingMode.value,
        crop_x: framingCropX.value,
        crop_y: framingCropY.value
      })
    })
    framingTest.value = { running: false, ready: false, preview_id: '' }
    framingConfirming.value = false
    framingDraftDirty.value = false
    await refreshSettings()
    framingTestStatusKind.value = 'success'
    framingTestStatus.value = `取景偏好已保存：${savedFramingSummary.value}`
  } catch (err) {
    framingTestStatusKind.value = 'danger'
    framingTestStatus.value = `偏好保存失败：${humanError(err.message)}`
  } finally {
    framingSaving.value = false
  }
}

async function clearFramingPreference() {
  if (framingSaving.value) return
  framingSaving.value = true
  try {
    settings.value = await api('/framing-preference/clear', { method: 'POST', body: '{}' })
    framingAspectRatio.value = null
    framingMode.value = 'center'
    framingCropX.value = 0.5
    framingCropY.value = 0.5
    framingConfirming.value = false
    framingDraftDirty.value = false
    framingSelectionLoaded.value = true
    await refreshSettings()
    framingTestStatusKind.value = 'success'
    framingTestStatus.value = '已清除取景偏好；成片将保持原始画面。'
  } catch (err) {
    framingTestStatusKind.value = 'danger'
    framingTestStatus.value = `清除偏好失败：${humanError(err.message)}`
  } finally {
    framingSaving.value = false
  }
}

function goToFramingSetup() {
  showFramingSetupPrompt.value = false
  active.value = 'robot'
}

function continueWithoutFramingPreference() {
  showFramingSetupPrompt.value = false
  createAutomationJobs(true)
}

async function testLlm() {
  isTestingLlm.value = true
  settingsStatusKind.value = 'muted'
  settingsStatus.value = '正在测试大模型服务...'
  try {
    if (!await saveSettings()) return
    const result = await api('/settings/test/llm', settingsAdminOptions({ method: 'POST', body: '{}' }))
    settingsStatusKind.value = result.ok ? 'success' : 'danger'
    settingsStatus.value = result.ok ? `大模型正常：${humanError(result.message)}` : `大模型失败：${humanError(result.message)}`
  } catch (err) {
    handleSettingsAdminError(err)
    settingsStatusKind.value = 'danger'
    settingsStatus.value = `大模型失败：${humanError(err.message)}`
  } finally {
    isTestingLlm.value = false
  }
}

async function testTts() {
  isTestingTts.value = true
  settingsStatusKind.value = 'muted'
  settingsStatus.value = '正在测试语音时间戳...'
  try {
    if (!await saveSettings()) return
    const result = await api('/settings/test/tts', settingsAdminOptions({ method: 'POST', body: '{}' }))
    settingsStatusKind.value = result.ok && result.details?.has_words ? 'success' : 'danger'
    settingsStatus.value = result.ok ? `语音合成正常：${result.details?.word_count || 0} 个定时词` : `语音合成失败：${humanError(result.message)}`
  } catch (err) {
    handleSettingsAdminError(err)
    settingsStatusKind.value = 'danger'
    settingsStatus.value = `语音合成失败：${humanError(err.message)}`
  } finally {
    isTestingTts.value = false
  }
}

async function testSeedance() {
  isTestingSeedance.value = true
  settingsStatusKind.value = 'muted'
  settingsStatus.value = '正在测试特效服务连接（不会提交生成任务）...'
  try {
    if (!await saveSettings()) return
    const result = await api('/settings/test/seedance', settingsAdminOptions({ method: 'POST', body: '{}' }))
    settingsStatusKind.value = result.ok ? 'success' : 'danger'
    settingsStatus.value = result.ok
      ? `特效服务正常：${result.details?.model || 'Seedance'}（未产生生成费用）`
      : `特效服务失败：${humanError(result.message)}`
  } catch (err) {
    handleSettingsAdminError(err)
    settingsStatusKind.value = 'danger'
    settingsStatus.value = `特效服务失败：${humanError(err.message)}`
  } finally {
    isTestingSeedance.value = false
  }
}

async function generateVoiceover() {
  if (!canGenerateVoiceover.value || isGeneratingVoiceover.value) return
  isGeneratingVoiceover.value = true
  voiceoverStatusKind.value = 'muted'
  voiceoverStatus.value = '正在生成旁白...'
  try {
    const result = await api('/tts/generate', {
      method: 'POST',
      body: JSON.stringify({
        title: voiceoverTitle.value,
        text: voiceoverText.value,
        use_llm: voiceoverUseLlm.value,
        // Length targeting only applies through the LLM, so send it only then.
        target_seconds: voiceoverUseLlm.value && voiceoverSeconds.value > 0 ? voiceoverSeconds.value : null
      })
    })
    await Promise.all([refreshMedia(), refreshVault(), refreshTtsAssets()])
    // Tick the new voiceover into the pool, so generating one is enough to use it. Without
    // this you would generate a narration and silently render without it.
    const newId = result.media_item?.id
    if (newId && !selectedAutomationVoiceoverIds.value.includes(newId)
        && selectedAutomationVoiceoverIds.value.length < MAX_AUTOMATION_ITEMS) {
      selectedAutomationVoiceoverIds.value.push(newId)
    }
    voiceoverStatusKind.value = 'success'
    voiceoverStatus.value = `已创建 ${result.asset.name}，可在自动剪辑工作台的旁白池中选用。`
  } catch (err) {
    voiceoverStatusKind.value = 'danger'
    voiceoverStatus.value = `旁白生成失败：${humanError(err.message)}`
  } finally {
    isGeneratingVoiceover.value = false
  }
}

function moveCalendar(offset) {
  const next = addMonths(calendarCursor.value, offset)
  if (next < calendarStartDate || next > startOfMonth(calendarMaxDate)) return
  calendarCursor.value = next
  const currentSelection = parseDateKey(selectedCalendarDate.value)
  if (!isSameMonth(currentSelection, next) || currentSelection < calendarStartDate || currentSelection > calendarMaxDate) {
    selectedCalendarDate.value = formatDateKey(next < calendarStartDate ? calendarStartDate : next)
  }
}

function buildCalendarCells(monthDate) {
  const first = startOfMonth(monthDate)
  const daysInMonth = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate()
  const cells = []
  for (let index = 0; index < first.getDay(); index += 1) {
    cells.push({ key: `blank-${index}`, day: '', dateKey: '', inRange: false, isToday: false, assetCount: 0 })
  }
  for (let day = 1; day <= daysInMonth; day += 1) {
    const date = new Date(first.getFullYear(), first.getMonth(), day)
    const key = formatDateKey(date)
    const inRange = date >= calendarStartDate && date <= calendarMaxDate
    cells.push({
      key,
      day,
      dateKey: key,
      inRange,
      isToday: key === formatDateKey(todayDate),
      assetCount: calendarAssetMap.value.get(key)?.length || 0
    })
  }
  return cells
}

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1)
}

function addMonths(date, months) {
  return new Date(date.getFullYear(), date.getMonth() + months, 1)
}

function addYears(date, years) {
  return new Date(date.getFullYear() + years, date.getMonth(), date.getDate())
}

function endOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0)
}

function isSameMonth(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth()
}

function formatDateKey(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function parseDateKey(key) {
  const [year, month, day] = String(key).split('-').map(Number)
  return new Date(year, month - 1, day)
}

function formatBytes(bytes) {
  const value = Number(bytes || 0)
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unit]}`
}

function formatDate(value) {
  return new Date(value).toLocaleString('zh-CN')
}

function formatMs(value) {
  const seconds = Number(value || 0) / 1000
  if (!seconds) return '0.0 秒'
  return `${seconds.toFixed(seconds >= 10 ? 1 : 2)} 秒`
}

function seedanceAssetStatusLabel(status) {
  return {
    draft: '草稿',
    queued: '排队中',
    running: '生成中',
    succeeded: '已完成',
    failed: '失败'
  }[status] || status
}

function roleLabel(role) {
  return {
    raw_video: '导入',
    music: '音乐',
    tts_voice: '旁白',
    image: '图片',
    seedance_effect: '特效',
    export: '导出',
    preview: '预览',
    cache: '缓存',
    unknown: '其他'
  }[role] || role
}

function bucketLabel(label) {
  return {
    'Downloads / Raw Media': '导入媒体',
    Voiceovers: '旁白',
    'Seedance Effects': '特效',
    'Seedance Cache': '特效缓存',
    Exports: '导出',
    Previews: '预览',
    Cache: '缓存'
  }[label] || label
}

function mediaKindLabel(kind) {
  return {
    video: '视频',
    audio: '音频',
    image: '图片',
    unknown: '媒体'
  }[kind] || '媒体'
}

function jobStatusLabel(status) {
  return {
    queued: '排队中',
    running: '渲染中',
    succeeded: '已完成',
    failed: '失败',
    canceled: '已取消'
  }[status] || status
}

function jobMessageLabel(message) {
  return humanError(message)
}

function robotStatusLabel(status) {
  if (!status) return '等待中'
  return {
    connected: '已连接',
    connecting: '连接中',
    reconnecting: '重连中',
    disconnected: '未连接',
    error: '连接异常',
    going: '前往中',
    done: '已完成',
    failed: '失败',
    ok: '正常',
    idle: '空闲',
    running: '运行中',
    recording: '录制中',
    waiting: '等待中',
    localization: '定位模式',
    mapping: '扫图模式',
    preparing: '准备中'
  }[status] || status
}

function eventLabel(type) {
  return {
    CAPTURE_STARTED: '采集已开始',
    CAPTURE_STOPPED: '采集已停止',
    JOB_CREATED: '任务已创建',
    JOB_UPDATED: '任务已更新',
    ROBOT_STATE: '机器人状态已更新',
    ROBOT_PHOTO: '机器人照片已返回',
    ROBOT_MEDIA_SYNCED: '机器人媒体已同步',
    ROBOT_MEDIA_SYNC_FAILED: '机器人媒体同步失败',
    CAPTURE_MARKER: '已记录点位标记',
    CRUISE_STARTED: '巡游已开始',
    CRUISE_POINT_DISPATCHED: '已下发巡游点位',
    CRUISE_POINT_ARRIVED: '已到达巡游点位',
    CRUISE_POINT_DEPARTED: '已离开巡游点位',
    CRUISE_POINT_FAILED: '巡游点位失败',
    CRUISE_FINISHED: '巡游已结束',
    CRUISE_CANCELED: '巡游已取消',
    CRUISE_FAILED: '巡游失败'
  }[type] || type
}

function humanError(message) {
  const text = String(message || '未知错误')
  const exact = {
    Unauthorized: '未授权',
    Queued: '排队中',
    'Analyzing media': '正在分析媒体',
    'Planning timeline': '正在规划时间线',
    'Rendering export': '正在渲染导出',
    'Export complete': '导出完成',
    'Export failed': '导出失败',
    'LLM settings are incomplete': '大模型设置不完整',
    'LLM responded': '大模型已响应',
    'TTS settings are incomplete': '语音合成设置不完整',
    'TTS responded with timestamps': '语音合成已返回时间戳',
    'TTS responded without word timestamps': '语音合成已响应，但未返回词级时间戳',
    'A cruise is already running': '巡游已在进行中。',
    'A manual capture is running; stop it before starting a cruise': '手动录制进行中，请先停止再开始巡游。',
    'A cruise is running; it already controls recording': '巡游进行中，录制由巡游控制。',
    'A cruise is running; cancel it instead of stopping the recording': '巡游进行中，请用「取消巡游」而不是停止录制。',
    'A name is required': '请填写名称。',
    'That media item no longer exists': '媒体库里已经没有这个文件了。',
    'That file no longer exists on disk': '硬盘上找不到这个文件了。',
    'That name is too long': '名称太长了。',
    'That file is being used by a render right now': '这个文件正在被渲染使用，等渲染结束再改名。',
    'Route not found': '找不到该清单。',
    'A name cannot contain / \\ : * ? " < > |': '名称里不能有 / \\ : * ? " < > | 这些字符。',
    'Effect not found': '找不到该特效。',
    'Only imported clips can be removed from the library': '只有导入的素材可以从媒体库移出。',
    'Voiceover needs text': '请填写旁白文案或大模型提示词。',
    'Nothing usable was found in the text': '文案里没有可用于口播的内容，请补充后再试。',
    'dwell_max_seconds must be >= dwell_min_seconds': '停留最长秒数不能小于最短秒数。',
    'Robot websocket URL is not configured': '未配置机器人 WebSocket 地址',
    'Robot websocket was reconfigured': '机器人 WebSocket 地址已重新配置',
    'Only direct http(s) media URLs are supported': '仅支持直接 http(s) 媒体链接',
    'Only direct video, audio, or image URLs are supported': '仅支持直接视频、音频或图片链接',
    'No source videos selected': '未选择源视频素材',
    'Source videos must be imported video media': '源视频素材必须是导入的视频',
    'Music must be music audio media': '音乐必须是音乐音频',
    'Voiceover must be generated TTS media': '旁白必须是生成的语音合成素材',
    'Music pool must contain music audio media': '音乐池只能包含音乐音频',
    'Voiceover pool must contain generated TTS media': '旁白池只能包含生成的语音合成素材',
    'Seedance is disabled': '未启用 Seedance 特效服务',
    'Seedance API key is not configured': '未配置 Seedance API 密钥',
    'Seedance model is not configured': '未配置 Seedance 视频模型或接入点 ID',
    'Seedream image model is not configured': '未配置 Seedream 图片模型。',
    'Seedream returned no image url': 'Seedream 没有返回图片地址。',
    'Seedance TOS bucket is not configured': '未配置 Seedance 暂存用 TOS 桶名',
    'Seedance TOS region is not configured': '未配置 Seedance 暂存用 TOS 地域',
    'Seedance TOS endpoint is not configured': '未配置 Seedance 暂存用 TOS Endpoint',
    'Seedance TOS credentials are incomplete': '未完整配置 Seedance 暂存用 TOS 密钥',
    'Seedance source file does not exist': 'Seedance 源文件不存在',
    'Seedance source image must be an image': 'Seedance 源素材必须是图片',
    'Seedance source video must be a source video': 'Seedance 视频帧必须来自源视频素材',
    'Seedance task timed out': 'Seedance 任务等待超时',
    'Seedance succeeded without video_url': 'Seedance 已完成但没有返回视频地址',
    'Seedance task response did not include an id': 'Seedance 没有返回任务 ID',
    'Desktop bridge is unavailable. Start the app through Electron, not the browser URL.': '桌面桥接不可用。请从 Electron 应用打开，而不是直接访问浏览器地址。',
    'Desktop bridge is still starting. Restart the Electron app if this stays visible.': '桌面桥接仍在启动中。如果一直显示，请重启 Electron 应用。'
  }
  if (exact[text]) return exact[text]
  if (text.startsWith('Ark model ')) return text.replace(/Ark model '(.+?)' is not activated.*/, '模型「$1」未在该账号开通。请到火山方舟控制台开通该模型服务。')
  if (text.startsWith('Ark rejected the API key')) return '方舟拒绝了 API Key，请检查设置里的 Seedance API Key。'
  if (text.startsWith('Ark request failed')) return text.replace('Ark request failed', '方舟请求失败')
  if (text.startsWith('TOS bucket ')) return text.replace(/TOS bucket '(.+?)' does not exist in (.+?)\. .*/, '对象存储桶「$1」在 $2 不存在。请先创建，或在设置里改成正确的桶名。')
  if (text.startsWith('TOS rejected the credentials')) return 'TOS 拒绝了密钥，请检查 Access Key ID 和 Secret Access Key。'
  if (text.startsWith('TOS denied access')) return 'TOS 拒绝访问该桶，密钥可能没有写入权限。'
  if (text.startsWith('TOS upload failed')) return text.replace('TOS upload failed', '对象存储上传失败')
  if (text.startsWith('Seedance daily limit reached')) return text.replace(/Seedance daily limit reached \((\d+)\)/, 'Seedance 今日生成次数已用完（上限 $1）')
  if (text.startsWith('Seedance daily duration limit reached')) return text.replace(/Seedance daily duration limit reached \((\d+)s remaining, (\d+)s requested\)/, 'Seedance 今日视频时长额度不足（剩余 $1 秒，本次需要 $2 秒）')
  if (text.startsWith('LLM API error')) return text.replace('LLM API error', '大模型 API 错误')
  if (text.startsWith('TTS API error')) return text.replace('TTS API error', '语音合成 API 错误')
  if (text.startsWith('TTS error')) return text.replace('TTS error', '语音合成错误')
  if (text.startsWith('Robot command timed out waiting for')) return text.replace('Robot command timed out waiting for', '机器人指令等待超时：')
  return text
}

onMounted(async () => {
  try {
    if (!window.desktopApi) throw new Error('桌面桥接不可用。请从 Electron 应用打开，而不是直接访问浏览器地址。')
    apiConfig.value = await window.desktopApi.getApiConfig()
    recordingClock = setInterval(() => { nowTs.value = Date.now() }, 1000)
    connectWs()
    setTimeout(async () => {
      try {
        await refreshAll(); connected.value = true
      } catch (err) { log(`后端预热：${humanError(err.message)}`) }
    }, 800)
  } catch (err) {
    connected.value = false
    downloadStatusKind.value = 'danger'
    downloadStatus.value = humanError(err.message)
    log(humanError(err.message))
  }
})

onUnmounted(() => {
  // HMR and window teardown must not leave a reconnecting socket behind. Otherwise every UI
  // refresh adds another backend connection and every one keeps polling the same state.
  const socket = ws.value
  apiConfig.value = null
  ws.value = null
  socket?.close()
  window.removeEventListener('pointermove', resizeSidebar)
  window.removeEventListener('pointermove', moveFramingDrag)
  window.removeEventListener('pointerup', endFramingDrag)
  if (recordingClock) clearInterval(recordingClock)
  if (seedancePollTimer) clearInterval(seedancePollTimer)
  if (editingCapabilityTimer) clearTimeout(editingCapabilityTimer)
  if (settingsAdminExpiryTimer) clearTimeout(settingsAdminExpiryTimer)
})
</script>
