#!/usr/bin/env bash
# Compatibility interface: OUTPUT INPUT...
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
    echo "usage: $0 OUTPUT INPUT..."
    exit 0
fi
if [[ $# -lt 2 ]]; then
    echo "usage: $0 OUTPUT INPUT..." >&2
    exit 2
fi
output=$1
shift
exec python3 "$(dirname "$0")/merge_mg_pythia_delphes_output.py" "$@" -o "$output"
