# Media Vault Design

The app treats media as managed assets, not as one flat list of files. This prevents rendered exports from becoming source clips in the next edit by accident.

## Managed Areas

- `data/downloads/`: downloaded raw videos, music, and images. Video files here are valid edit sources.
- `exports/`: rendered outputs. These are visible, openable, revealable, and deletable, but not edit sources by default.
- `previews/`: generated previews/proxies. These are cleanup candidates.
- `.cache/`: analysis and library cache. These are cleanup candidates.

## Asset Roles

- `raw_video`: selectable source clip.
- `music`: selectable soundtrack.
- `export`: rendered output, never automatically reused as source.
- `preview`: generated helper file.
- `cache`: disposable generated analysis/cache file.

## Safety Rules

- Edit Studio uses explicit source selection. It does not use every video in the workspace.
- Exports do not appear as source clips unless a future explicit `Use Export As Source` action copies or registers them intentionally.
- Delete/open/reveal actions are handled by Electron and restricted to managed app folders. Deletion moves files to the system trash.
- Storage warnings are based on a 20 GB managed-media threshold. The app recommends cleanup; it does not auto-delete user media.

## Production Workspace Direction

Development stores managed files under `/Users/user/Desktop/automated_video_editing`. Production should use a configurable workspace, with defaults like `~/Movies/Automated Video Editing` on macOS and `%USERPROFILE%\Videos\Automated Video Editing` on Windows.

## Calendar Range

The Media Vault calendar starts at the first day of the local current month and supports navigation through the same month ten years later. Months before the current month are blocked. Empty days are valid and show an empty detail panel.
