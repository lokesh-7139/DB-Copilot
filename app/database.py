"""SQL Server schema inspection and restricted query execution."""
import math
from threading import RLock
from time import monotonic
from types import SimpleNamespace

import pyodbc
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.retrieval import SchemaRetriever


class Database:
    dialect = 'tsql'

    def __init__(self, settings: Settings):
        self.settings = settings
        # Every physical connection enters the restricted context independently.
        pyodbc.pooling = False
        self.engine = create_engine(settings.database_url, poolclass=NullPool,
                                    connect_args={'timeout': 10})

        @event.listens_for(self.engine, 'connect')
        def restrict_connection(raw, record):
            raw.timeout = 30
            cursor = raw.cursor()
            try:
                cursor.execute(f"EXECUTE AS USER = '{settings.mssql_readonly_user}' WITH NO REVERT")
                cursor.execute('SET LOCK_TIMEOUT 30000')
                cursor.execute("SELECT USER_NAME(), HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CONTROL'), "
                               "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')")
                identity, control, create_table = cursor.fetchone()
                if identity != settings.mssql_readonly_user or control or create_table:
                    raise ValueError('SQL Server requires a restricted database user.')
                cursor.execute("SELECT COUNT(*) FROM sys.tables t WHERE "
                               "HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + "
                               "QUOTENAME(t.name), 'OBJECT', 'INSERT') = 1 OR "
                               "HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + "
                               "QUOTENAME(t.name), 'OBJECT', 'UPDATE') = 1 OR "
                               "HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(t.schema_id)) + '.' + "
                               "QUOTENAME(t.name), 'OBJECT', 'DELETE') = 1")
                if cursor.fetchone()[0]:
                    raise ValueError('SQL Server query user must not have table write access.')
            finally:
                cursor.close()

        self._catalog_lock = RLock()
        self._checked_at = 0
        self.schema = {}
        self.retriever = None
        self.snapshot(force=True)

    def snapshot(self, *, force=False):
        # Publish schema and retriever together; in-flight requests keep their snapshot.
        with self._catalog_lock:
            if force or monotonic() - self._checked_at >= 5:
                schema = self.reflect()
                if self.retriever is None or schema != self.schema:
                    retriever = SchemaRetriever(schema, self.dialect, 'all_user_schemas', self.settings)
                    self.schema, self.retriever = schema, retriever
                self._checked_at = monotonic()
            return SimpleNamespace(schema=self.schema, retriever=self.retriever,
                                   dialect=self.dialect, engine=self.engine, execute=self.execute)

    def reflect(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            result = {}
            for namespace in inspector.get_schema_names():
                if namespace in {'sys', 'INFORMATION_SCHEMA'}:
                    continue
                for name in inspector.get_table_names(schema=namespace):
                    key = f'{namespace}.{name}'
                    result[key] = {
                        'schema': namespace, 'table': name,
                        'columns': {c['name']: str(c['type']) for c in inspector.get_columns(name, schema=namespace)},
                        'description': inspector.get_table_comment(name, schema=namespace).get('text'),
                        'primary_key': inspector.get_pk_constraint(name, schema=namespace).get('constrained_columns', []),
                        'foreign_keys': [
                            {'columns': fk['constrained_columns'],
                             'table': f"{fk.get('referred_schema') or namespace}.{fk['referred_table']}",
                             'referred_columns': fk['referred_columns']}
                            for fk in inspector.get_foreign_keys(name, schema=namespace)
                        ],
                    }
            for meta in result.values():
                meta['foreign_keys'] = [fk for fk in meta['foreign_keys'] if fk['table'] in result]
            return result

    def execute(self, sql: str):
        with self.engine.connect() as connection:
            try:
                connection.connection.driver_connection.timeout = max(1, math.ceil(self.settings.timeout_ms / 1000))
                connection.exec_driver_sql(f'SET LOCK_TIMEOUT {self.settings.timeout_ms}').close()
                result = connection.execution_options(stream_results=True).exec_driver_sql(sql)
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(self.settings.max_rows + 1)]
                result.close()
                return columns, rows[:self.settings.max_rows], len(rows) > self.settings.max_rows
            finally:
                connection.rollback()
