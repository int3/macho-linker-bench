#!/bin/sh
# Portable entry point; Python handles the verified streaming pipelines.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/fetch.py" "$@"
