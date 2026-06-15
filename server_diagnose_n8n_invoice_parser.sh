#!/usr/bin/env bash
set -u

N8N_DIR="${N8N_DIR:-/home/ferreteria/n8n}"
COMPOSE_FILE="$N8N_DIR/docker-compose.yml"

section() {
  printf '\n===== %s =====\n' "$1"
}

run() {
  printf '\n$ %s\n' "$*"
  "$@" 2>&1 || true
}

section "host"
run hostname
run whoami
run id
run date -Is
run uname -a

section "n8n directory"
run ls -ld "$N8N_DIR"
run ls -la "$N8N_DIR"
if [ -f "$COMPOSE_FILE" ]; then
  echo
  echo "--- $COMPOSE_FILE ---"
  sed -n '1,220p' "$COMPOSE_FILE" 2>&1 || true
else
  echo "MISSING: $COMPOSE_FILE"
fi

section "docker access"
run command -v docker
run docker --version
run docker compose version
run docker-compose --version
run docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
run docker compose -f "$COMPOSE_FILE" ps

section "n8n container inspect"
run docker inspect n8n --format 'Image={{.Config.Image}}'
run docker inspect n8n --format 'Entrypoint={{json .Config.Entrypoint}} Cmd={{json .Config.Cmd}}'
run docker inspect n8n --format 'Networks={{json .NetworkSettings.Networks}}'
run docker inspect n8n --format 'Mounts={{json .Mounts}}'
run docker inspect n8n --format 'Env={{json .Config.Env}}'

section "inside n8n container"
run docker exec n8n sh -lc 'id'
run docker exec n8n sh -lc 'cat /etc/os-release || true'
run docker exec n8n sh -lc 'node -v; npm -v; n8n --version'
run docker exec n8n sh -lc 'command -v tesseract || true; command -v pdftoppm || true; command -v pdftotext || true'
run docker exec n8n sh -lc 'node -e "for (const m of [\"crypto\",\"fs\",\"path\",\"child_process\"]) { try { require(m); console.log(m+\": ok\") } catch(e) { console.log(m+\": \"+e.message) } }"'
run docker exec n8n sh -lc 'node -e "try { require(\"imghash\"); console.log(\"imghash: ok\") } catch(e) { console.log(\"imghash: \"+e.message) }"'

section "network reachability from container"
run docker exec n8n sh -lc 'ip route || true'
run docker exec n8n sh -lc 'getent hosts host.docker.internal || true'
run docker exec n8n sh -lc 'for u in http://127.0.0.1:5678/healthz http://172.17.0.1:8765/health http://172.18.0.1:8765/health http://192.168.0.195:8765/health; do echo "-- $u"; timeout 5 wget -qO- "$u" || true; echo; done'

section "host tools and dirs"
run command -v tesseract
run command -v pdftoppm
run command -v pdftotext
run command -v python3
run python3 --version
run ls -ld "$N8N_DIR/data" "$N8N_DIR/data/facturas_parseadas" "$N8N_DIR/data/invoice_parser"
run find "$N8N_DIR/data/facturas_parseadas" -maxdepth 2 -type f -printf '%p %s %TY-%Tm-%Td %TH:%TM\n'

section "recommendation hints"
cat <<'EOF'
Look for:
- docker access errors: if present, rerun this script with sudo.
- n8n image/version: use this exact image tag for a safer custom Dockerfile.
- container OS: apk means Alpine, apt-get means Debian/Ubuntu.
- missing tools: tesseract/pdftoppm/imghash are required for the full in-container workflow.
- NODE_FUNCTION_ALLOW_* env: needed only if using Code nodes with builtins/external modules.
EOF

