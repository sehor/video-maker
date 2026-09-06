# syntax=docker/dockerfile:1.7

ARG WORKER_COMFYUI_IMAGE

FROM ${WORKER_COMFYUI_IMAGE} AS snapshot

ARG WORKER_COMFYUI_IMAGE
ARG COMFY_CLI_VERSION=1.18.0
ARG COMFYUI_COMMIT=a8c44f9b2a0678ac4082e3529a3f43db7472acfe

USER root
RUN printf '%s' "${WORKER_COMFYUI_IMAGE}" \
      | grep -Eq '^.+@sha256:[0-9a-f]{64}$' \
    && python -m venv /opt/poc-comfy-cli \
    && /opt/poc-comfy-cli/bin/python -m pip install --no-cache-dir \
        "comfy-cli==${COMFY_CLI_VERSION}"

COPY workers/runpod-comfyui/comfy-lock.yaml /opt/poc/comfy-lock.yaml

# The release image is accepted only when it contains the ComfyUI commit paired
# with worker-comfyui 5.8.7. comfy-cli remains isolated in this build-only stage.
RUN test "$(git -C /comfyui rev-parse HEAD)" = "${COMFYUI_COMMIT}" \
    && /opt/poc-comfy-cli/bin/comfy --version \
    && /opt/poc-comfy-cli/bin/comfy --workspace /comfyui node save-snapshot \
        --output /opt/poc/comfy-snapshot.json

FROM ${WORKER_COMFYUI_IMAGE} AS production

ARG WORKER_COMFYUI_COMMIT=a1981e99b1f5a7201f387653420ad1f275b97d0a
ARG COMFYUI_COMMIT=a8c44f9b2a0678ac4082e3529a3f43db7472acfe

USER root
COPY --from=snapshot /opt/poc/comfy-snapshot.json /opt/video-factory/comfy-snapshot.json
COPY workers/runpod-comfyui/comfy-lock.yaml /opt/video-factory/comfy-lock.yaml
COPY workers/runpod-comfyui/contract /opt/video-factory/contract
COPY workflows/manifests/fast_wan_i2v_720_v1 /opt/video-factory/workflow
COPY workers/runpod-comfyui/locked-start.sh /opt/video-factory/locked-start.sh

# Manager is a development surface and must not exist in the production image.
RUN rm -rf \
        /comfyui/custom_nodes/ComfyUI-Manager \
        /comfyui/custom_nodes/comfyui-manager \
    && test "$(git -C /comfyui rev-parse HEAD)" = "${COMFYUI_COMMIT}" \
    && test ! -e /comfyui/custom_nodes/ComfyUI-Manager \
    && test ! -e /comfyui/custom_nodes/comfyui-manager \
    && chmod 0555 /opt/video-factory/locked-start.sh \
    && python /opt/video-factory/contract/validate.py \
        --root /opt/video-factory \
        --worker-commit "${WORKER_COMFYUI_COMMIT}" \
        --comfyui-commit "${COMFYUI_COMMIT}"

ENV COMFYUI_MANAGER_DISABLED=1 \
    COMFY_NO_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    VIDEO_FACTORY_WORKFLOW_ROOT=/opt/video-factory/workflow \
    VIDEO_FACTORY_CONTRACT_ROOT=/opt/video-factory/contract

CMD ["/opt/video-factory/locked-start.sh"]
