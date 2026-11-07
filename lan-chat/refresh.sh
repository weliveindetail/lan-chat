#!/usr/bin/env bash

cd frontend && npm ci && npm run build
cd ..

sudo cp -r server.py static /opt/lan-chat/
sudo chown -R root:root /opt/lan-chat
sudo systemctl restart lan-chat
