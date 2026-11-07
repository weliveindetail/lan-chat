# LAN Chat

A chat page for the local `qwen3.6:35b` model in Ollama, served over HTTPS with a self-signed certificate. There is no password, so anyone who can reach the port can use it. The server uses only the Python standard library. Ollama stays on loopback, and this server is the only thing exposed to the LAN.

The chat page is an [assistant-ui](https://github.com/assistant-ui/assistant-ui) React app, built once into plain files in `static/`. No Node process runs at runtime. See [Frontend](#frontend).

## Files

| Path | Purpose |
|---|---|
| `server.py` | HTTPS, static files, chat proxy |
| `frontend/` | UI source (Vite, React, assistant-ui) |
| `static/` | UI build output, served as is. Don't edit it by hand. |
| `test_server.py` | Tests against a fake model server |
| `lan-chat.service` | Draft systemd unit, not installed |
| `install.sh` | Creates the account and certificate, and installs the files and unit |

## Run the tests and try it locally

```sh
python3 -m unittest test_server
python3 server.py
```

Without certificate options the server uses plain HTTP, at `http://127.0.0.1:8080` by default. Use this only on loopback.

## HTTP API

No route requires authentication.

### `GET /<file>`

Serves files found in `static/` when the server starts, and `/` serves `index.html`. Any other route or method returns `404`.

### `POST /api/chat`

Send `Content-Type: application/json`:

```json
{
  "messages": [
    { "role": "user", "content": "Hello" },
    { "role": "assistant", "content": "Hi" },
    { "role": "user", "content": "How are you?" }
  ],
  "stream": true
}
```

- `messages` is required: 1 to 200 items, each with a `role` of `user` or `assistant` and a string `content`.
- `stream` is optional and defaults to `true`.
- All other fields are ignored, including `model` and `max_tokens`. The server sets the model and the output limit.
- A browser `Origin` header, if sent, must match `Host`.

**Streaming response** (`text/event-stream`):

```text
: thinking

data: {"choices": [{"delta": {"content": "Hel"}, "finish_reason": null}]}

data: {"choices": [{"delta": {"content": "lo"}, "finish_reason": null}]}

data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}

data: [DONE]

```

- `: thinking` is an SSE comment sent at most once a second while the model reasons. Reasoning text is never sent. Use it for a progress indicator or ignore it.
- `finish_reason` of `length` means the output limit was reached. Reasoning counts towards that limit, so a reply can be cut off or even empty.
- A failure during streaming arrives as `data: {"error": "..."}` and the stream then ends. If it ends with neither that nor `[DONE]`, treat the reply as incomplete.
- To stop a reply, close the connection, for example with `AbortController` in `fetch`. The server then closes its connection to Ollama.

**Non-streaming response** (`"stream": false`):

```json
{ "choices": [{ "message": { "role": "assistant", "content": "Hi" }, "finish_reason": "stop" }] }
```

**Errors** return `{"error": "message"}`:

| Status | Meaning |
|---|---|
| 400 | Invalid JSON or request fields |
| 403 | Cross-origin request |
| 404 | Unknown route or method |
| 411 / 413 | Content-Length missing / body over 1 MiB |
| 415 | Content-Type is not JSON |
| 429 | 2 chats are already running |
| 502 | Ollama unavailable or returned an error |

## Frontend

Build with Node 22 or newer. `npm ci` uses the committed lockfile, and `frontend/.npmrc` turns off install scripts and pins new dependencies to exact versions.

```sh
cd frontend
npm ci
npm run build
```

The build replaces everything in `static/`. The server lists its files at startup, so restart it afterwards. A running service also needs the copy steps in [Start and stop](#start-and-stop).

- **Requests:** the page calls `POST /api/chat` on its own origin through a custom assistant-ui adapter in `src/chatAdapter.js`, and sends the full history each time.
- **History:** it is kept in `sessionStorage`, so it survives a reload and is gone when the tab closes. The server stores no chats.
- **Model output:** HTML in replies is shown as text, unsafe link schemes are removed, and images are shown as links so they never load.
- **Assistant Cloud:** it is not used. `@assistant-ui/react` depends on the `assistant-cloud` package, so its code and default URL are in the bundle, but it only runs if a `cloud` option is passed. The policy below would block it anyway.

The server sends this policy with every response. The current UI works with it unchanged, so any other UI must also avoid CDNs, inline scripts and inline styles, or `SECURITY_HEADERS` in `server.py` must change.

```text
default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
```

A different folder of static files can be passed with `--static-dir`.

## Server options

| Option | Default | Notes |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `8080` | |
| `--tls-cert`, `--tls-key` | none | Turn on HTTPS and must be used together. The key must not be accessible by group or others. |
| `--http-port` | none | Also serves plain HTTP on this port, alongside HTTPS. Requires `--tls-cert`. The chat limit covers both ports. |
| `--max-tokens` | `8192` | Output limit, including reasoning |
| `--no-think` | off | Turns off model reasoning: faster replies, possibly weaker answers |
| `--upstream-timeout` | `300` | Seconds to wait for each read from Ollama |
| `--max-active-chats` | `2` | |
| `--upstream-host`, `--upstream-port` | `127.0.0.1`, `11434` | |
| `--static-dir` | `static/` | |

## Start and stop

```sh
npm ci && npm run build
./install.sh
sudo systemctl start lan-chat
systemctl status lan-chat
journalctl -u lan-chat -n 50
sudo systemctl stop lan-chat
```

Your friend opens `https://192.168.50.142:8080`, including the `https://`, because plain `http://` on this port doesn't load. The browser warns that the certificate isn't trusted. Your friend should compare its SHA-256 fingerprint with yours, then accept the warning. To show yours:

```sh
sudo openssl x509 -in /etc/lan-chat/cert.pem -noout -fingerprint -sha256
```

`http://192.168.50.142:8081` also works, with no certificate warning. Chats on this port are not encrypted, so anyone on the LAN could read or change them. The firewall must allow port 8081 as well.

To update after a change, repeat the `cp` and `chown` lines from `install.sh`, then restart the service.

The certificate lasts one year and is tied to `192.168.50.142`. To replace it, delete `/etc/lan-chat/key.pem`, run `install.sh` again and restart the service. Your friend then sees the warning again, with a new fingerprint.

## Known limits

- **No password:** anyone who can reach the port can use the model, limited only by any firewall rules and the 2 active chats.
- **Plain HTTP:** port 8081 has no encryption or server identity check. Remove `--http-port` from the unit to turn it off.
- **Self-signed certificate:** HTTPS traffic is encrypted, but the browser can't confirm the server's identity. Someone on the LAN could impersonate it unless the fingerprint is compared.
- **Address changes:** this machine's LAN address comes from DHCP (`ipv4.method` is `auto`), so `192.168.50.142` can change. If it does, the service can't bind and retries every 30 seconds. Reserve the address on the router, or update `lan-chat.service`, the firewall rule and the certificate.
- **Friend's address:** `<FRIEND_IP>` may also change if their laptop uses DHCP.
- **LAN range:** `IPAddressAllow` in the unit assumes `192.168.50.0/24`.
- **Shared model:** chats compete with your own Ollama use.
