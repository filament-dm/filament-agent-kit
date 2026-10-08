#!/usr/bin/env bash
STATE=${FILAMENT_STATE:-/workspace/filament}
# shellcheck source=package/scripts/restart_listener.sh
source "$STATE/restart_listener.sh"
init_job 480 || { echo NEED_START; exit 0; }
RUN=
# shellcheck disable=SC2329 # Invoked by EXIT.
cleanup() { stop_child; [ -z "$RUN" ] || rm -rf "$RUN"; }
trap cleanup EXIT
trap 'exit 0' TERM INT
stamp() { date +%Y-%m-%dT%H:%M:%S%z | sed 's/\([+-][0-9][0-9]\)\([0-9][0-9]\)$/\1:\2/'; } # Portable equivalent of date -Is.
case "${1:-}" in
    check)
        status=$(json_helper backstop-check 2>> "$STATE/backstop.log") || status=WEDGED
        if [ -f "$STATE/auth_failed" ]; then
            status=PAUSED
        elif ! live_listener "$(cat "$STATE/listener.lock.d/pid" 2>/dev/null)"; then
            status=NEED_START
        fi
        printf '%s %s\n' "$(stamp)" "$status" >> "$STATE/backstop.log"
        echo "$status"
        ;;
    started)
        printf '%s STARTED\n' "$(stamp)" >> "$STATE/backstop.log"
        ;;
    warn)
        payload=$(json_helper quota "${2:-}" "${3:-}") || { echo BAD_PERCENT; exit 1; }
        if [ "$payload" = ALREADY_WARNED ]; then echo ALREADY_WARNED; exit 0; fi
        RUN=$(mktemp -d "$STATE/.backstop.XXXXXX") || { echo 'WARNED rc=1'; exit 0; }
        run_mcp message_principal "$payload" 60; rc=$?
        if [ "$rc" -eq 0 ]; then touch "$STATE/quota_warned"; fi
        echo "WARNED rc=$rc"
        ;;
    *) echo 'Usage: backstop.sh check|started|warn <percent> <resets_at>'; exit 1;;
esac
exit 0
