#!/bin/sh
set -eu

if [ "${HOST_SHELL_MODE:-container}" != "host" ]; then
  exec /bin/bash.container "$@"
fi

exec nsenter \
  --target 1 \
  --mount \
  --uts \
  --ipc \
  --net \
  --pid \
  --wdns="$(pwd)" \
  -- /bin/bash "$@"
