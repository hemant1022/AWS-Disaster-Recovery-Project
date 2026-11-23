#!/usr/bin/env bash
# Load .env into the current bash session.
#
#   source scripts/env.sh
#
# Dot-source it so the variables land in your shell.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${repo_root}/.env"

if [[ ! -f "${env_file}" ]]; then
  echo "Missing ${env_file}. Copy .env.example to .env and fill it in." >&2
  return 1 2>/dev/null || exit 1
fi

loaded=0
while IFS= read -r raw || [[ -n "${raw}" ]]; do
  line="${raw#"${raw%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"

  [[ -z "${line}" || "${line}" == \#* ]] && continue
  [[ "${line}" != *=* ]] && continue

  name="${line%%=*}"
  value="${line#*=}"

  name="${name//[[:space:]]/}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"

  export "${name}=${value}"
  loaded=$((loaded + 1))
done < "${env_file}"

echo "Loaded ${loaded} variables from ${env_file}"
