# Automated Video Editing

A desktop operator console for robot-assisted filming and automated video editing.

This project is intentionally isolated from other checkouts. All source, scripts, caches, logs, previews, media, and exports live under this folder.

## Architecture

- `frontend/`: Electron + Vue desktop UI with a vertical operator-console layout.
- `backend/`: Python FastAPI backend for robot control, capture sessions, media analysis, edit planning, and rendering.
- `scripts/`: root-safe scripts. Scripts refuse to write outside this project root.
- `data/`, `.cache/`, `logs/`, `exports/`, `previews/`: generated/runtime folders.

## Why Electron + Python

Electron gives the app a polished local desktop shell: native file pickers, video preview UI, process management, packaged installs, and future hardware control panels. Python owns the heavy lifting: robot adapters, video analysis, timeline planning, and FFmpeg rendering.

## Development

Prerequisites: Python 3.12, Node.js 22.13 or newer, and Corepack (included with the supported Node.js release).

From the repository root, prepare the Python environment and start the desktop app:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e 'backend[dev,beat]'

cd frontend
corepack pnpm install --frozen-lockfile
corepack pnpm run dev
```

Do not use a path under `~/.cache/codex-runtimes/`; it is an internal, temporary Codex path and can change. The `packageManager` field in `frontend/package.json` makes Corepack use the project's pinned pnpm version.

Electron starts and stops the Python backend automatically. For backend-only API diagnostics, run this separately from the repository root (not at the same time as the Electron app):

```bash
.venv/bin/python scripts/run_backend.py
```

The Electron main process generates a random bridge token and starts the Python backend with that token. The token is not shown in the UI or logs.

## Basic Workflow

1. Open the app with `corepack pnpm run dev` from `frontend/`. Electron starts the Python backend automatically.
2. In Media Library, import local videos/music or paste a direct media URL, such as an `.mp4`, `.mov`, `.webm`, `.mp3`, or `.wav` URL. Streaming pages such as YouTube/TikTok/Bilibili need a later `yt-dlp` connector.
3. In Edit Studio, choose a music file, keep `Mute source video noise` enabled, keep `Cut to music beats` enabled, and create the edit job.
4. Render Queue tracks progress. Finished MP4 exports are written under `exports/` in the repository root.

The Ping button is only a local connection check: it sends a WebSocket `PING` and expects `PONG`. It does not start robot motion, capture, or rendering.

## Stable Editing Stack

Beat detection is installed through the `backend[beat]` extra. It pins `librosa`, `numba`, and `llvmlite` to versions with prebuilt Python 3.12 macOS x86_64 wheels, avoiding local LLVM source builds. The backend sets `NUMBA_CACHE_DIR` to `.cache/numba` so compiled beat-analysis functions stay inside the app folder.


- FFmpeg: trimming, transitions, audio mixing, encoding, export.
- PySceneDetect: scene boundary detection.
- librosa: music beat and onset detection.
- OpenCV: optional frame-level inspection and thumbnail/proxy helpers.

The initial app is designed to work with mock robot control first. Real hardware can be added by implementing the `RobotAdapter` interface.

## Media Vault

Media is organized by role instead of treated as one flat pile. Downloads are raw source media or music, exports are rendered results, and previews/cache are cleanup candidates. Edit Studio only uses explicitly selected raw clips, so exported MP4s do not get accidentally folded back into later renders. See `docs/media_vault.md` for the design rules.

## Settings, LLM, And Timed TTS

Provider credentials are owned by the backend `SettingsService` and stored locally in `data/settings.local.json`. API routes return masked status only, and blank secret fields in the UI keep the existing local value. Provider changes apply immediately; restart is only needed after code changes.

The timed voiceover path uses Volcengine's fast sync TTS endpoint with `with_timestamp=1`. It writes the generated audio and matching word-timing JSON under `data/tts/`, then registers the audio as a voiceover asset. Render jobs keep background music and voiceover separate so the voice can sit above lowered music instead of replacing it.

## 机器人运镜：查看下发、理解逻辑与修改位置

以下说明依据 2026-09-16 的工作区源码，供现场检查和调整使用。旧安装包不一定已经包含这些改动；源码逻辑与机器人实际执行结果需要分别查看。

### 先看整体关系

自动运镜的核心文件是 [backend/src/automated_video_editing_backend/services/cruise.py](backend/src/automated_video_editing_backend/services/cruise.py)。它负责选动作、计算目标、安排变焦和等待；[robot.py](backend/src/automated_video_editing_backend/services/robot.py) 负责把指令转换成机器人协议并通过 WebSocket 发送。

| 概念 | 决定什么 |
| --- | --- |
| 一条云台指令 | 从哪些 yaw / pitch / zoom 起点移动到哪些终点，以及水平、俯仰速度 |
| 50 / 30 / 20 | 本轮做单次运镜、左右往返，还是回锚点 |
| 80 / 20 | 进入自适应单次运镜后，水平大幅反向还是小幅继续向外；受 10° 空间门槛约束 |
| 四个角度分区 | 随机目标只需换到不同分区，跨过中线即可；不额外限制最小移动距离 |
| 变焦 zoom | 自动运镜开启时，由巡游到点后的流程单独安排，行进中的 50/30/20 保持当前倍率 |

```text
开始巡游 → 回锚点 → 开始录制并导航
                         ↓
                  行进中抽 50/30/20
                         ↓
                     选目标、检查分区
                         ↓
                发一条云台指令 → 等待
                         ↓
          继续本轮下一段，或重新抽下一种动作

