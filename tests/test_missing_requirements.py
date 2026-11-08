from types import SimpleNamespace

from app.generation import generate
from app.retrieval import RetrievedSchema
from app.schema_context import SchemaContext


def test_missing_requirement_discards_even_model_supplied_sql(client, database_stub, monkeypatch):
    schema = {'records': {**database_stub.schema['records'], 'columns': {'name': 'NVARCHAR(100)'}}}
    selected = SchemaContext(schema, RetrievedSchema(schema, 'embeddings', []))
    monkeypatch.setattr('app.generation.retrieve_context', lambda *args: selected)
    class FakeClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            return SimpleNamespace(output_parsed=kwargs['text_format'](
                missing_requirements=['ID filter requires an ID column'],
                sql="SELECT name FROM records WHERE name='15'", explanation='The ID column is unavailable.'))
    monkeypatch.setattr('app.generation.OpenAI', FakeClient)
    monkeypatch.setattr('app.main.generate', generate)
    response = client.post('/api/ask', json={'question': 'What is the name of record with id 15?'})
    assert response.status_code == 200
    assert response.json()['sql'] is None and response.json()['rows'] == []
    database_stub.execute.assert_not_called()
