# Robot Adapter Contract

The app uses the robot/hardware-side websocket URL saved in Settings. The URL must be a
`ws://` or `wss://` endpoint exposed by the robot/hardware side, not a documentation or
website URL.

The active hardware contract is summarized in:

- `docs/live_robot_api_requirements_yuque.md`
- `docs/live_robot_api_requirements.md`

Required websocket protocol operations:

- `{ "get_map_list": "all" }`
- `{ "set_switch_map": "map1" }`
- `{ "get_path_list": "map_name" }`
- `{ "set_goal": { "path_name": "path1", "goal_id": 3, "goal_object": "car" } }`
- `{ "video_record": { "start": 0, "resolution": 4 } }`
- `{ "video_record": { "stop": 0 } }`
- `{ "take_photo": { "counter": 1, "gap": 0 } }`
- `{ "gimbal_control": { ... } }`

The backend normalizes robot heartbeat messages into `RobotState`, including map,
navigation, object alignment, gimbal, recording, battery, and returned media URL fields.
Manual movement is not part of the hardware protocol; robot movement/alignment goes
through map, path, goal id, and goal object commands.
