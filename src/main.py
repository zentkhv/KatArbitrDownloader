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

from src.modules.app import app

HOST = "127.0.0.1"
PORT = 5000

if __name__ == "__main__":
    # Printed directly (not via the werkzeug logger) since that logger is
    # set to WARNING to silence per-request access logs — this would get
    # silenced along with them otherwise.
    print(f"http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False)
