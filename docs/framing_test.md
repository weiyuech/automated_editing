# 镜头取景测试

`硬件与镜头` keeps export framing separate from ordinary capture. A test run:

1. Moves the gimbal to -45°.
2. Starts recording and sweeps slowly to +45° for 10 seconds.
3. Stops recording, restores the previous yaw, and downloads the returned HTTP(S) video into an
   OS temporary directory.
4. Shows the entire source. Selecting custom 16:9 or 9:16 adds a draggable crop; four
   translucent blurred panes mark the pixels that will be discarded.

The test clip is never registered with `MediaService` and never enters `data/downloads`,
`previews`, or `exports`. Only one temporary preview exists. It is removed on replacement,
failure, cancellation, confirmation, or backend shutdown.

## Saved preference

There is one preference slot containing:

- aspect ratio: `16:9` or `9:16`;
- mode: centred default or custom;
- normalized crop position `x/y` in the range 0–1.

A later save replaces that tuple. Every new edit job freezes the current tuple into its request
and timeline. FFmpeg scales to fill the target canvas, crops at the saved normalized position,
then burns subtitles over the final frame.

Centred 16:9 and 9:16 can be selected and saved without a test clip. A test clip is required only
for a custom position. Clearing the preference returns to an intentional unselected state: the
first source's native frame becomes the output canvas and differently shaped clips are fitted
inside it without cropping.

Until a preference is confirmed, creating an automatic edit shows a short setup prompt. The
operator can go to `硬件与镜头`, cancel, or continue with that original-frame behaviour.
