#!/usr/bin/env bash
set -x

sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin lan-chat
# In the lan-chat group so it can share its socket, with no access to /etc/lan-chat
sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --gid lan-chat lan-chat-web

sudo install -d -m 755 /opt/lan-chat
sudo cp -r server.py lan_chat_web.py static /opt/lan-chat/
sudo chown -R root:root /opt/lan-chat
sudo chmod -R a+rX /opt/lan-chat

sudo install -d -m 700 -o lan-chat -g lan-chat /etc/lan-chat
# Root only, for the Tavily key. systemd passes it to the helper as a credential.
sudo install -d -m 700 -o root -g root /etc/lan-chat-web

# Keeps an existing certificate, so rerunning doesn't change the fingerprint
sudo sh -c 'umask 077; [ -f /etc/lan-chat/key.pem ] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 \
  -keyout /etc/lan-chat/key.pem -out /etc/lan-chat/cert.pem -subj "/CN=lan-chat" \
  -addext "subjectAltName=IP:192.168.50.142" -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=serverAuth"'
sudo chown lan-chat:lan-chat /etc/lan-chat/key.pem /etc/lan-chat/cert.pem

sudo cp lan-chat.service lan-chat-web.service /etc/systemd/system/
sudo systemctl daemon-reload
