# LAN Chat

A chat page for the local `qwen3.6:35b` model in Ollama, served over HTTPS with a self-signed certificate. There is no password, so anyone who can reach the port can use it. The server uses only the Python standard library. Ollama stays on loopback, and this server is the only thing exposed to the LAN.

The chat page is an [assistant-ui](https://github.com/assistant-ui/assistant-ui) React app, built once into plain files in `lan-chat/static/`. No Node process runs at runtime. See [Frontend](#frontend).

## Prerequisites

- **System:** Linux with systemd, and `sudo` access. The scripts also use `openssl`.
- **Python:** 3.10 or newer at `/usr/bin/python3`, with no extra packages.
- **Node:** 22 or newer with `npm`, only for building the page.
- **Ollama:** running on `127.0.0.1:11434`, and not exposed to the LAN, with the model pulled by `ollama pull qwen3.6:35b`. The model is about 23 GB, so the machine needs enough memory to run it.
- **Tavily API key:** only for web search. Without one, `lan-chat-web` can't start and `refresh.sh` reports an error at its restart step. Normal chat should still work, with web search off.
- **Firewall:** if one is active, allow ports 8080 and 8081 from your LAN, for example `sudo ufw allow from "$LAN_CHAT_SUBNET" to any port 8080,8081 proto tcp`.

## Install

Set this machine's address and your LAN range, using your own values:

```sh
export LAN_CHAT_IP=192.168.1.50
export LAN_CHAT_SUBNET=192.168.1.0/24
# First save your Tavily key to /tmp/tavily
./tavily.sh
./install.sh
./refresh.sh
systemctl status lan-chat lan-chat-web
journalctl -u lan-chat -u lan-chat-web -n 50
sudo systemctl stop lan-chat lan-chat-web
sudo systemctl enable lan-chat-web lan-chat
```

This takes effect at the next boot and doesn't start or stop anything now. To undo it, use `disable` in place of `enable`.

`https://$LAN_CHAT_IP:8080` warns that the certificate isn't trusted. Check the SHA-256 fingerprint is the following and accept:
```sh
C7:8A:CE:A7:12:81:C8:7C:7C:F8:4F:68:50:58:9D:E6:CC:EA:00:D7:D6:94:D1:96:F8:93:72:60:37:52:45:B2
```

`http://$LAN_CHAT_IP:8081` works without certificate, because it's not encrypted. Anyone on the LAN could read or change traffic.

Run `refresh.sh` to update after a change. It rebuilds the page, installs the code and unit files, and restarts both services, which ends any running chats.

The certificate lasts one year and is tied to `LAN_CHAT_IP`. To replace it, delete `/etc/lan-chat/key.pem`, then run `install.sh` and `refresh.sh` again. Your friend then sees the warning again, with a new fingerprint.

## Run the tests and try it locally

```sh
cd lan-chat
python3 -m unittest test_server test_web
python3 server.py
```

Without certificate options the server uses plain HTTP, at `http://127.0.0.1:8080` by default. Use this only on loopback.

## HTTP API

No route requires authentication.

### `GET /<file>`

Serves files found in `lan-chat/static/` when the server starts, and `/` serves `index.html`. Any other route or method returns `404`.

### `GET /api/web-status`

Says whether web search can be used. `message` explains why not, and the UI shows it:

```json
{ "available": true, "provider": "Tavily", "message": null }
```

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
- `web` is optional and defaults to `false`. `true` offers the model the web tools, and requires streaming.
- All other fields are ignored, including `model` and `max_tokens`. The server sets the model and the output limit. Clients can't send `tool` or `system` messages.
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
- With `web`, each search or page read also sends status events. Each one replaces the earlier event with the same `id`. `state` is `running`, `done`, `failed` or `blocked`. Search snippets and page text go only to the model.

  ```text
  data: {"web": {"id": 1, "tool": "web_search", "target": "the query", "state": "done", "detail": null, "results": [{"title": "A page", "url": "https://example.com/"}]}}
  ```

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
| 503 | `web` was requested but web search is unavailable |

## Web search

Each chat has a **Web search** checkbox, off by default and off again after **New chat**. Without it the model gets no tools and the helper isn't contacted.

```text
Browser --> server.py --> Ollama
               |  tool loop, no internet route
          Unix socket
               v
        lan_chat_web.py --> api.tavily.com, public websites
```

The model gets two tools: `web_search`, which sends the query to [Tavily](https://docs.tavily.com/documentation/api-reference/endpoint/search), and `fetch_url`, which reads a page as text. The browser never contacts these sites, so the CSP is unchanged.

### Rules

| Rule | Where it's enforced |
|---|---|
| `fetch_url` only takes URLs from this reply's search results or the user's messages, so an injected page can't send chat text to its own server. URLs found in a page can't be followed. | `lan-chat/server.py` |
| At most 5 tool rounds and 8 tool calls per reply. The last round offers no tools, which forces an answer. | `lan-chat/server.py` |
| Public addresses only, for every answer and every redirect hop, using the checked address for the connection so DNS can't change it afterwards | `lan-chat/lan_chat_web.py` |
| GET only on ports 80 and 443, no cookies, no compression, no user names in URLs | `lan-chat/lan_chat_web.py` |
| 20 seconds and 2 MiB per page, 5 redirects, 20,000 characters of text, text formats only, 8 search results, 4 requests at once | `lan-chat/lan_chat_web.py` |
| No route to loopback (except the DNS stub), the LAN or Tailscale | `IPAddressDeny` in `lan-chat/lan-chat-web.service` |

Tool results are labelled as untrusted for the model, but that label is only a request. The rules above are what limit the damage. Every step is listed in the reply, including refused ones, and refused URLs are never shown as links.

### Tavily key

The key lives in a root-only file. systemd reads it as root and passes a private copy to the helper through `$CREDENTIALS_DIRECTORY`. The `lan-chat` account can't read it, and it's kept apart from `/etc/lan-chat/`, which that account must be able to read.

To install it, save the key to `/tmp/tavily` and run `tavily.sh`, which copies it to `/etc/lan-chat-web/tavily-api-key` with mode `600` and then shreds the temporary file.

Outside systemd, use a file that only you can read:

```sh
cd lan-chat
python3 lan_chat_web.py --socket /tmp/web.sock --search-provider tavily --search-key-file ~/tavily-key
python3 server.py --web-socket /tmp/web.sock
```

The helper refuses to start if the key file is readable by group or others, or doesn't hold a single line of text. The key is sent only in the `Authorization` header to `https://api.tavily.com/search`, and redirects from that address are not followed.

Without `--search-provider` or a running helper, both tools are off, and the checkbox is disabled with the reason next to it. Normal chat keeps working. A missing key file stops `lan-chat-web` from starting, which the page reports as the helper not running.

To add another search backend, subclass `SearchProvider` in `lan-chat/lan_chat_web.py`, add it to `PROVIDERS`, and change `LoadCredential` and `--search-provider` in the unit. A backend on the LAN or loopback, such as SearXNG, would also need exceptions to the address checks and `IPAddressDeny`.

## Frontend

Build with Node 22 or newer. `npm ci` uses the committed lockfile, and `lan-chat/frontend/.npmrc` turns off install scripts and pins new dependencies to exact versions.

```sh
cd lan-chat/frontend
npm ci
npm run build
```

The build replaces everything in `lan-chat/static/`. The server lists its files at startup, so restart it afterwards. To update a running service, use `refresh.sh` from [Install](#install).

- **Requests:** the page calls `POST /api/chat` on its own origin through a custom assistant-ui adapter in `lan-chat/frontend/src/chatAdapter.js`, and sends the full history each time.
- **History:** it is kept in `sessionStorage`, along with the web checkbox and each reply's web steps, so they survive a reload and are gone when the tab closes. The server stores no chats.
- **Model output:** HTML in replies is shown as text, unsafe link schemes are removed, and images are shown as links so they never load.
- **Assistant Cloud:** it is not used. `@assistant-ui/react` depends on the `assistant-cloud` package, so its code and default URL are in the bundle, but it only runs if a `cloud` option is passed. The policy below would block it anyway.

The server sends this policy with every response. The current UI works with it unchanged, so any other UI must also avoid CDNs, inline scripts and inline styles, or `SECURITY_HEADERS` in `lan-chat/server.py` must change.

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
| `--web-socket` | none | The helper's Unix socket. Web search stays off without it. |
| `--upstream-timeout` | `300` | Seconds to wait for each read from Ollama |
| `--max-active-chats` | `2` | |
| `--upstream-host`, `--upstream-port` | `127.0.0.1`, `11434` | |
| `--static-dir` | `lan-chat/static/` | |

Helper options, for `lan-chat/lan_chat_web.py`:

| Option | Default | Notes |
|---|---|---|
| `--socket` | required | Created with mode `660` |
| `--search-provider` | none | `tavily`. Web search stays off without it. |
| `--search-key-file` | `$CREDENTIALS_DIRECTORY/tavily-api-key` | For running outside systemd |

## Known limits

- **Plain HTTP:** port 8081 has no encryption or server identity check. Remove `--http-port` from the unit to turn it off.
- **Self-signed certificate:** HTTPS traffic is encrypted, but the browser can't confirm the server's identity. Someone on the LAN could impersonate it unless the fingerprint is compared.
- **Address changes:** if this machine's LAN address changes, the service can't bind and retries every 30 seconds. Reserve the address on the router. If it does change, set the new `LAN_CHAT_IP`, delete `/etc/lan-chat/key.pem`, run `install.sh` and `refresh.sh`, and update the firewall rule.
- **LAN range:** only clients in `LAN_CHAT_SUBNET` can connect, so devices elsewhere, such as on a guest network, can't reach the page.
- **Shared model:** chats compete with your own Ollama use.
- **Prompt injection:** the rules stop a page making the model read unlisted URLs, but a page can still influence the answer. A search query could also carry chat text to Tavily, and a reply could contain a link holding chat text.
- **Follow-up questions:** only text goes back to the server, so fetched pages are gone on the next turn, and URLs from earlier search results can't be fetched again unless they're searched for again.
- **Page reading:** no JavaScript or PDFs, and pages are read with a simple HTML parser, so hidden text reaches the model and some sites will read poorly.
- **Busy slots:** a chat holds one of the 2 slots for its whole tool loop.
