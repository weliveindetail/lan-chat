#!/usr/bin/env bash
set -xe

cd "$(dirname "$0")"

(cd frontend && npm ci && npm run build)

sudo cp -r server.py lan_chat_web.py static /opt/lan-chat/
sudo chown -R root:root /opt/lan-chat
sudo chmod -R a+rX /opt/lan-chat

sudo cp lan-chat.service lan-chat-web.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl restart lan-chat-web lan-chat
