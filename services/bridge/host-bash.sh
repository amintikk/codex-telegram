#!/bin/bash.container
set -eu

if [ "${HOST_SHELL_MODE:-container}" != "host" ]; then
  exec /bin/bash.container "$@"
fi

resolve_host_codex_home() {
  if [ "${CODEX_AUTH_MODE:-shared}" = "per_chat" ]; then
    case "${HOME:-}" in
      /data/*)
        printf '%s/%s' "${BRIDGE_REPO_DIR:-/home/ubuntu/docker/codex-telegram}" "${HOME}"
        return 0
        ;;
    esac
  fi

  config_dir="${CODEX_CONFIG_DIR:-}"
  if [ -n "$config_dir" ]; then
    case "$config_dir" in
      /*) printf '%s' "$config_dir" ;;
      *) printf '%s/%s' "${BRIDGE_REPO_DIR:-/home/ubuntu/docker/codex-telegram}" "$config_dir" ;;
    esac
    return 0
  fi

  printf '%s' "/root/.codex"
}

rewrite_shell_snapshot_paths() {
  command_text="$1"
  host_codex_home="$(resolve_host_codex_home)"
  bridge_data_prefix="${BRIDGE_REPO_DIR:-/home/ubuntu/docker/codex-telegram}/data"
  printf '%s' "$command_text" | sed \
    -e "s#/root/.codex#${host_codex_home}#g" \
    -e "s#/data/#${bridge_data_prefix}/#g"
}

if [ "$#" -ge 2 ] && { [ "$1" = "-lc" ] || [ "$1" = "-c" ]; }; then
  rewritten_command="$(rewrite_shell_snapshot_paths "$2")"
  shift 2
  set -- -lc "$rewritten_command" "$@"
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
