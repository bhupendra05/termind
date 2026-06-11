"""termind entry point — boots the terminal REPL and a local web UI sharing one brain.

  termind            terminal + web (default)
  termind --no-web   terminal only
  termind --web      web only (browser UI)
"""
from __future__ import annotations

import argparse
import threading

from .repl import Session, run
from .web import serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="termind", add_help=True,
                                 description="A local AI agent for your terminal + browser.")
    ap.add_argument("--no-web", action="store_true", help="terminal only (skip the web UI)")
    ap.add_argument("--web", action="store_true", help="web UI only (no terminal REPL)")
    ap.add_argument("--port", type=int, default=8765, help="web UI port (default 8765)")
    args = ap.parse_args(argv)

    session = Session()                       # one shared brain for both surfaces

    if args.web:                              # web only — block on the server
        httpd, url = serve(session, port=args.port)
        print(f"▲ termind web → {url}   (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            return 0
        return 0

    web_url = None
    if not args.no_web:                       # default: start web in the background…
        try:
            httpd, web_url = serve(session, port=args.port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
        except OSError:
            web_url = None                    # port busy → just run the terminal

    return run(session, web_url=web_url)      # …and run the terminal in the foreground


if __name__ == "__main__":
    raise SystemExit(main())
