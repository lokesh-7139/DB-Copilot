from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.generation import GeneratedQuery
from app.main import create_app


@pytest.fixture
def options():
    return Settings(database_url='mssql+pyodbc://test/TestDatabase', api_key='test', model='test',
                    db_schema='dbo', allowed_tables=())


@pytest.fixture
def database_stub():
    database = MagicMock()
    database.dialect = 'tsql'
    database.schema = {
        'records': {'columns': {'id': 'INT', 'name': 'NVARCHAR(100)', 'group_id': 'INT'},
                    'primary_key': ['id'], 'foreign_keys': [], 'description': None},
        'groups': {'columns': {'id': 'INT', 'name': 'NVARCHAR(100)'},
                   'primary_key': ['id'], 'foreign_keys': [], 'description': None},
    }
    database.engine.url = SimpleNamespace(database='TestDatabase')
    database.retriever.status.return_value = {'mode': 'embeddings', 'indexed_tables': 0, 'top_k': 3}
    database.execute.return_value = (['id', 'name'], [[1, 'First'], [2, 'Second']], False)
    return database


@pytest.fixture
def client(monkeypatch, options, database_stub):
    monkeypatch.setattr('app.main.Database', lambda _: database_stub)
    def generated(*args):
        result = GeneratedQuery(sql='SELECT id, name FROM records ORDER BY id', explanation='Matching records.')
        result._retrieval = {'mode': 'embeddings', 'context_tables': ['records'],
                             'matched_tables': [{'table': 'records', 'score': 1.0}], 'relationship_tables': []}
        return result
    monkeypatch.setattr('app.main.generate', generated)
    with TestClient(create_app(options)) as connection:
        yield connection
