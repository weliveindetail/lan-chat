#!/usr/bin/env bash
set -xe

: "${LAN_CHAT_IP:?Set LAN_CHAT_IP to the LAN address of this machine, for example 192.168.1.50}"
: "${LAN_CHAT_SUBNET:?Set LAN_CHAT_SUBNET to the LAN range, for example 192.168.1.0/24}"
# These values reach the certificate and the unit, so only accept real addresses
python3 -I -c 'import ipaddress, sys
ip, subnet = ipaddress.ip_address(sys.argv[1]), ipaddress.ip_network(sys.argv[2])
sys.exit(None if ip in subnet else f"{ip} is not in {subnet}")' "$LAN_CHAT_IP" "$LAN_CHAT_SUBNET"

id -u lan-chat >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin lan-chat
# In the lan-chat group so it can share its socket, with no access to /etc/lan-chat
id -u lan-chat-web >/dev/null 2>&1 || sudo useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --gid lan-chat lan-chat-web

# refresh.sh installs the code and units
sudo install -d -m 755 -o root -g root /opt/lan-chat

sudo install -d -m 700 -o lan-chat -g lan-chat /etc/lan-chat
# Root only, for the Tavily key. systemd passes it to the helper as a credential.
sudo install -d -m 700 -o root -g root /etc/lan-chat-web

# Keeps an existing certificate, so rerunning doesn't change the fingerprint
sudo sh -c 'umask 077; [ -f /etc/lan-chat/key.pem ] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 \
  -keyout /etc/lan-chat/key.pem -out /etc/lan-chat/cert.pem -subj "/CN=lan-chat" \
  -addext "subjectAltName=IP:$1" -addext "basicConstraints=critical,CA:FALSE" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=serverAuth"' sh "$LAN_CHAT_IP"
sudo chown lan-chat:lan-chat /etc/lan-chat/key.pem /etc/lan-chat/cert.pem

# Kept outside the unit file, so refresh.sh can replace that without losing these
sudo install -d -m 755 -o root -g root /etc/systemd/system/lan-chat.service.d
printf '[Service]\nEnvironment=LAN_CHAT_IP=%s\nIPAddressAllow=%s\n' "$LAN_CHAT_IP" "$LAN_CHAT_SUBNET" |
  sudo tee /etc/systemd/system/lan-chat.service.d/lan.conf >/dev/null
