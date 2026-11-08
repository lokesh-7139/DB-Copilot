from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.generation import generate_without_rag
from app.validation import UnsafeSQL, validate_sql


def metadata(namespace, table):
    return {'schema': namespace, 'table': table, 'columns': {'id': 'INT', 'name': 'NVARCHAR(100)'},
            'foreign_keys': [], 'primary_key': ['id'], 'description': None}


@pytest.fixture
def catalog():
    return {f'{ns}.customers': metadata(ns, 'customers') for ns in ['dbo', 'sales']}


def test_without_rag_sends_entire_catalog_and_never_retrieves(catalog, options, monkeypatch):
    import json
    captured = []
    class FakeClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            captured.append(json.loads(kwargs['input']))
            return SimpleNamespace(output_parsed=kwargs['text_format'](
                missing_requirements=[], sql='SELECT name FROM sales.customers WHERE id=15', explanation='Customer.'))
    monkeypatch.setattr('app.generation.OpenAI', FakeClient)
    monkeypatch.setattr('app.generation.select_schema', lambda *args: pytest.fail('No selection in full mode'))
    database = SimpleNamespace(schema=catalog, retriever=MagicMock())
    result = generate_without_rag('Customer 15', database, options)
    assert captured == [{'question': 'Customer 15', 'schema': catalog}]
    assert result._schema == catalog and result._retrieval['mode'] == 'full_schema'
    database.retriever.retrieve.assert_not_called()


def test_api_modes_are_independent(client, monkeypatch, database_stub):
    from app.generation import GeneratedQuery
    baseline = MagicMock(return_value=GeneratedQuery(sql='SELECT id FROM groups', explanation='Full schema.'))
    monkeypatch.setattr('app.main.generate_without_rag', baseline)
    monkeypatch.setattr('app.main.generate', lambda *args: (_ for _ in ()).throw(RuntimeError('RAG unavailable')))
    assert client.post('/api/ask', json={'question': 'Show IDs', 'mode': 'rag'}).status_code == 502
    full = client.post('/api/ask', json={'question': 'Show IDs', 'mode': 'full'})
    assert full.status_code == 200 and full.json()['error'] is None
    assert full.json()['mode'] == 'full'
    assert baseline.call_count == 1
    database_stub.execute.assert_called_once()


def test_invalid_mode_rejected(client):
    assert client.post('/api/ask', json={'question': 'Show records', 'mode': 'other'}).status_code == 422


def test_refresh_endpoint_publishes_latest_catalog(client, database_stub):
    database_stub.schema['new_table'] = metadata('sales', 'new_table')
    response = client.post('/api/schema/refresh')
    assert response.status_code == 200 and 'new_table' in response.json()['tables']
    database_stub.snapshot.assert_called_with(force=True)


@pytest.mark.parametrize('query', [
    'SELECT a.name, b.name FROM dbo.customers a JOIN sales.customers b ON a.id=b.id',
    'WITH x AS (SELECT name FROM sales.customers) SELECT name FROM x',
    'SELECT sales.customers.name FROM sales.customers',
])
def test_cross_schema_sql_is_validated_against_exact_tables(catalog, query):
    assert validate_sql(query, catalog, allow_wildcards=False)


@pytest.mark.parametrize('query', [
    'SELECT name FROM customers',
    'SELECT name FROM private.customers',
    'SELECT name FROM anotherdb.sales.customers',
    'SELECT password FROM sales.customers',
    'DELETE FROM sales.customers',
])
def test_ambiguity_unavailable_columns_and_writes_stay_blocked(catalog, query):
    with pytest.raises(UnsafeSQL):
        validate_sql(query, catalog, allow_wildcards=False)


def test_reflection_includes_all_user_schemas_and_qualified_relationships(options, monkeypatch):
    from app.database import Database
    inspector = MagicMock()
    inspector.get_schema_names.return_value = ['dbo', 'sales', 'sys', 'INFORMATION_SCHEMA']
    inspector.get_table_names.return_value = ['customers']
    inspector.get_columns.return_value = [{'name': 'id', 'type': 'INT'}]
    inspector.get_pk_constraint.return_value = {'constrained_columns': ['id']}
    inspector.get_table_comment.return_value = {'text': None}
    inspector.get_foreign_keys.side_effect = lambda name, schema: ([{
        'constrained_columns': ['id'], 'referred_schema': 'dbo',
        'referred_table': 'customers', 'referred_columns': ['id']}] if schema == 'sales' else [])
    monkeypatch.setattr('app.database.inspect', lambda _: inspector)
    db = Database.__new__(Database)
    db.engine = MagicMock()
    db.settings = options
    schema = db.reflect()
    assert set(schema) == {'dbo.customers', 'sales.customers'}
    assert schema['sales.customers']['foreign_keys'][0]['table'] == 'dbo.customers'
    assert inspector.get_table_names.call_count == 2


def test_refresh_replaces_catalog_atomically_without_mutating_old_snapshot(options, monkeypatch):
    from threading import RLock
    from app.database import Database
    db = Database.__new__(Database)
    db.settings, db.engine = options, MagicMock()
    db._catalog_lock, db._checked_at = RLock(), 0
    db.schema, db.retriever = {}, None
    first = {'dbo.a': metadata('dbo', 'a')}
    second = {'sales.b': metadata('sales', 'b')}
    db.reflect = MagicMock(side_effect=[first, second, second])
    factory = MagicMock(side_effect=lambda schema, *args: SimpleNamespace(schema=schema))
    monkeypatch.setattr('app.database.SchemaRetriever', factory)
    old = db.snapshot(force=True)
    latest = db.snapshot(force=True)
    unchanged = db.snapshot(force=True)
    assert old.schema == first and latest.schema == second
    assert old.retriever.schema == first and latest.retriever.schema == second
    assert unchanged.retriever is latest.retriever
    assert factory.call_count == 2
