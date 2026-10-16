#!/usr/bin/env bash
# Запуск контент-завода
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "Создаю venv..."
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet fastapi uvicorn python-multipart
fi
echo "Запускаю дашборд: http://127.0.0.1:8000"
exec ./.venv/bin/python server.py
