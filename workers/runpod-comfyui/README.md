# worker-comfyui 5.8.7 fixed-workflow POC

This directory contains only the no-cost, reproducible portion of Issue #14.

Pinned upstream baseline:

- `runpod-workers/worker-comfyui` `5.8.7` at `a1981e99b1f5a7201f387653420ad1f275b97d0a`;
- compatible ComfyUI `v0.29.0` at `a8c44f9b2a0678ac4082e3529a3f43db7472acfe`;
- isolated build/snapshot CLI `comfy-cli` `v1.18.0` at
  `6a5e9d772453f27b0778b14e8a4b1e25ac5f949e`.

The official repository tags do not have a matching public
`docker.io/runpod/worker-comfyui:5.8.7[-base]` manifest. Do not guess another mutable image
tag. Build the upstream `5.8.7` source at the pinned commit in the authorized POC, publish it
to the isolated POC registry, record its digest, and pass that digest explicitly when building
this production layer from the repository root:

```powershell
docker build `
  --build-arg WORKER_COMFYUI_IMAGE=<isolated-registry>/worker-comfyui@sha256:<digest> `
  -f workers/runpod-comfyui/Dockerfile `
  -t video-factory/worker-comfyui:poc-5.8.7 .
```

The production stage removes ComfyUI Manager, does not copy comfy-cli, does not expose the
ComfyUI port, and validates all pinned commits and fixtures. The request contract accepts only
the fixed workflow ID, prompt, approved 5-second 720p dimensions, and opaque system-signed
input/output/callback claims. It has no field for workflow JSON, nodes, models, code, or URLs.

Run the free regression suite with:

```powershell
python -m unittest discover -s tests -p "test_*poc.py"
python workers/runpod-comfyui/contract/validate.py --root .
```

## One-command paid POC

Keep credentials in the process environment or an external secret manager; never put them in
the command line, request fixtures, evidence, or Git. The fail-closed preflight reports presence
only:

```powershell
uv run --no-project python scripts/run_worker_comfyui_poc.py preflight
```

Before execution, configure these operator-owned values outside the repository:

- `RUNPOD_API_KEY` and the pre-created `RUNPOD_ENDPOINT_ID`;
- `POC_REGISTRY_IMAGE` (non-`latest` tag), its `POC_IMAGE_DIGEST`, and either an existing
  Docker login or `POC_REGISTRY_USERNAME` plus `POC_REGISTRY_TOKEN`;
- `POC_COST_LIMIT_USD` (maximum accepted by the harness: USD 25) and a conservative all-in
  upper-bound `POC_COST_PER_SECOND_USD`;
- private, freshly signed `POC_REQUEST_16X9` and `POC_REQUEST_9X16` JSON paths;
- a private `POC_MEDIA_DIR` where the system claim collector writes `<attempt_id>.mp4`;
- `POC_MODEL_SHA256_JSON`, containing the verified model-name-to-SHA-256 map.

The endpoint must already reference the recorded isolated image digest and use at most one
active worker for this sequential POC. Then both aspect ratios run with one command:

```powershell
uv run --no-project python scripts/run_worker_comfyui_poc.py execute `
  --confirm-paid-poc ISSUE-14-PAID-POC `
  --evidence <private-evidence-path>\issue-14.json
```

The harness divides the remaining approved budget between unfinished cases, subtracts a
cancellation reserve, sends RunPod `policy.executionTimeout`, independently cancels on local
deadline, and stops before another submission if the remaining budget is insufficient. Cases
are sequential, so it cannot create parallel job spend. Every exit atomically records timings,
estimated upper-bound cost, failures, and cleanup results without claims or API keys.

If the process was interrupted after writing evidence, retry cleanup only:

```powershell
uv run --no-project python scripts/run_worker_comfyui_poc.py cleanup `
  --evidence <private-evidence-path>\issue-14.json
```

Cleanup requires only `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID`, and the private evidence path, so
it remains usable even if request/model/media files are no longer present. It cancels only
unfinished provider job IDs recorded by this POC. The harness does not
delete a pre-existing endpoint or registry image that it did not create; those resources remain
operator-owned. A cancellation failure is retained as `active_jobs_remaining > 0` so the POC
cannot be accepted silently.

The real RunPod POC was not executed: no independent credentials or cost authorization were
available, and the user explicitly chose to skip it and proceed without claiming a pass. The
source build verification, model hashes, image digest, GPU/cold-start/runtime data, and cost
therefore remain unverified and empty in `poc-baseline.json`. Adoption remains `CONDITIONAL`.
The one-command harness is retained for a future authorized POC.
