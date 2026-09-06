#!/bin/sh
set -eu

# Never inherit the upstream development SSH or public ComfyUI API switches.
unset PUBLIC_KEY
export SERVE_API_LOCALLY=false

exec /start.sh
