#!/usr/bin/env python3
import argparse
import http.client
import ipaddress
import json
import os
import socket
import socketserver
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

MAX_REQUEST_BYTES = 64 * 1024
MAX_URL_CHARS = 2048
MAX_QUERY_CHARS = 400
MAX_RESULTS = 8
MAX_TITLE_CHARS = 300
MAX_SNIPPET_CHARS = 1000
USER_AGENT = "lan-chat-web/1.0"
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object"}
BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
              "figcaption", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
              "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"}

# These embed another address, so is_global can't tell where they really lead
TUNNELLED_V6 = tuple(ipaddress.ip_network(net) for net in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))


class WebError(Exception):
    # The message is shown to the model and the user, so it must hold no secrets
    pass


def is_public_ip(text):
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped:
            # Python 3.12.3 doesn't look inside mapped addresses
            ip = ip.ipv4_mapped
        elif any(ip in net for net in TUNNELLED_V6):
            return False
    # is_global alone allows IPv4 multicast
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_link_local
                                 or ip.is_loopback or ip.is_unspecified)


def system_resolve(host, port):
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [answer[4][0] for answer in answers]


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def remaining(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise WebError("The request timed out")
        return left

    def expired(self):
        return time.monotonic() >= self.end


class PinnedConnection(http.client.HTTPConnection):
    # Connects to an address that was already checked, so DNS can't change between check and use
    def __init__(self, host, port, ip, deadline, tls_context=None):
        super().__init__(host, port, timeout=deadline.remaining())
        self.ip = ip
        self.deadline = deadline
        self.tls_context = tls_context
        self.default_port = 443 if tls_context else 80
        self.watchdog = None
        self.timer = None

    def connect(self):
        raw = socket.create_connection((self.ip, self.port), self.timeout)
        # Socket timeouts apply per read, so a slow drip needs this to enforce the total limit
        self.watchdog = raw.dup()
        self.timer = threading.Timer(self.deadline.remaining(), self.cut_off)
        self.timer.daemon = True
        self.timer.start()
        self.sock = (self.tls_context.wrap_socket(raw, server_hostname=self.host)
                     if self.tls_context else raw)

    def cut_off(self):
        try:
            self.watchdog.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        if self.timer:
            self.timer.cancel()
        if self.watchdog:
            self.watchdog.close()
        self.close()


class TextExtractor(HTMLParser):
    def __init__(self, max_chars):
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.parts = []
        self.size = 0
        self.title = []
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIPPED_TAGS:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIPPED_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.in_title:
            self.title.append(data)
        elif self.size <= self.max_chars * 2:
            # Collects past the limit because whitespace is removed later
            self.parts.append(data)
            self.size += len(data)


def tidy_text(text):
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def page_text(data, charset, is_html, max_chars):
    try:
        decoded = data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        decoded = data.decode("utf-8", errors="replace")
    title = ""
    if is_html:
        extractor = TextExtractor(max_chars)
        extractor.feed(decoded)
        extractor.close()
        decoded = "".join(extractor.parts)
        title = tidy_text("".join(extractor.title))
    text = tidy_text(decoded)
    return title[:MAX_TITLE_CHARS], text[:max_chars], len(text) > max_chars


def is_text_type(media_type):
    return (not media_type or media_type.startswith("text/")
            or media_type in ("application/json", "application/xml", "application/xhtml+xml")
            or media_type.endswith(("+json", "+xml")))


@dataclass
class Fetcher:
    resolve: object = system_resolve
    ip_allowed: object = is_public_ip
    ports: tuple = (80, 443)
    api_schemes: tuple = ("https",)
    timeout: float = 20.0
    max_redirects: int = 5
    max_bytes: int = 2 * 1024 * 1024
    max_text_chars: int = 20000
    tls_context: ssl.SSLContext | None = None

    def __post_init__(self):
        self.tls_context = self.tls_context or ssl.create_default_context()

    def target(self, url, schemes):
        if len(url) > MAX_URL_CHARS:
            raise WebError("The URL is too long")
        if any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise WebError("The URL has invalid characters")
        try:
            parts = urlsplit(url)
            port = parts.port
            host = (parts.hostname or "").encode("idna").decode()
        except (ValueError, UnicodeError):
            raise WebError("The URL is not valid") from None
        if parts.scheme.lower() not in schemes:
            raise WebError("Only http and https URLs are allowed")
        if parts.username is not None or parts.password is not None:
            raise WebError("URLs with a user name or password are not allowed")
        if not host:
            raise WebError("The URL has no host")
        port = port or (443 if parts.scheme.lower() == "https" else 80)
        if port not in self.ports:
            raise WebError("Only ports 80 and 443 are allowed")
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
        query = quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
        return parts.scheme.lower(), host, port, path + (f"?{query}" if query else "")

    def checked_ips(self, host, port):
        try:
            ips = self.resolve(host, port)
        except (OSError, UnicodeError):
            raise WebError("The host name could not be resolved") from None
        # All answers must pass, so a mix of public and private addresses is refused
        if not ips or not all(self.ip_allowed(ip) for ip in ips):
            raise WebError("That address is not on the public internet")
        return ips

    def connect(self, url, schemes, deadline):
        scheme, host, port, path = self.target(url, schemes)
        context = self.tls_context if scheme == "https" else None
        failure = None
        for ip in self.checked_ips(host, port):
            conn = PinnedConnection(host, port, ip, deadline, context)
            try:
                conn.connect()
                return conn, path
            except WebError:
                conn.finish()
                raise
            except ssl.SSLCertVerificationError:
                conn.finish()
                raise WebError("The site's certificate could not be verified") from None
            except OSError as error:
                conn.finish()
                failure = error
        raise WebError("Could not connect to the site") from failure

    def read_body(self, response, deadline):
        chunks, size = [], 0
        while size < self.max_bytes:
            # read1 returns after one socket read, so the watchdog stays effective
            chunk = response.read1(min(65536, self.max_bytes - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if deadline.expired():
            raise WebError("The request timed out")
        return b"".join(chunks), size >= self.max_bytes

    def fetch_page(self, url):
        deadline = Deadline(self.timeout)
        current = url
        try:
            for _ in range(self.max_redirects + 1):
                # Every hop is checked again and gets its own pinned connection
                conn, path = self.connect(current, ("http", "https"), deadline)
                try:
                    # No cookies are stored or sent, and identity keeps out compression bombs
                    conn.request("GET", path, headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                        "Accept-Encoding": "identity",
                    })
                    response = conn.getresponse()
                    if response.status in REDIRECT_STATUSES:
                        location = response.getheader("Location")
                        if not location:
                            raise WebError("The site sent a redirect with no address")
                        current = urljoin(current, location)
                        continue
                    if response.status != 200:
                        raise WebError(f"The site returned HTTP {response.status}")
                    return self.read_page(current, response, deadline)
                finally:
                    conn.finish()
        except (OSError, http.client.HTTPException, ValueError):
            if deadline.expired():
                raise WebError("The request timed out") from None
            raise WebError("The site could not be read") from None
        raise WebError("The site redirected too many times")

    def read_page(self, url, response, deadline):
        encoding = (response.getheader("Content-Encoding") or "identity").lower()
        if encoding != "identity":
            raise WebError("The site sent a compressed page despite being asked not to")
        message = response.headers
        media_type = message.get_content_type() if message.get("Content-Type") else ""
        if not is_text_type(media_type):
            raise WebError(f"This type of file can't be read: {media_type[:60]}")
        data, cut_off = self.read_body(response, deadline)
        if b"\0" in data[:1024] and message.get_content_charset() is None:
            raise WebError("This looks like a binary file, which can't be read")
        is_html = media_type in ("text/html", "application/xhtml+xml")
        title, text, cut_text = page_text(data, message.get_content_charset(), is_html,
                                          self.max_text_chars)
        return {"url": url, "title": title, "text": text, "truncated": cut_off or cut_text}

    def fetch_json(self, url, headers=None, body=None):
        # For search providers: never redirects, so an API key can't follow to another host
        deadline = Deadline(self.timeout)
        try:
            conn, path = self.connect(url, self.api_schemes, deadline)
            try:
                conn.request("POST" if body is not None else "GET", path,
                             None if body is None else json.dumps(body).encode(),
                             {"User-Agent": USER_AGENT, "Accept": "application/json",
                              "Accept-Encoding": "identity",
                              **({} if body is None else {"Content-Type": "application/json"}),
                              **(headers or {})})
                response = conn.getresponse()
                if response.status != 200:
                    raise WebError(f"The search provider returned HTTP {response.status}")
                data, cut_off = self.read_body(response, deadline)
            finally:
                conn.finish()
            if cut_off:
                raise WebError("The search provider sent too much data")
            return json.loads(data)
        except (OSError, http.client.HTTPException, ValueError):
            raise WebError("The search provider could not be read") from None


class SearchProvider:
    name = ""
    needs_key = True
    # File name inside $CREDENTIALS_DIRECTORY
    credential = ""

    def __init__(self, fetcher, api_key=None):
        self.fetcher = fetcher
        self.api_key = api_key

    def search(self, query, limit):
        # Returns a list of {"title", "url", "snippet"}, using self.fetcher.fetch_json
        raise NotImplementedError


class TavilyProvider(SearchProvider):
    name = "Tavily"
    credential = "tavily-api-key"
    endpoint = "https://api.tavily.com/search"

    def search(self, query, limit):
        reply = self.fetcher.fetch_json(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            # Set explicitly so a change in Tavily's defaults can't add cost or size
            body={"query": query, "max_results": limit, "search_depth": "basic",
                  "include_answer": False, "include_raw_content": False,
                  "include_images": False},
        )
        return [{"title": result.get("title"), "url": result.get("url"),
                 "snippet": result.get("content")} for result in reply["results"]]


PROVIDERS = {"tavily": TavilyProvider}


def clean_results(results):
    clean = []
    for result in results:
        url = result.get("url")
        if not isinstance(url, str) or urlsplit(url).scheme.lower() not in ("http", "https"):
            continue
        clean.append({
            "title": str(result.get("title") or url)[:MAX_TITLE_CHARS],
            "url": url[:MAX_URL_CHARS],
            "snippet": str(result.get("snippet") or "")[:MAX_SNIPPET_CHARS],
        })
    return clean[:MAX_RESULTS]


class WebService:
    def __init__(self, fetcher, provider, max_active=4):
        self.fetcher = fetcher
        self.provider = provider
        self.slots = threading.BoundedSemaphore(max_active)

    def handle(self, request):
        op = request.get("op") if isinstance(request, dict) else None
        if op == "status":
            return {"ok": True, "available": self.provider is not None,
                    "provider": self.provider.name if self.provider else None}
        if op not in ("search", "fetch"):
            return {"ok": False, "error": "Unknown operation"}
        if self.provider is None:
            return {"ok": False, "error": "No search provider is configured"}
        if not self.slots.acquire(blocking=False):
            return {"ok": False, "error": "Too many web requests at once"}
        try:
            return {"ok": True, **(self.search(request) if op == "search" else self.fetch(request))}
        except WebError as error:
            return {"ok": False, "error": str(error)}
        finally:
            self.slots.release()

    def search(self, request):
        query = request.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise WebError(f"The query must be 1 to {MAX_QUERY_CHARS} characters")
        try:
            return {"results": clean_results(self.provider.search(query.strip(), MAX_RESULTS))}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise WebError("The search provider sent an unexpected response") from None

    def fetch(self, request):
        url = request.get("url")
        if not isinstance(url, str):
            raise WebError("The URL must be a string")
        return self.fetcher.fetch_page(url)


class RequestHandler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self):
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(line) > MAX_REQUEST_BYTES:
                reply = {"ok": False, "error": "Request too large"}
            else:
                reply = self.server.service.handle(json.loads(line))
        except (ValueError, RecursionError):
            reply = {"ok": False, "error": "Request is not valid JSON"}
        except OSError:
            return
        except Exception as error:
            # Stay up and keep details private: errors could quote a URL or a query
            print(f"request failed: {type(error).__name__}", file=sys.stderr, flush=True)
            reply = {"ok": False, "error": "The web helper hit an internal error"}
        try:
            self.wfile.write(json.dumps(reply, ensure_ascii=False).encode() + b"\n")
        except OSError:
            pass


class WebServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path, service):
        self.service = service
        path = Path(socket_path)
        if path.is_socket():
            path.unlink()
        super().__init__(str(path), RequestHandler)

    def server_bind(self):
        super().server_bind()
        os.chmod(self.server_address, 0o660)


