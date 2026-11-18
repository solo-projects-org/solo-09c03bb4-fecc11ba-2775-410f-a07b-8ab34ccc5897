#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$(dirname "${DATABASE_PATH:-/data/carryover.sqlite3}")"
    chown -R nobody:nogroup "$(dirname "${DATABASE_PATH:-/data/carryover.sqlite3}")"
    exec gosu nobody "$@"
fi

exec "$@"
