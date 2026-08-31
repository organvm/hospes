#!/usr/bin/env bash

# Shared, source-only shell lifecycle primitives for the synthetic demo.

synthetic_demo_run_clean_env() {
  local python_root="$1"
  shift
  env -i \
    "HOME=$HOME" \
    "PATH=$PATH" \
    "PYTHONPATH=$python_root" \
    "LANG=${LANG:-C.UTF-8}" \
    "TMPDIR=${TMPDIR:-/tmp}" \
    "$@"
}

synthetic_demo_terminate_started() {
  local started_pid
  local attempt
  local remaining
  local cleanup_failed=0
  local -a bounded_pids=()

  for started_pid in "$@"; do
    if [[ "$started_pid" =~ ^[0-9]+$ ]] && (( started_pid > 1 )); then
      bounded_pids+=("$started_pid")
    else
      cleanup_failed=1
    fi
  done
  if [[ ${#bounded_pids[@]} -eq 0 ]]; then
    return "$cleanup_failed"
  fi

  for started_pid in "${bounded_pids[@]}"; do
    kill -TERM "$started_pid" 2>/dev/null || true
  done
  for attempt in {1..40}; do
    remaining=0
    for started_pid in "${bounded_pids[@]}"; do
      if kill -0 "$started_pid" 2>/dev/null; then
        remaining=1
      fi
    done
    if [[ "$remaining" -eq 0 ]]; then
      return "$cleanup_failed"
    fi
    sleep 0.25
  done

  for started_pid in "${bounded_pids[@]}"; do
    kill -KILL "$started_pid" 2>/dev/null || true
  done
  for attempt in {1..20}; do
    remaining=0
    for started_pid in "${bounded_pids[@]}"; do
      if kill -0 "$started_pid" 2>/dev/null; then
        remaining=1
      fi
    done
    if [[ "$remaining" -eq 0 ]]; then
      return "$cleanup_failed"
    fi
    sleep 0.1
  done
  return 1
}

synthetic_demo_failed_start() {
  local failure_status="$1"
  local ledger_path="$2"
  shift 2
  local cleanup_status=0
  local ledger_directory
  local ledger_name
  local ledger_temporary

  trap - ERR
  if [[ ! "$failure_status" =~ ^[0-9]+$ ]] || [[ "$failure_status" -eq 0 ]]; then
    failure_status=1
  fi
  set +e
  synthetic_demo_terminate_started "$@"
  cleanup_status=$?
  if [[ -f "$ledger_path" && ! -L "$ledger_path" ]]; then
    rm -f -- "$ledger_path"
  fi
  if [[ "$ledger_path" == */* ]]; then
    ledger_directory="${ledger_path%/*}"
  else
    ledger_directory="."
  fi
  ledger_name="${ledger_path##*/}"
  ledger_temporary="$ledger_directory/.$ledger_name.tmp"
  if [[ -f "$ledger_temporary" && ! -L "$ledger_temporary" ]]; then
    rm -f -- "$ledger_temporary"
  fi
  set -e
  if [[ "$cleanup_status" -ne 0 ]]; then
    echo "failed-start cleanup could not prove every child exited" >&2
  fi
  exit "$failure_status"
}
