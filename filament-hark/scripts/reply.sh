#!/usr/bin/env bash
STATE=${FILAMENT_STATE:-/workspace/filament}
# shellcheck source=package/scripts/restart_listener.sh
source "$STATE/restart_listener.sh"
if [ -z "${FILAMENT_JOB_START+x}" ]; then FILAMENT_JOB_START=$(date +%s); fi
export FILAMENT_JOB_START
init_job 480 || { exec bash "$STATE/restart_listener.sh"; }
RUN=
cleanup() { stop_child; release_lock "$STATE/reply.lock.d"; [ -z "$RUN" ] || rm -rf "$RUN"; }
chain() {
    cleanup
    log_event RELAUNCHED
    trap - EXIT TERM INT
    # No persistent descriptors: all redirections are scoped to commands.
    exec bash "$STATE/restart_listener.sh"
}
trap cleanup EXIT
trap chain TERM INT
mode=reply
if [ "${1:-}" = --ack ]; then mode=ack; shift; fi
number=${1:-}
log_event "REPLY-START item=$number"
if [ "$#" -ne 1 ] || ! [[ $number =~ ^[1-9][0-9]*$ ]] || [ ! -f "$STATE/items/$number.json" ]; then
    log_event BAD-ITEM; echo BAD_ITEM; chain
fi
RUN=$(mktemp -d "$STATE/.reply.XXXXXX") || chain
if [ "$mode" = reply ]; then cat > "$RUN/body"; else : > "$RUN/body"; fi
valid=$(json_helper validate "$RUN" "$number" "$mode" 2>> "$STATE/reply.log") || valid=BAD_ITEM
case "$valid" in
    BAD_ITEM) log_event BAD-ITEM; echo BAD_ITEM; chain;;
    EMPTY_BODY) log_event EMPTY-BODY; echo EMPTY_BODY; chain;;
    NO_IDS) log_event NO-IDS; chain;;
esac
if ! take_lock "$STATE/reply.lock.d" 150; then log_event LOCK-TIMEOUT; chain; fi
# Recheck after waiting: another worker may already have removed this item.
if [ ! -f "$STATE/items/$number.json" ]; then log_event BAD-ITEM; echo BAD_ITEM; chain; fi
if [ "$(remaining)" -lt 10 ]; then log_event DEADLINE; chain; fi
plan=$(json_helper plan "$RUN" "$mode" 2>> "$STATE/reply.log") || { log_event STATE-ERROR; chain; }
cap=60; word=REPLIED
if [ "$plan" = ack ]; then cap=30; word=ACKED; fi
run_mcp "$(cat "$RUN/tool")" "$(cat "$RUN/args")" "$cap"; rc=$?
log_event "$word rc=$rc"
if [ "$rc" -ne 125 ]; then
    cp "$RUN/out" "$STATE/last_result.json"
    log_file "$RUN/out" reply.log
    if [ "$rc" -ne 0 ]; then log_file "$RUN/err" reply.log; mark_failure; fi
fi
# Remove while still holding the lock, so a concurrent same-number reply fails.
rm -f "$STATE/items/$number.json"
release_lock "$STATE/reply.lock.d"
chain
