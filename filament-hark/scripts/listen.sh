#!/usr/bin/env bash
STATE=${FILAMENT_STATE:-/workspace/filament}
# shellcheck source=package/scripts/restart_listener.sh
source "$STATE/restart_listener.sh"
init_job 480 || { echo NO_WORK; exit 3; }

# Remove expired attachments even when this run is paused or a duplicate.
json_helper clean-media 2>> "$STATE/listen.log" || :

budget=480
if [ "$#" -ne 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != --budget ] || ! [[ $2 =~ ^[0-9]{1,3}$ ]]; then echo BAD_BUDGET; exit 3; fi
    budget=$((10#$2))
fi
if [ "$budget" -lt 60 ] || [ "$budget" -gt 540 ]; then echo BAD_BUDGET; exit 3; fi
DEADLINE=$((DEADLINE + budget - 480))

# shellcheck disable=SC2329 # Invoked by the EXIT trap.
cleanup() {
    stop_child
    release_lock "$STATE/reply.lock.d"
    release_lock "$STATE/listener.lock.d"
    if [ "$(cat "$STATE/listener.pid" 2>/dev/null)" = "$$" ]; then rm -f "$STATE/listener.pid"; fi
    if [ "$(cat "$STATE/restart.pending" 2>/dev/null)" = "$$" ]; then rm -f "$STATE/restart.pending"; fi
    [ -z "${RUN:-}" ] || rm -rf "$RUN"
}
# shellcheck disable=SC2329 # Invoked by the TERM and INT traps.
killed() { stop_child; log_event LISTENER-KILLED-SELF; echo KILLED; exit 3; }
trap cleanup EXIT
trap killed TERM INT
paused && exit 2
duplicate() { log_event LISTENER-DUPLICATE-EXIT; echo DUPLICATE; exit 3; }

if ! mkdir "$STATE/listener.lock.d" 2>/dev/null; then
    reclaim_stale "$STATE/listener.lock.d" || duplicate
    mkdir "$STATE/listener.lock.d" 2>/dev/null || duplicate
fi
printf '%s\n' "$$" > "$STATE/listener.lock.d/pid"
printf '%s\n' "$$" > "$STATE/listener.pid"
date +%s > "$STATE/listener.lock.d/started"
if [ "$(cat "$STATE/restart.pending" 2>/dev/null)" = "$$" ]; then rm -f "$STATE/restart.pending"; fi
RUN=$(mktemp -d "$STATE/.listen.XXXXXX") || { echo NO_WORK; exit 3; }

fatal_if_paused() { paused && exit 2; return 0; }
request() {
    local rc
    run_mcp "$1" "$2" "${3:-15}"; rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 125 ]; then
        log_event "REQUEST-FAILED tool=$1 rc=$rc"
        log_file "$RUN/err" listen.log; mark_failure; fatal_if_paused
    fi
    return "$rc"
}

rem=$(remaining)
if [ "$rem" -ge 10 ]; then
    cap=15; [ "$cap" -le "$rem" ] || cap=$rem
    curl -sS -m "$cap" -X POST https://api.filament.dm/mcp/agents/heartbeat -o /dev/null > "$RUN/out" 2> "$RUN/err" &
    CPID=$!; wait "$CPID"; rc=$?; CPID=
    if [ "$rc" -ne 0 ]; then log_event "HEARTBEAT-FAILED rc=$rc"; log_file "$RUN/err" listen.log; fi
fi

SELF_ID=$(json_helper self fresh "$STATE/self.json" 2>/dev/null) || SELF_ID=
if [ -z "$SELF_ID" ]; then
    if request get_self '{}'; then
        SELF_ID=$(json_helper self save "$RUN/out" 2>> "$STATE/listen.log") || SELF_ID=
    fi
    if [ -z "$SELF_ID" ]; then SELF_ID=$(json_helper self stale "$STATE/self.json" 2>/dev/null) || SELF_ID=; fi
fi
for kind in invites vouches; do
    if [ "$kind" = invites ]; then list=list_pending_invites; accept=accept_invite; else list=list_vouches; accept=accept_vouch; fi
    if request "$list" '{}'; then
        if json_helper invites "$RUN/out" "$kind" > "$RUN/invites" 2>> "$STATE/listen.log"; then
            while IFS= read -r args; do request "$accept" "$args" || :; done < "$RUN/invites"
        fi
    fi
done

backoff=2
ready=0
while [ "$(remaining)" -ge 20 ]; do
    rem=$(remaining); wait_seconds=$((rem - 15))
    [ "$wait_seconds" -le 60 ] || wait_seconds=60
    run_mcp poll_work "{\"wait_seconds\":$wait_seconds,\"max_items\":10}" "$((wait_seconds + 15))"; rc=$?
    printf '%s POLL rc=%s wait=%s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$rc" "$wait_seconds" >> "$STATE/listen.log"
    if [ "$rc" -eq 125 ]; then break; fi
    if [ "$rc" -ne 0 ]; then
        log_file "$RUN/err" listen.log; mark_failure; fatal_if_paused
        bounded_sleep "$backoff"
        backoff=$((backoff * 2)); [ "$backoff" -le 30 ] || backoff=30
        continue
    fi
    if [ "$ready" -eq 0 ]; then log_event "LISTENER-READY $$"; ready=1; fi
    backoff=2
    rm -f "$RUN/ack"
    if ! json_helper poll "$RUN" "$SELF_ID" > "$RUN/verdict" 2>> "$STATE/listen.log"; then
        log_event BAD-JSON; bounded_sleep 5; continue
    fi
    read -r verdict delay count timestamp filtered < "$RUN/verdict"
    if [ "$verdict" = busy ]; then bounded_sleep "$delay"; continue; fi
    if [ "$filtered" -gt 0 ]; then log_event "FILTERED $filtered"; fi
    if [ -f "$RUN/ack" ]; then
        # Filtering acks are best effort, including auth errors, per step 6e.
        run_mcp poll_work "$(cat "$RUN/ack")" 15; rc=$?
        if [ "$rc" -ne 0 ] && [ "$rc" -ne 125 ]; then
            log_event "FILTER-ACK-FAILED rc=$rc"; log_file "$RUN/err" listen.log
        fi
    fi
    if [ "$verdict" = work ]; then
        n=0
        while IFS= read -r args; do
            n=$((n + 1))
            run_mcp get_recent_messages "$args" 15; rc=$?
            if [ "$rc" -eq 0 ]; then
                cp "$RUN/out" "$RUN/media-$n"
            elif [ "$rc" -ne 125 ]; then
                log_event "MEDIA-ENRICH-FAILED rc=$rc"; log_file "$RUN/err" listen.log
            fi
        done < "$RUN/media-requests"
        if ! take_lock "$STATE/reply.lock.d" 25; then
            log_event DELIVER-LOCK-TIMEOUT; echo NO_WORK; exit 3
        fi
        if ! json_helper deliver "$RUN" 2>> "$STATE/listen.log"; then
            log_event BAD-JSON; echo NO_WORK; exit 3
        fi
        release_lock "$STATE/reply.lock.d"
        log_event "WORK $timestamp items=$count"
        cat "$RUN/delivery"
        echo "ITEMS $count"
        log_event WAKE-DELIVERED
        exit 0
    fi
    [ "$verdict" != truncated ] || continue
    rem=$(remaining); rem=$((rem - 20))
    if [ "$rem" -gt 0 ]; then
        delay=$(awk -v n="$delay" -v r="$rem" 'BEGIN {print (n<r?n:r)}')
        bounded_sleep "$delay"
    fi
done
echo NO_WORK
exit 3
