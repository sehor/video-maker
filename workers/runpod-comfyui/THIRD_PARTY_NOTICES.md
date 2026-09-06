# worker-comfyui release notices

This notice accompanies the fixed `fast_wan_i2v_720_v1` worker release record. It is an
engineering inventory, not legal advice. A production release remains blocked until every
model row is reviewed and the immutable image SBOM is attached.

## Runtime components

- `runpod-workers/worker-comfyui` 5.8.7, commit
  `a1981e99b1f5a7201f387653420ad1f275b97d0a`, AGPL-3.0-only. Corresponding source:
  <https://github.com/runpod-workers/worker-comfyui/tree/a1981e99b1f5a7201f387653420ad1f275b97d0a>.
  The production layer removes ComfyUI Manager and adds the fixed contract, workflow, release
  validation, and output provenance requirements maintained in this repository.
- ComfyUI 0.29.0, commit `a8c44f9b2a0678ac4082e3529a3f43db7472acfe`,
  GPL-3.0-only. Corresponding source:
  <https://github.com/comfyanonymous/ComfyUI/tree/a8c44f9b2a0678ac4082e3529a3f43db7472acfe>.
- FFmpeg/ffprobe: the exact Worker build configuration and license status must be captured from
  the immutable release image in its SPDX SBOM and release evidence. The development-host
  snapshot in `docs/licenses/ffmpeg-build.json` does not satisfy this Worker gate.

## Build-only component

- comfy-cli 1.18.0, commit `6a5e9d772453f27b0778b14e8a4b1e25ac5f949e`,
  GPL-3.0-only. It is isolated in the snapshot build stage and is not copied into the production
  layer. Source: <https://github.com/Comfy-Org/comfy-cli/tree/6a5e9d772453f27b0778b14e8a4b1e25ac5f949e>.

## Models and custom nodes

The authoritative model and custom-node reviews are `docs/licenses/models.csv` and
`docs/licenses/custom-nodes.csv`. They are intentionally empty while the paid POC is skipped.
No production release may be recorded until the fixed model files have verified SHA-256 values,
license terms, SaaS/commercial approval, and notice obligations. The fixed workflow currently
declares no external custom nodes.
