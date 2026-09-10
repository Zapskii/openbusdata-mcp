import os
import sys
import tempfile
from pathlib import Path

import pytest

# Redirect HOME + DB_PATH to a temp dir BEFORE importing the server module,
# so its import-time side effects (ensure_schema, counts) touch nothing real.
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import openbusdata_mcp.store as store_mod  # noqa: E402
store_mod.DB_PATH = Path(os.environ["HOME"]) / "index.db"

import openbusdata_mcp.server as server  # noqa: E402
from openbusdata_mcp.store import TimetableStore, TimetableWriter  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    """Give every test its own temp DB.

    The server's module-global writer (and store) are rebuilt on the per-test
    path so loader paths (server.writer) and query paths (server.store, e.g.
    plan_journey) both land on the same fresh DB. store_mod.DB_PATH is read at
    construction time (store.py:50, 339), so resetting it per test works.
    """
    store_mod.DB_PATH = tmp_path / "index.db"
    server.writer = TimetableWriter()
    server.writer.ensure_schema()
    server.store = TimetableStore()
    yield server.writer
    if server.writer._conn is not None:
        server.writer._conn.close()
    server.store.close()


@pytest.fixture()
def writer(_fresh_db):
    return server.writer


@pytest.fixture()
def store(_fresh_db):
    s = TimetableStore()
    yield s
    s.close()
