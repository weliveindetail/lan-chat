#!/usr/bin/env bash
set -xe

# Before running this, store your tvly-••• API key to /tmp/tavily
stat -c '%U %a' /tmp/tavily
sudo install -d -m 700 -o root -g root /etc/lan-chat-web
sudo install -m 600 -o root -g root /tmp/tavily /etc/lan-chat-web/tavily-api-key
sudo stat -c '%U %a %s' /etc/lan-chat-web/tavily-api-key
shred -u /tmp/tavily
