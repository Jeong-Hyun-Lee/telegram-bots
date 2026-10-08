#!/usr/bin/env bash
# Ubuntu 서버에서 실행: bash ~/telegram-bots/deploy/setup.sh nara [다른봇 ...]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -eq 0 ]; then
    echo "사용법: bash deploy/setup.sh <봇이름> [봇이름 ...]"
    exit 1
fi

sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp deploy/telegram-bot@.service /etc/systemd/system/
sudo systemctl daemon-reload
for bot in "$@"; do
    if [ ! -f "bots/$bot/.env" ]; then
        echo "bots/$bot/.env 없음: .env.example 복사 후 값 입력하고 다시 실행"
        exit 1
    fi
    chmod 600 "bots/$bot/.env"
    sudo systemctl enable --now "telegram-bot@$bot"
    sudo systemctl status "telegram-bot@$bot" --no-pager
done
