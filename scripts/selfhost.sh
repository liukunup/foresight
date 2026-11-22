#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
CONFIG_FILE="$ROOT/config.toml"

die() {
    printf '错误: %s\n' "$1" >&2
    exit 1
}

info() {
    printf '==> %s\n' "$1"
}

trim() {
    local value="$1"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

env_value() {
    local key="$1" line value
    [[ -f "$ENV_FILE" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*${key}=(.*)$ ]] || continue
        value="${BASH_REMATCH[1]}"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        printf '%s' "$value"
        return 0
    done < "$ENV_FILE"
    return 1
}

set_env() {
    local key="$1" value="$2" line replaced=0 tmp
    tmp="$(mktemp)"
    if [[ -f "$ENV_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^[[:space:]]*${key}= && "$replaced" -eq 0 ]]; then
                printf '%s=%s\n' "$key" "$value" >> "$tmp"
                replaced=1
            else
                printf '%s\n' "$line" >> "$tmp"
            fi
        done < "$ENV_FILE"
    fi
    if [[ "$replaced" -eq 0 ]]; then
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

random_hex() {
    od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'
}

prompt_unless_set() {
    local name="$1" label="$2" value="$3" hidden="${4:-}"
    if [[ -n "$value" ]]; then
        printf '%s' "$value"
        return 0
    fi
    if [[ ! -t 0 ]]; then
        die "请在终端交互运行 ${name}，或事先设置 ${name} 环境变量"
    fi
    if [[ "$hidden" == "hidden" ]]; then
        read -rsp "${label}: " value
        printf '\n' >&2
    else
        read -rp "${label}: " value
    fi
    printf '%s' "$value"
}

command -v docker >/dev/null 2>&1 || die "未找到 docker，请先安装 Docker"
docker compose version >/dev/null 2>&1 || die "docker compose 不可用，请安装 Compose 插件"
[[ -f .env.example ]] || die "缺少 .env.example"
[[ -f config.example.toml ]] || die "缺少 config.example.toml"

if [[ ! -f "$ENV_FILE" ]]; then
    cp .env.example "$ENV_FILE"
    info "已从 .env.example 创建 .env"
fi

DISCORD_TOKEN="$(trim "${DISCORD_TOKEN:-$(env_value DISCORD_TOKEN || true)}")"
DISCORD_TOKEN="$(prompt_unless_set DISCORD_TOKEN '请输入 Discord bot token' "$DISCORD_TOKEN" hidden)"
[[ -n "$DISCORD_TOKEN" ]] || die "DISCORD_TOKEN 不能为空"
set_env DISCORD_TOKEN "$DISCORD_TOKEN"

CHANNEL_IDS="$(trim "${DISCORD_CHANNEL_IDS:-$(env_value DISCORD_CHANNEL_IDS || true)}")"
CHANNEL_IDS="$(prompt_unless_set DISCORD_CHANNEL_IDS '请输入要采集的频道 ID（多个用逗号分隔）' "$CHANNEL_IDS")"
CHANNEL_IDS="$(trim "$CHANNEL_IDS")"
normalized_ids=""
IFS=',' read -ra channel_parts <<< "$CHANNEL_IDS"
for part in "${channel_parts[@]}"; do
    part="$(trim "$part")"
    [[ -n "$part" ]] || continue
    normalized_ids="${normalized_ids:+$normalized_ids,}$part"
done
CHANNEL_IDS="$normalized_ids"
[[ "$CHANNEL_IDS" =~ ^[0-9]+(,[0-9]+)*$ ]] || die "频道 ID 必须是数字，多个用逗号分隔"
set_env DISCORD_CHANNEL_IDS "$CHANNEL_IDS"

API_TOKEN="$(env_value FORESIGHT_API_TOKEN || true)"
[[ -n "$API_TOKEN" ]] || API_TOKEN="$(random_hex 32)"
set_env FORESIGHT_API_TOKEN "$API_TOKEN"

DB_PASSWORD="$(env_value FORESIGHT_DB_PASSWORD || true)"
[[ -n "$DB_PASSWORD" ]] || DB_PASSWORD="$(random_hex 24)"
set_env FORESIGHT_DB_PASSWORD "$DB_PASSWORD"

S3_ACCESS_KEY="$(env_value FORESIGHT_S3_ACCESS_KEY_ID || true)"
[[ -n "$S3_ACCESS_KEY" ]] || S3_ACCESS_KEY="$(random_hex 12 | tr 'a-f' 'A-F')"
set_env FORESIGHT_S3_ACCESS_KEY_ID "$S3_ACCESS_KEY"

S3_SECRET="$(env_value FORESIGHT_S3_SECRET_ACCESS_KEY || true)"
[[ -n "$S3_SECRET" ]] || S3_SECRET="$(random_hex 32)"
set_env FORESIGHT_S3_SECRET_ACCESS_KEY "$S3_SECRET"

while IFS='=' read -r key value; do
    set_env "$key" "$value"
done <<EOF
COMPOSE_PROFILES=mysql,rustfs
FORESIGHT_DATABASE_BACKEND=mysql
FORESIGHT_DATABASE_HOST=mysql
FORESIGHT_DATABASE_PORT=3306
FORESIGHT_DATABASE_NAME=foresight
FORESIGHT_DATABASE_USER=foresight
FORESIGHT_MEDIA_BACKEND=s3
FORESIGHT_S3_BUCKET=foresight
FORESIGHT_S3_ENDPOINT_URL=http://rustfs:9000
FORESIGHT_S3_REGION=us-east-1
FORESIGHT_S3_PREFIX=foresight
FORESIGHT_S3_USE_PATH_STYLE=true
EOF

chmod 600 "$ENV_FILE"

if [[ ! -f "$CONFIG_FILE" ]]; then
    cp config.example.toml "$CONFIG_FILE"
    info "已从 config.example.toml 创建 config.toml"
fi

API_PORT="$(env_value FORESIGHT_PORT || true)"
API_PORT="${API_PORT:-8000}"
RUSTFS_PORT="$(env_value RUSTFS_PORT || true)"
RUSTFS_PORT="${RUSTFS_PORT:-9000}"
RUSTFS_CONSOLE_PORT="$(env_value RUSTFS_CONSOLE_PORT || true)"
RUSTFS_CONSOLE_PORT="${RUSTFS_CONSOLE_PORT:-9001}"

info "启动 MySQL 与 RustFS，并初始化数据库"
docker compose up -d mysql rustfs
docker compose run --rm foresight init-db

info "启动 ForeSight"
docker compose up -d
docker compose ps

cat <<EOF

ForeSight 自托管已就绪（MySQL + RustFS）

  API 地址:        http://<服务器地址>:${API_PORT}
  API 文档:        http://<服务器地址>:${API_PORT}/docs
  RustFS 控制台:   http://<服务器地址>:${RUSTFS_CONSOLE_PORT}
  RustFS S3 API:   http://<服务器地址>:${RUSTFS_PORT}

  API Bearer token:      ${API_TOKEN}
  数据库密码:            ${DB_PASSWORD}
  RustFS access key:     ${S3_ACCESS_KEY}
  RustFS secret key:     ${S3_SECRET}
  采集频道 ID:           ${CHANNEL_IDS}

以上密钥已写入 .env（权限 600），不要提交到版本库。采集前请在 Discord 开发者后台开启 MESSAGE CONTENT INTENT 并把 bot 邀请进服务器。
常用命令: make status / make down / make restart
EOF
