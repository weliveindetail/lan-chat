# Web search for LAN chat

## Goal

Let the local model use the internet when a user asks for it, without giving the chat server itself an internet route.

## Decision

Two read-only tools, `web_search` and `fetch_url`. The tool loop runs in `server.py`, and internet access exists only in a separate helper, `lan_chat_web.py`, reached over a Unix socket. Tavily is the search provider.

Each chat has a checkbox that defaults to off. There is still no password, so anyone on the LAN can use web search and spend Tavily credits. The rules are listed in [README.md](../README.md#web-search).

## What was measured

- **Tool calling:** `qwen3.6:35b` on Ollama 0.40.4 sends each tool call whole in one streaming delta, followed by `finish_reason: tool_calls` and `[DONE]`. `arguments` is a JSON string. The loop also joins fragmented calls, in case another version splits them.
- **Second round:** Ollama accepts `tool` messages with `tool_call_id` through `/v1/chat/completions`, and the model answered from them.
- **Context size:** `/api/ps` reports a `context_length` of 262144, the model's maximum, and the Ollama unit sets no override I could see. Page text is unlikely to overflow it: the worst case is 8 pages of 20,000 characters per reply.
- **Tavily:** the current docs give `POST https://api.tavily.com/search` with `Authorization: Bearer <key>`, returning `results[]` with `title`, `url` and `content`.

## Results

- **Real model:** with a fake Tavily and a fake site on loopback, Qwen searched, read the page and answered from it. The page contained an injected instruction to fetch another URL, and the model ignored it. That is one run and proves little, so the refusal itself is covered by tests with a scripted model.
- **Browser:** headless Chromium against the real `server.py` with a scripted model, covering the checkbox, web steps, reload, New chat, Stop, the unavailable state and phone width, with no CSP errors.

## Still untested

- A live Tavily call and the real key file
- Both systemd units, including `LoadCredential`, `IPAddressDeny`, the DNS stub exception and the server connecting to the socket under `ProtectSystem=strict`
- Fetching real websites over real TLS. Tests use plain HTTP on loopback, so certificate checks and TLS pinning have not run against a real site.
- Firefox, Safari and real phones with the new UI
- Whether a Stop during a page read ends the helper's request early. It doesn't: the helper finishes or hits its 20 second limit.

## Alternatives ruled out

- **Search on every message:** sends every prompt to the provider.
- **MCP:** Ollama wouldn't run the tools, so the loop would still be needed, with a protocol on top.
- **Opening the server's own firewall rules:** the server holds the TLS key and takes LAN traffic, so it stays without an internet route.
