# Mock Provider fixtures

`mock-success.mp4` is a locally generated two-second H.264 video (1280×720, 25 fps, yuv420p) used by
`MockVideoProvider`. Tests and development requests copy these bytes instead of invoking
FFmpeg for every Job. Failure, timeout, duplicate-callback, and corrupt-output modes remain
fully simulated and never contact a GPU or external provider.

Regenerate from the repository root with native FFmpeg:

```powershell
ffmpeg -nostdin -v error -f lavfi -i color=c=black:s=1280x720:r=25:d=2 -c:v libx264 -preset veryfast -pix_fmt yuv420p -movflags +faststart -y apps/api/app/fixtures/mock-success.mp4
```

The media regression validates these facts; E2E also requires successful browser loading.
The old MPEG-4 Part 2 sample was not browser-playable and its one-second duration disagreed
with the provider's two-second metadata.
