#!/usr/bin/env bash
set -xe

# Without this file the service would only listen on loopback
if [ ! -f /etc/systemd/system/lan-chat.service.d/lan.conf ]; then
  echo "Set LAN_CHAT_IP and LAN_CHAT_SUBNET and run install.sh first" >&2
  exit 1
fi

cd "$(dirname "$0")/lan-chat"

(cd frontend && npm ci && npm run build)

sudo cp -r server.py lan_chat_web.py static /opt/lan-chat/
sudo chown -R root:root /opt/lan-chat
sudo chmod -R a+rX /opt/lan-chat

sudo cp lan-chat.service lan-chat-web.service /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl restart lan-chat-web lan-chat
