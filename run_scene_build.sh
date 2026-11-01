#!/usr/bin/env bash
# Run the scene pipeline on one project folder inside the hardened container.
set -euo pipefail

IMAGE="${SCENE_IMAGE:-moneyprinterturbo-scene:local}"
PROJECT_ARG="${1:?usage: run_scene_build.sh <project-folder> [validate|plan|build|status] [--text] [--output-name NAME] [--overwrite]}"
COMMAND="${2:-build}"
shift $(( $# < 2 ? $# : 2 ))
# Only these flags are passed through to the pipeline.
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --text|--overwrite) EXTRA+=("$1") ;;
    --output-name) [ $# -ge 2 ] || { echo "--output-name needs a value" >&2; exit 2; }; EXTRA+=("$1" "$2"); shift ;;
    *) echo "unsupported option: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$COMMAND" in validate|plan|build|status) ;; *) echo "unsupported command: $COMMAND" >&2; exit 2 ;; esac

REPO="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(cd "$PROJECT_ARG" && pwd)"
[ -f "$PROJECT/scenes.json" ] || { echo "no scenes.json in $PROJECT" >&2; exit 2; }

# Refuse folders that would expose secrets.
for secret in "$HOME/.ssh" "$HOME/.config" "$HOME/.aws" "$HOME/.docker"; do
  if [[ "$secret/" == "$PROJECT/"* || "$PROJECT/" == "$secret/"* ]]; then
    echo "refusing project folder that contains or sits inside $secret" >&2; exit 2
  fi
done
if [ -n "$(find "$PROJECT" -maxdepth 3 -name '.env*' -print -quit)" ]; then
  echo "refusing project folder that contains .env files" >&2; exit 2
fi

# Temp copy of app/ and a keyless config; never the real config.toml or storage/.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -R "$REPO/app" "$WORK/app"
cp "$REPO/config.example.toml" "$WORK/config.toml"

run() {  # run <network> <command> [extra args]
  local network="$1" command="$2"; shift 2
  docker run --rm --network "$network" --cap-drop ALL --security-opt no-new-privileges \
    --user 1000:1000 --read-only --tmpfs /tmp:rw,size=512m \
    --tmpfs /MoneyPrinterTurbo/storage:rw,uid=1000,size=256m \
    --memory 2g --memory-swap 2g --cpus 2 --pids-limit 256 \
    -v "$WORK/app:/MoneyPrinterTurbo/app:ro" \
    -v "$WORK/config.toml:/MoneyPrinterTurbo/config.toml:ro" \
    -v "$PROJECT:/project" -w /MoneyPrinterTurbo \
    -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
    "$IMAGE" python -m app.services.scene_pipeline --project /project "$command" ${@+"$@"}
}

NETWORK=none
if [ "$COMMAND" = "build" ]; then
  # Stop if this check fails; never fall through to a build with the wrong network.
  if ! NEED="$(run none needs-network)"; then
    echo "needs-network check failed; stopping (the build was not started)" >&2
    exit 1
  fi
  case "$NEED" in
    yes) NETWORK=bridge; echo "network on: some scenes need narration generated" >&2 ;;
    no) ;;
    *) echo "unexpected needs-network answer: '$NEED'; stopping" >&2; exit 1 ;;
  esac
fi
run "$NETWORK" "$COMMAND" ${EXTRA[@]+"${EXTRA[@]}"}

LEFT="$(docker ps -a -q --filter "ancestor=$IMAGE")"
[ -z "$LEFT" ] || echo "warning: containers still present: $LEFT" >&2
