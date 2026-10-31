import os
import sys
import tempfile
from pathlib import Path

# Isolated SQLite DB per test session; must be set before revenueos is imported.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp) / 'test.db'}"
os.environ["REVENUEOS_NO_DOTENV"] = "1"  # tests never read the real .env (keys, SMTP, CRM)
os.environ.pop("SMTP_HOST", None)
os.environ.pop("SF_USERNAME", None)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
# Any model call a test forgets to mock fails fast locally instead of reaching the real API.
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from revenueos.config import settings  # noqa: E402
from revenueos.db import Base, ControlBase, _factories, engine, init_db  # noqa: E402
from revenueos.services import auth, workspaces  # noqa: E402
from revenueos.tools import crm  # noqa: E402

workspaces.install()


@pytest.fixture(autouse=True)
def fresh_db():
    init_db()
    yield
    Base.metadata.drop_all(engine)
    ControlBase.metadata.drop_all(engine)
    for f in list(_factories.values()):  # client workspace databases created by a test
        Base.metadata.drop_all(f.kw["bind"])
    _factories.clear()
    workspaces._cache.clear()
    crm._crm.clear()
    auth.reset_cache()
    # Tests patch settings with object.__setattr__; drop those shadows so workspace overrides apply again.
    for key in [k for k in vars(settings) if k not in ("_base", "_overrides")]:
        del vars(settings)[key]
