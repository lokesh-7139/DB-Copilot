import pytest

from app.generation import GeneratedQuery
from app.validation import UnsafeSQL, validate_sql


def test_home_and_schema(client):
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200
    data = client.get('/api/schema').json()
    assert data['database'] == 'TestDatabase'
    assert set(data['tables']) == {'records', 'groups'}
    assert data['retrieval']['mode'] == 'embeddings'


def test_returns_executed_sql_and_rows(client, database_stub):
    response = client.post('/api/ask', json={'question': 'List records'})
    assert response.status_code == 200
    result = response.json()
    database_stub.execute.assert_called_once_with(result['sql'])
    assert result['columns'] == ['id', 'name']
    assert result['rows'] == [[1, 'First'], [2, 'Second']]
    assert result['row_count'] == 2 and not result['truncated']
    assert result['retrieval']['context_tables'] == ['records']


@pytest.mark.parametrize('sql', [
    'DELETE FROM records',
    'SELECT * FROM records; DROP TABLE records',
    'SELECT * INTO copy FROM records',
    'WITH x AS (DELETE FROM records OUTPUT deleted.id) SELECT * FROM x',
    "EXEC xp_cmdshell 'whoami'",
    'SELECT * FROM master.sys.databases',
    'SELECT * FROM secrets',
    'SELECT password FROM records',
    'SELECT id FROM records r JOIN groups g ON g.id = r.group_id',
    'SELECT * FROM records WITH (UPDLOCK)',
])
def test_rejects_unsafe_or_invalid_sql(database_stub, sql):
    with pytest.raises(UnsafeSQL):
        validate_sql(sql, database_stub.schema)


def test_cte_and_schema_qualifiers(database_stub):
    sql = 'WITH grouped AS (SELECT group_id, COUNT(*) AS total FROM dbo.records GROUP BY group_id) SELECT * FROM grouped'
    assert 'dbo.records' in validate_sql(sql, database_stub.schema)


def test_clarification_does_not_execute(client, database_stub, monkeypatch):
    monkeypatch.setattr('app.main.generate', lambda *args: GeneratedQuery(sql=None, explanation='Which date range?'))
    result = client.post('/api/ask', json={'question': 'Show recent activity'}).json()
    assert result['sql'] is None and result['rows'] == []
    assert result['explanation'] == 'Which date range?'
    database_stub.execute.assert_not_called()


def test_invalid_sql_is_visible_but_not_executed(client, database_stub, monkeypatch):
    monkeypatch.setattr('app.main.generate', lambda *args: GeneratedQuery(sql='DELETE FROM records', explanation=''))
    response = client.post('/api/ask', json={'question': 'Delete everything'})
    assert response.status_code == 422
    assert response.json()['sql'] == 'DELETE FROM records'
    database_stub.execute.assert_not_called()


def test_empty_results(client, database_stub):
    database_stub.execute.return_value = (['id', 'name'], [], False)
    result = client.post('/api/ask', json={'question': 'Find matching records'}).json()
    assert result['columns'] == ['id', 'name'] and result['rows'] == [] and result['error'] is None


def test_result_truncation_is_reported(client, database_stub):
    database_stub.execute.return_value = (['id'], [[1]], True)
    result = client.post('/api/ask', json={'question': 'List records'}).json()
    assert result['row_count'] == 1 and result['truncated']


def test_execution_error_preserves_sql_and_hides_details(client, database_stub):
    database_stub.execute.side_effect = RuntimeError('secret-password')
    response = client.post('/api/ask', json={'question': 'List records'})
    assert response.status_code == 502 and response.json()['sql']
    assert 'secret-password' not in response.text


def test_input_limits(client):
    assert client.post('/api/ask', json={'question': 'x'}).status_code == 422
    assert client.post('/api/ask', json={'question': 'x' * 2001}).status_code == 422