底盘报告到点 → 结束行进运镜 → 回锚点 → 变焦 → 恢复锚点倍率
```

等待可以由新心跳确认到位而结束，也可以因预计时间用完而结束；发送成功、等待结束、物理到位是不同状态。随机抽选按每个动作块进行，不保证每十次正好是五次、三次、两次。

### 在哪里看应用下发和机器人实测

正式应用中打开 **镜头设置 → 镜头控制 → 底部「拍摄诊断」**。非巡游和巡游共用这一处；巡游开始后也可以切换到这里查看。

| 项目 | 应用下发 | 机器人心跳 |
| --- | --- | --- |
| 水平 yaw | 起点 → 目标角度 | 实测角度 |
| 俯仰 pitch | 起点 → 目标角度 | 实测角度 |
| 变焦 zoom | 起点 → 目标倍率 | 不回传 |

- 「应用下发」在机器人 WebSocket 写入成功后才更新，证明指令已发送，不证明机器人已经到位。
- 「机器人心跳」来自独立收到的反馈。断开连接，或该轴超过 5 秒没有更新时，界面显示「未实时回报」。
- 起点与终点相同时只显示一个值。这张卡片保留最近一条云台指令，后续指令会覆盖它，不是完整历史。
- 手动控制、取景测试、巡游行进、回锚点、停稳后的变焦和到点扫描，都使用这份最近指令快照。
- 完整发送历史和 JSON 可在运行数据目录的 `logs/diagnostics.log` 中搜索 `robot.command.sent`、`gimbal_control`。开发运行与安装版的数据目录可能不同。

界面位于 [App.vue](frontend/src/renderer/src/App.vue) 的「拍摄诊断」区，数据处理入口是 `lastGimbalCommand`、`appTargetLabel()`、`heartbeatAxisLabel()`。发送快照在 [robot.py](backend/src/automated_video_editing_backend/services/robot.py) 的 `_send()` 中记录为 `diagnostics.last_gimbal_command`；实测数据为 `diagnostics.last_heartbeat`。

不要把通用的 `state.yaw` / `state.pitch` 当成纯实测，因为发送函数也会把它们写成目标值。核对实际位置时，应使用心跳快照或 `heartbeat_yaw()` / `heartbeat_pitch()`。

### 非巡游与巡游分别采用什么逻辑

| 场景 | 当前行为 | 是否使用「大幅反向 / 小幅同向」 |
| --- | --- | --- |
| 手动「发送镜头控制」 | 按表单中的 yaw / pitch 起点、终点、速度，以及 zoom 起点、终点发送 | 否 |
| 原地采集 | 开始录制，本身不启动随机运镜 | 否 |
| 取景测试 | 执行预设测试扫动，之后恢复原来的水平角 | 否 |
| 巡游开启「自动运镜」 | 开始回锚点，行进中选择动作，到点后回锚点及变焦 | 是，用于自适应单次运镜 |
| 巡游关闭自动运镜、开启到点扫描 | 到点后按指定方向、偏移、速度扫动，再返回起始水平角 | 否，使用固定扫描规则 |
| 巡游两个运镜选项都关闭 | 这两个运镜流程都不启动 | 否 |

巡游或取景测试运行时，手动镜头控制被禁用，后端也会拒绝竞争控制。相关检查在 [api/ws.py](backend/src/automated_video_editing_backend/api/ws.py) 的 `camera_commands` 分支中。

### 「偏左向右大幅、向左小幅」如何实现

这条建议已经实现在 [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 的 `_adaptive_yaw_target()` 中。

本协议规定 **yaw 正数表示左、负数表示右；pitch 负数表示上、正数表示下**。左右判断依据协议的 0°，不是锚点，也不是配置范围的中点；函数会先把当前角度限制到允许范围内。

两侧剩余空间都大于 10° 时，自适应单次运镜这样选择：

| 当前水平角 | 80% 的选择 | 20% 的选择 |
| --- | --- | --- |
| 正数，偏左 | 向右运动该方向可用距离的 60%～90% | 继续向左运动剩余距离的 10%～30% |
| 负数，偏右 | 向左运动该方向可用距离的 60%～90% | 继续向右运动剩余距离的 10%～30% |

例如允许范围为 **−60°～+60°**，当前角度为 **+30°，偏左**：向右可走 90°，大幅目标约为 **−24°～−51°**；继续向左可走 30°，小幅目标约为 **+33°～+39°**。

目标角度取整数并限制在配置范围内。代码设置至少 1° 的候选位移，但边界会限制最终位移。方向选择的空间门槛从 0.1° 改为 **10°**：继续向外的空间小于或等于 10°，且反方向空间大于 10° 时，直接选择大幅反向，不再抽签。例如范围 ±60°、当前 +52°，继续向左只剩 8°，会直接选择向右。

当前为 0° 时也使用同一门槛：右侧大于 10° 且左侧小于或等于 10° 时直接向右；两侧都大于 10° 时等概率选择，并运动该侧可用距离的 40%～80%。**10° 只决定方向分支，不是固定步长，也不是目标必须避开的边界区。** 判断结构保持不变：如果两侧空间都不大于 10°，会走原有的外摆分支（居中时为向左分支），目标仍限制在原范围内。不对称或单侧范围不保证每次跨过 0°。

**80/20 是进入自适应规则后的分支概率，不是全部巡游动作的比例。** 行进中的上一层选择是：

1. **50% 自适应单次运镜**：调用上述左右规则；俯仰目标在设置范围内随机取整数，发送前再检查下面的换区约束。
2. **30% 左右往返**：当前偏左就先向右，当前偏右就先向左；端点由 `_pingpong_poses()` 另行生成，不套用上述 60%～90% 的公式。
3. **20% 回锚点**：回锚点一次后继续选择动作。如果已经在锚点附近，就改做自适应单次运镜，不原地空等。

每段速度从保存的 `speed_min`～`speed_max` 中随机取整数，范围限制为 2～5°/秒。该段水平和俯仰使用同一个速度；**大幅运动不会因此加速**，行进中变焦保持不变。

巡游开始会先回保存的锚点，再开始录制和导航；随后每段重新判断当前角度。因此锚点为 0° 时，第一段通常从居中分支开始，不会一直沿用启动前的偏左或偏右角度。

每段优先读取最近收到的心跳角度；没有该轴心跳值时，使用内部保存的上一目标。动作等待会核对新的完整 yaw/pitch 心跳，误差容限为 2°，连续两次达标；没有反馈时按距离 / 速度加余量等待。规划器读取最近心跳值的函数本身没有套用界面的 5 秒新鲜度判断，不能把它描述成始终取得实时实测。

到点后取消行进运镜、回锚点，再在锚点姿态上变焦并返回完整锚点。如果心跳明确显示回锚点失败，不继续添加变焦。**巡游到点只依据 `goal_status`，不等待 `object_status`；巡游下发会去掉 `goal_object`，旧路线也不能恢复目标物对准等待。** 这是产品优先规则。更多巡游流程说明见 [docs/cruise_capture.md](docs/cruise_capture.md)。

### 什么时候选择下一目标，以及四象限约束

**象限的英文是 quadrant，四象限是 four quadrants。** 这里的坐标是云台的 yaw × pitch，不是底盘在地图上的位置。

yaw 与 pitch 两个角度描述一个观看方向，不包含物体距离：同一方向上，物体可能在 2 米外，也可能在 20 米外。代码划分的是用户允许的**二维角度范围**，不是把真实三维场景切成四块。两条中线划出四个区域，每个区域都包含很多候选角度。

| | yaw 小于中线 | yaw 大于等于中线 |
| --- | --- | --- |
| pitch 大于等于中线 | 区域 A | 区域 B |
| pitch 小于中线 | 区域 C | 区域 D |

换角度分区不代表一定拍到另一个物体。判断真实场景或物体覆盖，需要额外的位置、深度或画面内容信息。

50% 表示这一轮抽中了「单次运镜」，不是到点后固定执行的动作。它先选目标、发送一条云台指令，再等待到位反馈或预计时间结束，然后重新抽取 50/30/20 中的下一种动作。30% 的左右往返会顺序发送两段，每段都有等待；底盘到达巡游点时则停止行进运镜，转入回锚点流程。超时后仍可能继续下一条，不能把顺序等待理解为每条都已确认物理到位。

为让连续随机目标进入不同角度分区，发送前检查以下约束：

- **按用户范围中点划分四个区域**：分界线为 `(yaw_min + yaw_max) / 2` 和 `(pitch_min + pitch_max) / 2`。例如 yaw 范围 −60°～+60°、pitch 范围 −20°～+10°，分界线是 yaw=0°、pitch=−5°，不是统一使用协议 0°。恰好在中线时归入数值较大的一半。
- **下一目标必须属于不同区域**，但不要求每次都去对角区域，也不要求依次走完四个区域。
- **只要水平或俯仰跨到中线另一侧就算换区**，不再额外要求移动范围的 25%。中线附近的小幅跨区也接受；原有水平大幅/小幅选点规则仍然适用。
- **保留原先选出的水平目标**，在范围内选择满足约束的整数俯仰目标。因此 80/20 的水平方向选择、10° 门槛及水平幅度规则继续有效；俯仰的选择会受新约束影响。
- 约束用于 50% 单次运镜、30% 左右往返的每一段，以及已经在锚点附近时改做的单次运镜。每段发送前使用最近的位置重新检查，包括往返第二段。真正的回锚点动作直接使用保存的锚点，不套用这个约束。

例如当前为 `(yaw=+30°, pitch=+5°)`，原规则选择了小幅向左的 `yaw=+36°`，则可搭配 `pitch=−6°` 跨过 −5° 的俯仰中线。若当前在俯仰 −4°，目标 −6° 也算换区，即使只相差 2°。**所有目标仍在用户选择的角度范围内**。约束依据本次采用的起始位置计算；最近心跳及无心跳时的上一目标回退机制保持不变。

当前实现**不是先从其他三个象限中等概率抽一个，再在里面随机选点**。它先沿用水平选点规则，并在范围内随机取整数 pitch：如果水平已经跨中线，任意范围内的 pitch 都可以；如果水平没有跨中线，pitch 就必须落在另一半，原 pitch 不合格时从合格整数角度中随机重选。因此三个可去分区不保证各占三分之一。

### 单次运镜结束后会不会停顿

50% 单次运镜完成等待后，会直接进入下一轮动作选择，没有另外添加静止停留。30% 往返的两段之间、20% 行进中回锚点完成后，也没有额外的静止停留。

但当前仍是逐条指令衔接：每段等待新的 yaw/pitch 心跳连续两次进入 2° 容差范围，或等到约「最大轴角度变化 / 速度 + 0.6 秒」的预算结束；检查间隔为 0.2 秒。反馈确认、通信和设备执行可能造成短暂间隔，应用没有做跨指令的连续速度或加速度规划，因此不能仅凭源码保证真机完全无停顿。底盘到点后的停留、变焦等待及锚点保持属于另一阶段。

### 变焦由谁控制，在哪个文件

下面三个函数**全部在同一个文件**：[backend/src/automated_video_editing_backend/services/cruise.py](backend/src/automated_video_editing_backend/services/cruise.py)。行号是本次整理时的位置，后续代码增减时可直接搜索函数名。

| 函数 | 当前行号 | 作用 |
| --- | --- | --- |
| `_parked_zoom_target()` | 777 | 在用户的变焦范围内选择目标倍率 |
| `_parked_zoom_and_anchor()` | 784 | 回锚点、发送变焦、等待，再恢复锚点倍率 |
| `_return_to_anchor()` | 817 | 恢复保存的水平、俯仰及变焦锚点 |

**50% 单次、30% 往返、20% 行进中回锚点都不随机改变倍率。** 它们经过 `_camerawork_leg()`，其中起始和目标 zoom 相同：

```python
zoom_start=self._cw_zoom,
zoom_end=self._cw_zoom,
```

真正的自动变焦由到点停留流程 `_dwell()` 调用 `_parked_zoom_and_anchor()`，顺序是：

1. 回到保存的完整锚点。如果取消，或回锚点流程未通过，则跳过这次变焦。
2. `_parked_zoom_target()` 根据 `zoom_min`、`zoom_max` 和 `anchor_zoom` 选择倍率。
3. 保持 yaw、pitch 为锚点角度，发送新的 zoom 目标。
4. 等待 `_CW_ZOOM_SETTLE_SECONDS`，当前为 1 秒，然后恢复完整锚点。

倍率选择代码是：

```python
midpoint = (config.zoom_min + config.zoom_max) / 2.0
if config.anchor_zoom <= midpoint:
    return round(random.uniform(midpoint, config.zoom_max), 2)
