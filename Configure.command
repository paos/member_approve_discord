#!/bin/zsh
set -eu
cd -- "${0:A:h}"
read -r 'guild_id?Discord test server ID: '
if [[ -z "$guild_id" || "$guild_id" == *[^0-9]* ]]; then
    printf 'Enter a numeric Discord server ID.\n' >&2
    exit 1
fi
.venv/bin/python -m mensa.cli configure --guild "$guild_id"
printf '\nConfiguration saved. Open the installation URL printed above.\n'
read -r '?Press Enter to close.'
