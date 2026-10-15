#!/bin/sh
# Native AGY logs default to second-resolution names. Concurrent CLI processes
# can overwrite the same file. Give every process a private, distinct log file.
set -eu
umask 077
agy_diag_dir="${HOME}/.gemini/antigravity-cli/log"
mkdir -p "$agy_diag_dir"
agy_diag_file="$agy_diag_dir/cli-diag-$(date -u +%Y%m%d_%H%M%S)-$$.log"
exec agy \
  --vmodule=http_helpers=2,conversation_manager=2,printmode=2,latency_breakdown=2 \
  --log-file "$agy_diag_file" \
  "$@"
