#!/usr/bin/env python3
"""Serve the viewer with version-aware persistent caching for large PLY files."""
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


class ViewerRequestHandler(SimpleHTTPRequestHandler):
    def parse_request(self) -> bool:
        # Some forwarded browser sessions prepend a short control-byte frame
        # before an otherwise valid HTTP/1 request. Ignore that frame if the
        # standard method line immediately follows it.
        for method in (b"GET ", b"HEAD ", b"OPTIONS "):
            start = self.raw_requestline.find(method)
            if 0 < start <= 32 and all(byte <= 0x20 for byte in self.raw_requestline[:start]):
                self.raw_requestline = self.raw_requestline[start:]
                break
        return super().parse_request()

    def end_headers(self) -> None:
        # This simple server is used for large streamed PLY responses. Closing
        # each response explicitly avoids clients reusing a connection after a
        # progressive PLY load has ended.
        self.close_connection = True
        self.send_header("Connection", "close")
        parsed = urlsplit(getattr(self, "path", ""))
        request_path = unquote(parsed.path)
        local_path = Path(self.translate_path(request_path))

        if request_path.lower().endswith(".ply") and local_path.is_file():
            stat = local_path.stat()
            version = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
            self.send_header("ETag", f'"{version}"')
            if parse_qs(parsed.query).get("v") == [version]:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                # The small HEAD request discovers the current ETag before the
                # browser requests the versioned PLY URL.
                self.send_header("Cache-Control", "no-cache")
        elif "/assets/" in request_path:
            # Vite names these files by content hash.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        elif request_path.endswith("/") or request_path.lower().endswith(".html"):
            self.send_header("Cache-Control", "no-cache")

        super().end_headers()


def main() -> None:
    server = ThreadingHTTPServer(
        ("0.0.0.0", 8080),
        lambda *args, **kwargs: ViewerRequestHandler(*args, directory="/srv", **kwargs),
    )
    print("Serving NextGen viewer on 0.0.0.0:8080", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
