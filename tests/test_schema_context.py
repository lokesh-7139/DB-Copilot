from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.generation import generate
from app.metrics import RequestMetrics, active_metrics
from app.retrieval import RetrievedSchema
from app.schema_context import retrieve_context
from app.validation import UnsafeSQL, validate_sql


def test_rag_preserves_filter_columns_and_relationship_metadata_with_one_llm_call(options, monkeypatch):
    import json
    catalog = {
        'sales.customers': {'schema': 'sales', 'table': 'customers',
            'columns': {'id': 'INT', 'name': 'TEXT', 'city': 'TEXT', 'registered_at': 'DATE'},
            'primary_key': ['id'], 'foreign_keys': [], 'description': 'Customer directory'},
        'sales.orders': {'schema': 'sales', 'table': 'orders',
            'columns': {'id': 'INT', 'customer_id': 'INT', 'amount': 'DECIMAL'},
            'primary_key': ['id'], 'foreign_keys': [{'columns': ['customer_id'],
            'table': 'sales.customers', 'referred_columns': ['id']}], 'description': None},
    }
    retrieved = RetrievedSchema(catalog, 'embeddings', [{'table': 'sales.orders', 'score': 0.9}])
    db = SimpleNamespace(retriever=SimpleNamespace(retrieve=MagicMock(return_value=retrieved)))
    calls = []
    class FakeClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            calls.append(json.loads(kwargs['input']))
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10, total_tokens=110),
                output_parsed=kwargs['text_format'](missing_requirements=[],
                sql='SELECT name,city FROM sales.customers WHERE id=15', explanation=''))
    monkeypatch.setattr('app.generation.OpenAI', FakeClient)
    metrics = RequestMetrics()
    token = active_metrics.set(metrics)
    try: result = generate('Name and city of customer 15', db, options)
    finally: active_metrics.reset(token)
    assert len(calls) == 1 and calls[0]['schema'] == catalog
    assert result._schema == catalog and result._retrieval['stage'] == 'retrieved'
    assert result._retrieval['selected_columns']['sales.customers'] == ['id','name','city','registered_at']
    assert metrics.summary()['llm_calls'] == 1
    assert [call['stage'] for call in metrics.calls] == ['generation']
    assert validate_sql(result.sql, result._schema, allow_wildcards=False)
    with pytest.raises(UnsafeSQL):
        validate_sql('SELECT password FROM sales.customers', result._schema, allow_wildcards=False)


def test_empty_retrieval_skips_generation_and_clarifies(options, monkeypatch):
    db = SimpleNamespace(retriever=SimpleNamespace(retrieve=lambda _: RetrievedSchema({}, 'embeddings', [])))
    monkeypatch.setattr('app.generation.OpenAI', lambda **kwargs: pytest.fail('Unexpected LLM call'))
    result = generate('Show customers', db, options)
    assert result.sql is None and result.explanation


def test_schema_inspection_endpoint_executes_no_model_or_data_query(client, database_stub, monkeypatch):
    db = database_stub
    db.retriever.retrieve.return_value = RetrievedSchema(db.schema, 'embeddings', [])
    response = client.post('/api/schema/select', json={'question': 'Show records'})
    assert response.status_code == 200
    assert response.json()['schema'] == db.schema
    assert response.json()['retrieval']['stage'] == 'retrieved'
    db.execute.assert_not_called()
