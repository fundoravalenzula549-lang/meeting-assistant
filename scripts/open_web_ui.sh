#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
URL="http://127.0.0.1:8765/"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/server.log"
PID_FILE="$LOG_DIR/server.pid"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export NO_PROXY="127.0.0.1,localhost,::1,${NO_PROXY:-}"
export no_proxy="$NO_PROXY"

mkdir -p "$LOG_DIR"

is_web_ui_up() {
  /usr/bin/curl -fsS --max-time 1 "$URL" >/dev/null 2>&1
}

notify() {
  /usr/bin/osascript -e "display notification \"$1\" with title \"会议转录工作台\"" >/dev/null 2>&1 || true
}

if ! is_web_ui_up; then
  cd "$PROJECT_DIR"
  if [[ -f "$PID_FILE" ]] && /bin/kill -0 "$(<"$PID_FILE")" >/dev/null 2>&1; then
    old_pid="$(<"$PID_FILE")"
    /bin/kill "$old_pid" >/dev/null 2>&1 || true
    for _ in {1..10}; do
      if ! /bin/kill -0 "$old_pid" >/dev/null 2>&1; then
        break
      fi
      /bin/sleep 0.3
    done
    if /bin/kill -0 "$old_pid" >/dev/null 2>&1; then
      /bin/kill -9 "$old_pid" >/dev/null 2>&1 || true
    fi
  fi

  /usr/bin/nohup "$PROJECT_DIR/.venv/bin/meeting-workbench" server >> "$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"
  notify "正在启动本地服务"

  for _ in {1..40}; do
    if is_web_ui_up; then
      break
    fi
    /bin/sleep 0.5
  done
fi

if is_web_ui_up; then
  /usr/bin/open "$URL"
  notify "Web UI 已打开"
else
  /usr/bin/osascript -e "display dialog \"会议转录工作台没有启动成功。请查看日志：$LOG_FILE\" buttons {\"知道了\"} default button 1 with icon caution" >/dev/null 2>&1 || true
  exit 1
fi
