#!/usr/bin/env bash
# Запуск сервера на Oracle Always Free VPS (первый раз — вручную, потом через systemd)
set -e
cd "$(dirname "$0")/.."
exec python3 server.py