return round(random.uniform(config.zoom_min, midpoint), 2)
```

含义是：锚点倍率在较小的一半，就从较大的一半选目标；锚点倍率在较大的一半，就从较小的一半选目标。倍率保留两位小数，仍在用户范围内。

例如范围 **1×～1.5×**、锚点 **1×**：中点为 1.25×，目标从 1.25×～1.5× 随机选，一次指令流程可能是 **1× → 1.4× → 1×**。

机器人心跳不回传 zoom，`_cw_zoom` 记录的是应用最近下发的倍率，1 秒等待也不是实际变焦到位证明。底层协议中的 `zoom_speed` 固定为 0，应用没有单独提供变焦速度调节。手动镜头控制可直接设置 zoom 起点和终点；独立 HTML 调试页则固定发送 1×，不演示这套到点变焦流程。

### 如何按代码块阅读

下面这些函数都在 [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 的 `CruiseService` 类中，可在编辑器按函数名搜索：

| 阅读顺序 | 函数 | 先理解的问题 |
| --- | --- | --- |
| 1 | `_run_camerawork()` | 一轮怎样抽动作、怎样安排一个或两个目标 |
| 2 | `_adaptive_yaw_target()` | 当前偏左或偏右时，怎样计算水平目标 |
| 3 | `_separate_camerawork_target()` | 初选目标是否换区；不合格时怎样重选俯仰 |
| 4 | `_camerawork_leg()` | 怎样把角度和速度装进一条指令，并在发送后等待 |
| 5 | `_await_camerawork_pose()` | 怎样区分新的心跳、连续到位反馈和等待超时 |
| 6 | `_parked_zoom_target()`、`_parked_zoom_and_anchor()` | 到点后怎样选择并发送倍率，再恢复锚点 |

新增的 `_separate_camerawork_target()` 可以分成三块阅读：

1. **整理输入和中线**：`target[0]` 是水平角，`target[1]` 是俯仰角；`_clamp()` 把数值限制在用户范围内，`yaw_mid` / `pitch_mid` 计算两条中线。
2. **判断是否换区**：`(yaw >= yaw_mid) != (current_yaw >= yaw_mid)` 比较新旧水平是否在中线不同侧。`!=` 表示两边答案不同；水平或俯仰任一轴换侧即可换区。
3. **替换不合格的俯仰**：如果原 pitch 没能让目标换区，就遍历范围内的整数角度，留下 `acceptable(candidate)` 为真的候选，再用 `random.choice(choices)` 随机选一个。最终返回 `(yaw, pitch)`，保留原先选出的水平目标。不再检查额外的最小距离。

沿用 yaw −60°～+60°、pitch −20°～+10° 的范围：从 `(30, 5)` 到 `(36, 6)` 没换区，不合格；改成 `(36, −6)` 后，俯仰跨过 −5° 中线，因此合格。这里的 `or` 表示水平或俯仰任一轴换侧即可。

### 从界面操作到机器人发送

手动镜头控制的调用路径：

```text
App.vue: sendGimbal()
  → ROBOT_GIMBAL
  → api/ws.py
  → RobotService.set_gimbal()
  → 适配器 set_gimbal()
  → _send()
  → 机器人 WebSocket
