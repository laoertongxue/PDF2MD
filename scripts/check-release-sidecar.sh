#!/usr/bin/env -S -i PATH=/usr/bin:/bin HOME=/var/empty TMPDIR=/tmp LC_ALL=C LANG=C /bin/sh
set -eu

case "$0" in
  */*) script_dir=${0%/*} ;;
  *) script_dir=. ;;
esac

exec /usr/bin/python3 -I -S -B "$script_dir/check_release_sidecar.py" "$@"
