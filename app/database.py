"""SQL Server schema inspection and restricted query execution."""
import math

import pyodbc
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.retrieval import SchemaRetriever


class Database:
    dialect = 'tsql'

    def __init__(self, settings: Settings):
        self.settings = settings
        self.schema_name = settings.db_schema
        # Every physical connection enters the restricted context independently.
        pyodbc.pooling = False
        self.engine = create_engine(settings.database_url, poolclass=NullPool,
                                    connect_args={'timeout': 10})

        @event.listens_for(self.engine, 'connect')
        def restrict_connection(raw, record):
            raw.timeout = max(1, math.ceil(settings.timeout_ms / 1000))
            cursor = raw.cursor()
            try:
                cursor.execute(f"EXECUTE AS USER = '{settings.mssql_readonly_user}' WITH NO REVERT")
                cursor.execute(f'SET LOCK_TIMEOUT {settings.timeout_ms}')
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

        self.schema = self.reflect()
        self.retriever = SchemaRetriever(self.schema, self.dialect, self.schema_name, settings)

    def reflect(self):
        with self.engine.connect() as connection:
            inspector = inspect(connection)
            names = inspector.get_table_names(schema=self.schema_name)
            allowed = set(self.settings.allowed_tables)
            if allowed - set(names):
                raise ValueError('ALLOWED_TABLES contains tables absent from the configured schema.')
            result = {}
            for name in names:
                if allowed and name not in allowed:
                    continue
                result[name] = {
                    'columns': {c['name']: str(c['type']) for c in inspector.get_columns(name, schema=self.schema_name)},
                    'description': inspector.get_table_comment(name, schema=self.schema_name).get('text'),
                    'primary_key': inspector.get_pk_constraint(name, schema=self.schema_name).get('constrained_columns', []),
                    'foreign_keys': [
                        {'columns': fk['constrained_columns'], 'table': fk['referred_table'],
                         'referred_columns': fk['referred_columns']}
                        for fk in inspector.get_foreign_keys(name, schema=self.schema_name)
                    ],
                }
            return result

    def execute(self, sql: str):
        with self.engine.connect() as connection:
            try:
                result = connection.execution_options(stream_results=True).exec_driver_sql(sql)
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(self.settings.max_rows + 1)]
                result.close()
                return columns, rows[:self.settings.max_rows], len(rows) > self.settings.max_rows
            finally:
                connection.rollback()
