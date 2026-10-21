"""Fail closed when pytest is not explicitly pointed at temporary SQLite."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import validate_test_database_url

validate_test_database_url(os.environ.get("SNEAKCHAT_DATABASE_URL"))
