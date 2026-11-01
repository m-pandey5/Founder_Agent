#!/usr/bin/env bash
# Founder_Agent - one-command setup and run.
set -euo pipefail
cd "$(dirname "$0")"

# Load local secrets if present. .env is gitignored and never read by the app
# itself - only exported into this process's environment.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ ! -d .venv ]; then
  echo "==> creating virtualenv"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

case "${1:-serve}" in
  serve) exec .venv/bin/python -m founder_agent.cli serve "${@:2}" ;;
  demo)  exec .venv/bin/python -m founder_agent.cli demo ;;
  test)  exec .venv/bin/python -m pytest tests -q ;;
  *)     exec .venv/bin/python -m founder_agent.cli "$@" ;;
esac