def load_key(path, check_mode=True):
    # Errors name the file only, so the key can't reach a log
    if check_mode and stat.S_IMODE(Path(path).stat().st_mode) & 0o077:
        raise SystemExit(f"{path} must not be accessible by group or others (use chmod 600)")
    try:
        key = Path(path).read_bytes().decode("ascii").strip()
    except UnicodeDecodeError:
        raise SystemExit(f"{path} does not hold a valid API key") from None
    if not key or any(ord(char) <= 32 or ord(char) == 127 for char in key):
        raise SystemExit(f"{path} does not hold a valid API key")
    return key


def make_provider(name, key_file, fetcher):
    if not name:
        return None
    provider_class = PROVIDERS.get(name)
    if provider_class is None:
        raise SystemExit(f"Unknown search provider: {name}")
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    from_systemd = not key_file and credentials and provider_class.credential
    if from_systemd:
        key_file = Path(credentials) / provider_class.credential
    if provider_class.needs_key and not key_file:
        raise SystemExit(f"{name} needs --search-key-file or a {provider_class.credential} "
                         "systemd credential")
    # systemd sets the mode of credentials, often group-readable, in a directory private to the unit
    key = load_key(key_file, check_mode=not from_systemd) if key_file else None
    return provider_class(fetcher, key)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Web search and fetch helper for lan-chat")
    parser.add_argument("--socket", required=True, help="path of the Unix socket to create")
    parser.add_argument("--search-provider", choices=sorted(PROVIDERS),
                        help="search backend, web tools stay off without one")
    parser.add_argument("--search-key-file", help="file holding the provider's API key")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    fetcher = Fetcher()
    provider = make_provider(args.search_provider, args.search_key_file, fetcher)
    server = WebServer(args.socket, WebService(fetcher, provider))
    label = provider.name if provider else "no search provider, web tools are off"
    print(f"lan-chat-web on {args.socket} ({label})", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
