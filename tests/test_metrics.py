from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.metrics import RequestMetrics, active_metrics, begin_call, finish_call, cache_hit


def test_pipeline_totals_include_selection_and_keep_embedding_usage_separate():
    metrics = RequestMetrics()
    token = active_metrics.set(metrics)
    try:
        schema = {'dbo.records': {'columns': {'id': 'INT', 'name': 'TEXT'}}}
        selection = begin_call('selection', schema=schema)
        finish_call(selection, SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120)))
        generation = begin_call('generation', schema=schema)
        finish_call(generation, SimpleNamespace(usage={'input_tokens': 50, 'output_tokens': 10, 'total_tokens': 60}))
        embedding = begin_call('embeddings', embedding=True)
        finish_call(embedding, SimpleNamespace(usage=SimpleNamespace(prompt_tokens=7, total_tokens=7)))
        cache_hit('schema_embeddings')
    finally:
        active_metrics.reset(token)
    summary = metrics.summary()
    assert summary['input_tokens'] == 150 and summary['output_tokens'] == 30
    assert summary['total_tokens'] == 180 and summary['embedding_tokens'] == 7
    assert summary['llm_calls'] == 2 and summary['embedding_calls'] == 1
    assert summary['calls'][0]['schema']['table_count'] == 1
    assert summary['calls'][0]['schema']['column_count'] == 2
    assert summary['cache_hits'] == {'schema_embeddings': True}


def test_missing_usage_is_unknown_not_zero():
    metrics = RequestMetrics()
    token = active_metrics.set(metrics)
    try:
        call = begin_call('generation')
        finish_call(call, SimpleNamespace(usage=None))
    finally:
        active_metrics.reset(token)
    assert metrics.summary()['total_tokens'] is None
    assert metrics.summary()['input_tokens'] is None
    assert metrics.summary()['embedding_tokens'] == 0  # No embedding calls were made.


def test_parallel_requests_do_not_share_usage():
    def request(count):
        metrics = RequestMetrics()
        token = active_metrics.set(metrics)
        try:
            call = begin_call('generation')
            finish_call(call, SimpleNamespace(usage={'input_tokens': count, 'output_tokens': 1, 'total_tokens': count + 1}))
            return metrics.summary()
        finally:
            active_metrics.reset(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(request, [10, 100]))
    assert a['total_tokens'] == 11 and b['total_tokens'] == 101
    assert a['llm_calls'] == b['llm_calls'] == 1
    assert active_metrics.get() is None


def test_api_preserves_partial_metrics_on_failure(client, monkeypatch):
    def fail(*args):
        call = begin_call('selection')
        finish_call(call, SimpleNamespace(usage={'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}))
        begin_call('generation')
        raise RuntimeError('Provider failed')
    monkeypatch.setattr('app.main.generate', fail)
    response = client.post('/api/ask', json={'question': 'Show records'})
    assert response.status_code == 502
    metrics = response.json()['resources']
    assert metrics['llm_calls'] == 2 and metrics['total_tokens'] is None
    assert metrics['calls'][0]['total_tokens'] == 12
    assert active_metrics.get() is None


def test_generation_records_api_usage_and_schema(client, database_stub, options, monkeypatch):
    from app.generation import generate_without_rag
    class FakeClient:
        def __init__(self, **kwargs): self.responses = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def parse(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=101, output_tokens=9, total_tokens=110),
                output_parsed=kwargs['text_format'](missing_requirements=[], sql='SELECT id FROM records', explanation=''))
    monkeypatch.setattr('app.generation.OpenAI', FakeClient)
    response = client.post('/api/ask', json={'question': 'Show records', 'mode': 'full'})
    assert response.status_code == 200
    metrics = response.json()['resources']
    assert metrics['total_tokens'] == 110 and metrics['llm_calls'] == 1
    assert metrics['calls'][0]['schema']['tables'] == list(database_stub.schema)


def test_retrieval_cache_avoids_counting_embedding_calls(options, tmp_path, monkeypatch):
    from app.retrieval import SchemaRetriever
    class FakeClient:
        def __init__(self, **kwargs): self.embeddings = self
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def create(self, **kwargs):
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, total_tokens=5),
                data=[SimpleNamespace(index=i, embedding=[1, 0]) for i, _ in enumerate(kwargs['input'])])
    monkeypatch.setattr('app.retrieval.OpenAI', FakeClient)
    retriever = SchemaRetriever({'records': {'columns': {'id': 'INT'}}}, 'tsql', 'dbo', options, cache_dir=tmp_path)
    first = RequestMetrics()
    token = active_metrics.set(first)
    try: retriever.retrieve('show records')
    finally: active_metrics.reset(token)
    second = RequestMetrics()
    token = active_metrics.set(second)
    try: retriever.retrieve('show records')
    finally: active_metrics.reset(token)
    assert first.summary()['embedding_calls'] == 2 and first.summary()['embedding_tokens'] == 10
    assert second.summary()['embedding_calls'] == second.summary()['embedding_tokens'] == 0
    assert second.summary()['cache_hits'] == {'schema_embeddings': True, 'question_embedding': True}
