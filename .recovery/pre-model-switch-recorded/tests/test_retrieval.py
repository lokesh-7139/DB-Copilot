from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.retrieval import SchemaRetriever, RetrievalUnavailable, relationship_context, table_document


def table(columns, parents=(), description=''):
    return {'columns': columns, 'description': description, 'primary_key': ['id'],
            'foreign_keys': [{'table': name, 'columns': [f'{name}_id'], 'referred_columns': ['id']}
                             for name in parents]}


SCHEMA = {
    'customers': table({'id': 'INT', 'name': 'TEXT'}),
    'orders': table({'id': 'INT', 'total': 'NUMERIC'}, ['customers']),
    'order_items': table({'id': 'INT', 'quantity': 'INT'}, ['orders', 'products']),
    'products': table({'id': 'INT', 'stock': 'INT'}, ['categories']),
    'categories': table({'id': 'INT', 'name': 'TEXT'}),
    'private_link': table({'id': 'INT'}, ['not_allowed']),
}


def settings(**overrides):
    return Settings(database_url='mssql+pyodbc://test/TestDatabase', api_key='test', model='test',
                    allowed_tables=(), db_schema='dbo', **overrides)


def vectors_for(texts):
    return [[1, 0] if 'stock' in text.lower() or text == 'inventory question' else [0, 1] for text in texts]


def test_semantic_match_and_parent_expansion(tmp_path):
    retriever = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(schema_top_k=1),
                                embedder=vectors_for, cache_dir=tmp_path)
    result = retriever.retrieve('inventory question')
    assert result.matches == [{'table': 'products', 'score': 1.0}]
    assert set(result.schema) == {'products', 'categories'}
    assert result.metadata()['relationship_tables'] == ['categories']
    assert 'not_allowed' not in result.schema


def test_foreign_key_bridge_includes_intermediate_tables():
    result = relationship_context(SCHEMA, ['customers', 'categories'])
    assert set(result) == {'customers', 'orders', 'order_items', 'products', 'categories'}


def test_bridge_supports_reverse_edges_and_disconnected_tables():
    result = relationship_context(SCHEMA, ['categories', 'customers', 'private_link'])
    assert set(result) == set(SCHEMA)
    assert 'not_allowed' not in result


def test_relationship_parents_are_expanded_transitively():
    # The matched order_items links to products, whose category parent is also required.
    result = relationship_context(SCHEMA, ['order_items'])
    assert set(result) == {'order_items', 'orders', 'customers', 'products', 'categories'}


def test_foreign_key_cycles_terminate():
    schema = {'a': table({'id':'INT'}, ['b']), 'b': table({'id':'INT'}, ['a'])}
    assert set(relationship_context(schema, ['a'])) == {'a', 'b'}


def test_cache_survives_restart_and_questions_stay_in_memory(tmp_path):
    calls = []
    def embed(texts):
        calls.append(texts)
        return vectors_for(texts)
    first = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), embedder=embed, cache_dir=tmp_path)
    first.retrieve('inventory question')
    first.retrieve('inventory question')
    assert len(calls) == 2  # One schema batch and one question.
    assert 'inventory question' not in first.cache_file.read_text()
    second = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), embedder=embed, cache_dir=tmp_path)
    second.retrieve('another question')
    assert len(calls) == 3 and calls[-1] == ['another question']


@pytest.mark.parametrize('change', ['model', 'description', 'column', 'namespace', 'allowlist'])
def test_changed_index_inputs_invalidate_cache(tmp_path, change):
    from copy import deepcopy
    original = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), embedder=vectors_for, cache_dir=tmp_path)
    schema, dialect, namespace, options = deepcopy(SCHEMA), 'tsql', 'dbo', settings()
    if change == 'model': options = replace(options, embedding_model='another-model')
    if change == 'description': schema['products']['description'] = 'Updated inventory meaning'
    if change == 'column': schema['products']['columns']['price'] = 'DECIMAL'
    if change == 'namespace': namespace = 'sales'

    if change == 'allowlist': schema.pop('private_link')
    updated = SchemaRetriever(schema, dialect, namespace, options, embedder=vectors_for, cache_dir=tmp_path)
    assert updated.fingerprint != original.fingerprint