```

巡游自动运镜的调用路径：

```text
App.vue: buildCruiseRequest()
  → /api/cruise/start
  → CruiseService.start()
  → _run_segment()
  → _run_camerawork()
  → 随机目标先经 _separate_camerawork_target() 检查
  → _camerawork_leg()
  → 同一个 RobotService.set_gimbal()
  → 适配器 set_gimbal() → _send() → 机器人 WebSocket
```

保存自动运镜设置只保存配置，不发送云台动作。配置保存在 `settings.automation.camerawork`，巡游开始时读取；运行中修改配置，下一次巡游才使用新值。

### 按修改目的查找文件

| 想修改什么 | 文件与入口 |
| --- | --- |
| 用户可调的锚点、允许范围、速度 | 先在应用「镜头设置 → 自动运镜（巡游）」修改并保存，无须改代码 |
| 提前转向的空间门槛（当前 10°） | [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 顶部的 `_CW_DIRECTION_ROOM_DEG` |
| 按用户范围中点换区 | 同文件 `_separate_camerawork_target()`；由 `_run_camerawork()` 在每段随机目标发送前调用，无额外最小距离门槛 |
| 50/30/20、80/20、大幅/小幅距离比例 | [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 顶部的 `_CAMERAWORK_MODES`、`_CW_OPPOSITE_PROB`、`_CW_OPPOSITE_DISTANCE`、`_CW_OUTWARD_DISTANCE`、`_CW_CENTER_DISTANCE` |
| 偏左、偏右、居中时如何选目标 | 同文件 `_adaptive_yaw_target()` |
| 往返顺序和端点 | 同文件 `_pingpong_poses()` |
| 如何循环选动作、每段速度 | 同文件 `_run_camerawork()` |
| 巡游每段实际发送哪些参数 | 同文件 `_camerawork_leg()` |
| 选择变焦倍率 | [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 的 `_parked_zoom_target()`；范围和锚点来自用户保存配置 |
| 到点后执行变焦、回锚点 | [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) 的 `_parked_zoom_and_anchor()`、`_return_to_anchor()`；由 `_dwell()` 调用 |
| 到点变焦后的等待时间 | 同文件 `_CW_ZOOM_SETTLE_SECONDS`，当前为 1 秒 |
| 到点固定扫描 | 同文件 `_scan()` |
| 手动表单、自动运镜设置、诊断对照界面 | [App.vue](frontend/src/renderer/src/App.vue) 中的「镜头控制」「自动运镜（巡游）」「拍摄诊断」 |
| 手动发送入口 | 同文件 `sendGimbal()` → [api/ws.py](backend/src/automated_video_editing_backend/api/ws.py) 的 `ROBOT_GIMBAL` 分支 |
| 自动运镜开关怎样进入请求 | `App.vue` 的 `buildCruiseRequest()` → [api/routes.py](backend/src/automated_video_editing_backend/api/routes.py) 的 `cruise_start()` |
| 保存自动运镜配置 | `App.vue` 的 `saveCameraworkPreference()` → `api/routes.py` 的 `camerawork_preference_save()` |
| 共用云台 JSON 字段、固定 `mode=1` | [robot.py](backend/src/automated_video_editing_backend/services/robot.py) 的适配器 `set_gimbal()`；单角度设置和扫动另有 `set_camera_angle()`、`sweep_camera()` 构造指令 |
| 最终网络发送、记录最近指令 | 同文件 `_send()` |
| 心跳角度解析 | 同文件 `_handle_message()` 中的 `gimbal` 解析 |
| 默认值、允许范围和后端校验 | [models.py](backend/src/automated_video_editing_backend/core/models.py) 的 `GimbalMoveRequest`、`CameraworkProfile`；同时对应 `App.vue` 输入框与 `cameraworkWarning` 校验 |
| 固定取景测试动作 | [framing_test.py](backend/src/automated_video_editing_backend/services/framing_test.py) 的 `_run_test()` |

修改 `models.py` 的默认值不会自动覆盖用户已经保存的设置。若要调整已保存的锚点或范围，应在应用中重新保存。

### HTML 调试页是独立实现

[tools/robot-control-console.html](tools/robot-control-console.html) 直接连接机器人 WebSocket，不经过正式应用的 Python 后端。

- 右侧「心跳实测位置」「发送目标与实测偏差」用于查看；展开「通信记录」可以查看发送记录。
- `MODE_WEIGHTS`、`OPPOSITE_PROBABILITY`、`OPPOSITE_DISTANCE`、`OUTWARD_DISTANCE`、`CENTER_DISTANCE` 控制权重和幅度。
- `DIRECTION_ROOM_DEG = 10` 是方向选择的空间门槛，与正式应用保持一致。
- `separateCameraworkTarget()` 检查目标是否跨过范围中线进入不同分区；`moveAutoTarget()` 在发送随机目标前调用，固定锚点除外，没有额外最小距离门槛。
- `adaptiveYawTarget()` 实现左右规则，`runAutomatic()` 循环选择动作。
- `sendGimbal()` 构造指令，`sendObject()` 实际发送。
- 自动页模拟行进/到达阶段，不等于实际底盘导航。页面隐藏变焦，但实际发送包仍固定 `zoom_start=1`、`zoom_end=1`；可见通信记录省略了 zoom 字段。

**修改 HTML 不会修改正式应用的 Python 运镜逻辑。** 如果现场用 HTML 验证方案，并要求正式应用采用同样行为，需要同步修改两份实现中的对应规则。

### 修改后应查看的现有测试

- [backend/tests/test_cruise_camerawork.py](backend/tests/test_cruise_camerawork.py)：左右大幅/小幅、模式比例、不对称范围、往返顺序、回锚点与反馈等待。
- [backend/tests/test_robot.py](backend/tests/test_robot.py)：机器人协议发送和反馈处理。
- [frontend/tests/robot-diagnostics.test.mjs](frontend/tests/robot-diagnostics.test.mjs)：诊断数据与心跳判断。
- [tools/tests/robot-control-console.test.mjs](tools/tests/robot-control-console.test.mjs)：独立 HTML 调试工具。
