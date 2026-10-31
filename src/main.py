import os
import sys

# Running this file directly (e.g. PyCharm's "Run", or `python src\main.py`)
# puts src/ itself on sys.path, not the project root — a plain relative
# import (`from .modules.app import app`) then fails with "attempted
# relative import with no known parent package", since Python has no
# package context for a script run this way. Explicitly adding the project
# root (one level up from this file) makes the absolute import below work
# the same whether this is run directly or as `python -m src.main`.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import threading
import webbrowser

from src.modules.app import app

HOST = "127.0.0.1"
PORT = 5000

if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    # Printed directly (not via the werkzeug logger) since that logger is
    # set to WARNING to silence per-request access logs — this would get
    # silenced along with them otherwise.
    print(url)
    # The packaged .exe has no console the average user would think to read
    # for "now open this URL" — so the browser is opened for them instead.
    # Delayed via a timer (rather than opening before app.run()) so it fires
    # shortly after the server is actually listening instead of racing it;
    # the delay is generous because a onefile .exe has extra startup
    # overhead (unpacking itself to a temp dir) compared to running the
    # plain script.
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False)