def test_corrupt_disk_cache_is_rebuilt(tmp_path):
    retriever = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), embedder=vectors_for, cache_dir=tmp_path)
    retriever.cache_file.write_text('{broken')
    assert retriever.retrieve('inventory question').schema
    assert retriever.cache_file.read_text().startswith('{"fingerprint"')


def test_wrong_question_dimensions_are_rejected(tmp_path):
    def embed(texts):
        return [[1, 0, 0]] if texts == ['inventory question'] else vectors_for(texts)
    retriever = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), embedder=embed, cache_dir=tmp_path)
    with pytest.raises(RetrievalUnavailable, match='dimensions'):
        retriever.retrieve('inventory question')


def test_provider_response_order_is_respected(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs): self.embeddings = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create(self, **kwargs):
            return SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[0, 1]),
                                         SimpleNamespace(index=0, embedding=[1, 0])])
    monkeypatch.setattr('app.retrieval.OpenAI', FakeClient)
    retriever = SchemaRetriever(SCHEMA, 'tsql', 'dbo', settings(), cache_dir=tmp_path)
    assert retriever._openai_embed(['one', 'two']) == [[1, 0], [0, 1]]



def test_empty_schema_never_calls_provider(tmp_path):
    retriever = SchemaRetriever({}, 'tsql', 'dbo', settings(),
                                embedder=lambda _: pytest.fail('Unexpected API call'), cache_dir=tmp_path)
    assert retriever.retrieve('anything').schema == {}


def test_schema_documents_include_relationships_and_bound_input_size():
    document = table_document('orders', SCHEMA['orders'], 'tsql', 'dbo')
    assert 'customers' in document and 'dbo.orders' in document
    assert len(table_document('wide', {'description': '文' * 10000}, 'tsql', 'dbo').encode('utf-8')) <= 6000


def test_generation_uses_retrieved_schema_and_keeps_trace_out_of_model_schema(tmp_path, monkeypatch):
    import json
    from app.generation import GeneratedQuery, generate
    from app.selection import SelectionPlan, TableSelection
    captured = {}
    class FakeClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            if kwargs['text_format'] is SelectionPlan:
                return SimpleNamespace(output_parsed=SelectionPlan(
                    tables=[TableSelection(table='products',columns=['stock'])], joins=[], clarification=None))
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=GeneratedQuery(sql='SELECT stock FROM products', explanation='Stock.'))
    monkeypatch.setattr('app.generation.OpenAI', FakeClient)
    monkeypatch.setattr('app.selection.OpenAI', FakeClient)
    options = settings(schema_top_k=1)
    retriever = SchemaRetriever(SCHEMA, 'tsql', 'dbo', options, embedder=vectors_for, cache_dir=tmp_path)
    generated = generate('inventory question', SimpleNamespace(dialect='tsql', retriever=retriever), options)
    sent_schema = json.loads(captured['input'])['schema']
    assert set(sent_schema) == {'products'}
    assert set(sent_schema['products']['columns']) == {'stock'}
    assert generated._schema == sent_schema
    assert generated._retrieval['mode'] == 'embeddings'
    assert set(GeneratedQuery.model_json_schema()['properties']) == {'sql', 'explanation'}


def test_retrieval_failures_return_actionable_errors_without_running_query(client, database_stub, monkeypatch):
    def fail(*args):
        raise RetrievalUnavailable('Enable Write access to /v1/embeddings.')
    monkeypatch.setattr('app.main.generate', fail)
    response=client.post('/api/ask',json={'question':'inventory question'})
    assert response.status_code == 502
    assert '/v1/embeddings' in response.json()['error']
    assert response.json()['sql'] is None and response.json()['rows'] == []
    database_stub.execute.assert_not_called()


def test_api_rejects_sql_outside_retrieved_context(client, database_stub, monkeypatch):
    from app.generation import GeneratedQuery
    result=GeneratedQuery(sql='SELECT id FROM records',explanation='')
    result._retrieval={'mode':'embeddings','context_tables':['groups']}
    monkeypatch.setattr('app.main.generate',lambda *args: result)
    response=client.post('/api/ask',json={'question':'A schema-grounded question'})
    assert response.status_code == 422
    assert response.json()['error'] == 'Table records is not available.'
    database_stub.execute.assert_not_called()
