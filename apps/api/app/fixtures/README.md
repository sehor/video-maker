# Mock Provider fixtures

`mock-success.mp4` is a deterministic, locally generated one-second color frame used by
`MockVideoProvider`. Tests and development requests copy these bytes instead of invoking
FFmpeg for every Job. Failure, timeout, duplicate-callback, and corrupt-output modes remain
fully simulated and never contact a GPU or external provider.
