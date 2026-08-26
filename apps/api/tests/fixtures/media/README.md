# Media validation fixtures

These fixtures contain only synthetic blue frames generated with FFmpeg's `color` source.
They cover the Issue #13 contract without external media or user data:

- `valid-720p-h264.mp4`: MP4, H.264, yuv420p, 1280x720, 5 seconds.
- `wrong-container.mkv`: otherwise valid H.264 remuxed into Matroska.
- `wrong-resolution.mp4`: otherwise valid H.264 at 640x360.
- `wrong-duration.mp4`: otherwise valid H.264 lasting 2 seconds.
- `wrong-codec.mp4`: otherwise valid MP4 using MPEG-4 Part 2.
- `corrupt-decode.mp4`: truncated all-intra H.264; ffprobe reads its facts but full decode fails.

The files were generated on 2026-08-26 with the build recorded in
`docs/licenses/ffmpeg-build.json`. Production code never generates fixtures and accepts no
caller-supplied FFmpeg arguments.
