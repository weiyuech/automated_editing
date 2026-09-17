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

## 巡游自动运镜

以下说明对应当前源码。自动运镜默认关闭；开启后使用「镜头设置」里保存的一套配置。核心规划位于 [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py)，协议发送位于 [robot.py](backend/src/automated_video_editing_backend/services/robot.py)。

### 内部目标选择和锚点

界面不会向使用者解释区域编号；使用者只需要设置允许范围、锚点、速度，以及
回到锚点的节奏。以下区域划分只是实现与测试说明。

用户设置的 yaw 与 pitch 范围分别从中点切成两半，组合成四个区域：

| | yaw 较小的一半 | yaw 较大的一半 |
| --- | --- | --- |
| pitch 较小的一半 | 区域 1 | 区域 2 |
| pitch 较大的一半 | 区域 3 | 区域 4 |

- 第一个区域从四个区域中随机选择。
- 此后每个区域都从上一个区域以外的三个区域中等概率选择；锚点阶段不会清除这个记忆。因此 `1 → 4 → 1 → 4` 合法，`1 → 1` 不合法。
- 选定区域后，yaw 与 pitch 都在该区域的整数角度范围内随机选择。目标始终在用户保存的总范围内。
- 普通目标没有停留阶段：连续两次新心跳进入目标 ±2°，或完整预计行程时间结束后，
  立即选择下一个目标。
- 锚点是唯一带有明确停留时间的目标，不是「随机停在某个角度」。

已退役的方向策略只保存在 [docs/archive/camerawork-directional-v1.md](docs/archive/camerawork-directional-v1.md)，不会参与运行。

### 锚点时间怎样计算

界面只提供两个节奏参数：

- `anchor_time_percent`：回到锚点并停留的计划时间占比，默认 20%。
- `anchor_dwell_seconds`：每次确认回到锚点后停留的基准时长，默认 5 秒。

每个周期先在基准时长的 ±30% 内生成实际锚点时长 `A`，再按时间比例计算四区域时长：

```text
Q = A × (100 - anchor_time_percent) / anchor_time_percent
```

一个周期先执行 `Q 秒普通自动运镜`，再发送回锚点指令。只有连续两次新心跳进入
锚点 ±2°，或完整预计返回时间结束后，才开始完整的 `A 秒锚点停留`。返回路程不占用
这段停留时间。普通运镜预算如果在一次移动途中结束，会先完成该次移动，再回锚点；
不会截断移动，也不会为了追赶旧时间线而缩短或跳过锚点停留。

`anchor_time_percent` 划分的是计划中的普通运镜和锚点停留时间；回锚点的路程属于转换
开销，因此真实墙钟占比会有轻微差异。完整动作和完整停留优先于机械追求百分比。
每个锚点停留结束后才生成下一周期。内部的 ±30% 变化不作为额外界面设置。

- 设为 0%：只做四区域运镜。
- 设为 100%：开始前回到锚点并连续续接完整停留；不会反复发送无意义的回锚指令。
- 其他数值：按计划时间分配，不是按指令条数抽签。

开始前和录制停止后的安全回锚点不属于这个随机周期，也不会污染成片中的锚点时间占比。

### 行进、到点和变焦

同一套时间状态跨越底盘行进与到点停留，不会在每个点额外强塞一段锚点画面：

- 普通自动运镜：底盘行进或停稳时都可以缓慢改变 yaw/pitch；到达一个目标后立即继续，
  不增加额外停顿。
- 回锚点、底盘行进时：只回到并保持锚点 yaw/pitch，zoom 保持当前值。
- 已到锚点、底盘停稳时：完整锚点停留已经开始，此时才允许变焦，并在离开前恢复
  完整锚点。

因此「锚点停留时间」包含停稳时的锚点变焦时间，而不是另设一个静止概率。变焦没有
机器人心跳回传；应用只能记录最近下发的倍率。已经退役的到点扫视不会参与运行。

机器人到达路线点位后仍会保留一小段内部取景窗口，默认以 7.5 秒为中心做 ±30% 变化；
这是底盘停稳的拍摄窗口，不是镜头目标停顿，也不作为用户设置。不开录像的「试跑」会
完全跳过这段窗口。

巡游到点始终只依据 `goal_status`。应用不会等待 `object_status`，并会在下发前移除旧清单里的 `goal_object`；目标物识别或对准失败不能阻止拍摄。这是高于旧协议数据的产品规则。

### 如何核对应用与机器人

在正式应用打开「镜头设置 → 镜头控制 → 拍摄诊断」：

| 项目 | 应用下发 | 机器人心跳 |
| --- | --- | --- |
| 水平 yaw | 最近一条指令的起点与目标 | 实测角度 |
| 俯仰 pitch | 最近一条指令的起点与目标 | 实测角度 |
| 变焦 zoom | 应用内部记录 | 机器人不回传 |

「已下发」只证明 WebSocket 写入成功，不证明机器人已经执行。完整记录在运行数据目录的 `logs/diagnostics.log`，可搜索 `robot.command.sent` 和 `gimbal_control`。断开连接或心跳过旧时，界面不会把应用保存的目标伪装成实测值。

### 独立 HTML 调试页

[tools/robot-control-console.html](tools/robot-control-console.html) 直接连接机器人 WebSocket，
不经过 Python 后端。它保留手动 yaw/pitch 控制、心跳实测和目标偏差，并用与正式应用
相同的自动运镜与锚点计时逻辑模拟巡游。

- 调试页只验证 yaw/pitch；界面不展示变焦，发送包固定为 1×。
- 配置使用本地存储 v2；旧 v1 的锚点、角度范围和速度会安全迁移，并补上默认 20% / 5 秒。已退役字段不会迁入新规划器。
- 「模拟到达点位」只把底盘状态从行进切为停稳；它不重置时间阶段、区域历史或自动运镜，也不会额外发送一个目标。「停止并回锚点」才会结束模拟并回锚点。
- 修改 HTML 不会自动修改正式应用；两份实现必须一起维护。

### 主要修改位置和测试

| 内容 | 位置 |
| --- | --- |
| 配置模型、默认值与范围 | [models.py](backend/src/automated_video_editing_backend/core/models.py) 的 `CameraworkConfig` |
| 内部目标选择、锚点计时、停稳变焦 | [cruise.py](backend/src/automated_video_editing_backend/services/cruise.py) |
| 自动运镜设置与拍摄诊断 | [App.vue](frontend/src/renderer/src/App.vue) |
| 机器人命令、心跳和最近下发快照 | [robot.py](backend/src/automated_video_editing_backend/services/robot.py) |
| 独立现场调试页 | [tools/robot-control-console.html](tools/robot-control-console.html) |

修改后至少运行：

```bash
.venv/bin/python -m pytest backend/tests/test_cruise_camerawork.py backend/tests/test_robot.py
node --test tools/tests/robot-control-console.test.mjs
```

更完整的巡游生命周期、故障策略和接口说明见 [docs/cruise_capture.md](docs/cruise_capture.md)。
