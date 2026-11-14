#!/usr/bin/env bash
# Install the foresight-analytics skill into an agent's skills directory.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SKILL_NAME="foresight-analytics"
SOURCE_DIR="${REPO_ROOT}/skills/${SKILL_NAME}"

TARGET_DIR="${HOME}/.config/opencode/skills"
CONNECTION_FILE="${HOME}/.config/foresight/connection.toml"
API_BASE_URL="http://127.0.0.1:8000"
TOKEN_ENV="FORESIGHT_API_TOKEN"
SYMLINK=0

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

  --target-dir DIR       where the skill is installed
                         (default: ~/.config/opencode/skills)
  --connection-file FILE where connection.toml is written
                         (default: ~/.config/foresight/connection.toml)
  --api-base-url URL     base URL of the ForeSight API
                         (default: ${API_BASE_URL})
  --token-env NAME       environment variable holding the API token
                         (default: ${TOKEN_ENV})
  --target AGENT         shorthand: opencode | claude
  --symlink              symlink instead of copying (for development)
  -h, --help             show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-dir) TARGET_DIR="$2"; shift 2 ;;
        --connection-file) CONNECTION_FILE="$2"; shift 2 ;;
        --api-base-url) API_BASE_URL="$2"; shift 2 ;;
        --token-env) TOKEN_ENV="$2"; shift 2 ;;
        --target)
            case "$2" in
                opencode) TARGET_DIR="${HOME}/.config/opencode/skills" ;;
                claude) TARGET_DIR="${HOME}/.claude/skills" ;;
                *) echo "unknown target: $2" >&2; exit 2 ;;
            esac
            shift 2
            ;;
        --symlink) SYMLINK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "skill source not found: ${SOURCE_DIR}" >&2
    exit 1
fi

mkdir -p "${TARGET_DIR}"
DEST="${TARGET_DIR}/${SKILL_NAME}"

if [[ "${SYMLINK}" -eq 1 ]]; then
    rm -rf "${DEST}"
    ln -s "${SOURCE_DIR}" "${DEST}"
else
    rm -rf "${DEST}"
    cp -R "${SOURCE_DIR}" "${DEST}"
fi

mkdir -p "$(dirname "${CONNECTION_FILE}")"
cat > "${CONNECTION_FILE}" <<EOF
# Written by install_skill.sh
api_base_url = "${API_BASE_URL}"
token_env = "${TOKEN_ENV}"
EOF

cat <<EOF
Installed ${SKILL_NAME} to ${DEST}
Wrote connection info to ${CONNECTION_FILE}

Next steps:
  1. export ${TOKEN_ENV}=<your api token>
  2. start the service:  foresight serve   (or: docker compose up)
  3. ask your agent to analyse the chat archive
EOF
