import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv('DATABASE_URL', '')
    api_key: str = os.getenv('OPENAI_API_KEY', '')
    model: str = os.getenv('OPENAI_MODEL', '')
    db_schema: str = os.getenv('DB_SCHEMA', 'dbo')
    allowed_tables: tuple[str, ...] = tuple(
        t.strip() for t in os.getenv('ALLOWED_TABLES', '').split(',') if t.strip()
    )
    max_rows: int = int(os.getenv('MAX_ROWS', '200'))
    timeout_ms: int = int(os.getenv('QUERY_TIMEOUT_MS', '5000'))
    mssql_readonly_user: str = os.getenv('MSSQL_READONLY_USER', 'copilot_reader')
    embedding_model: str = os.getenv('EMBEDDING_MODEL', 'text-embedding-3-small')
    schema_top_k: int = int(os.getenv('SCHEMA_TOP_K', '3'))

    def __post_init__(self):
        if not self.database_url or make_url(self.database_url).drivername != 'mssql+pyodbc':
            raise ValueError('Set DATABASE_URL to a SQL Server mssql+pyodbc connection.')
        if not 1 <= self.max_rows <= 10000 or not 1 <= self.timeout_ms <= 60000:
            raise ValueError('Invalid row limit or query timeout.')
        if not self.api_key or not self.model:
            raise ValueError('Set OPENAI_API_KEY and OPENAI_MODEL.')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', self.mssql_readonly_user):
            raise ValueError('MSSQL_READONLY_USER must be a simple database user name.')
        if not 1 <= self.schema_top_k <= 20:
            raise ValueError('SCHEMA_TOP_K must be between 1 and 20.')
        if not self.embedding_model.strip():
            raise ValueError('EMBEDDING_MODEL cannot be empty.')
