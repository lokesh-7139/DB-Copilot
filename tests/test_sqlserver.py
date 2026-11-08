"""T-SQL unit checks and opt-in checks against any configured SQL Server database."""
import os
from dataclasses import replace

import pytest
from fastapi.encoders import jsonable_encoder

from app.config import Settings
from app.database import Database
from app.validation import validate_sql


@pytest.mark.parametrize('expression', [
    'DATEFROMPARTS(YEAR(GETDATE()),MONTH(GETDATE()),1)',
    'DATEADD(month,-6,CAST(GETDATE() AS date))',
    'DATEADD(MONTH,DATEDIFF(MONTH,0,GETDATE())-5,0)',
])
def test_tsql_date_functions(expression):
    assert validate_sql(f'SELECT {expression} AS result', {})


@pytest.fixture(scope='module')
def sqlserver():
    if os.getenv('RUN_SQLSERVER_TESTS') != '1':
        pytest.skip('Set RUN_SQLSERVER_TESTS=1 to test the configured SQL Server database.')
    database = Database(Settings())
    yield database
    database.engine.dispose()


def table_target(database):
    if not database.schema:
        pytest.skip('No visible tables are configured.')
    name = sorted(database.schema)[0]
    quote = database.engine.dialect.identifier_preparer.quote_identifier
    return f'{quote(database.schema_name)}.{quote(name)}', name


def test_reflection_and_select(sqlserver):
    target, name = table_target(sqlserver)
    columns, rows, truncated = sqlserver.execute(f'SELECT TOP (1) * FROM {target}')
    assert set(columns) == set(sqlserver.schema[name]['columns'])
    assert len(rows) <= 1 and not truncated


def test_normalized_tsql_executes(sqlserver):
    sql = 'SELECT DATEADD(MONTH,DATEDIFF(MONTH,0,GETDATE())-5,0) AS month_start'
    assert sqlserver.execute(validate_sql(sql, sqlserver.schema))[1] == sqlserver.execute(sql)[1]


def test_sqlserver_api_sql_and_results(sqlserver, monkeypatch):
    from fastapi.testclient import TestClient
    from app.generation import GeneratedQuery
    from app.main import create_app
    target, name = table_target(sqlserver)
    sql = f'SELECT TOP (3) * FROM {target}'
    def generated(*args):
        result = GeneratedQuery(sql=sql, explanation='Matching rows.')
        result._retrieval = {'mode': 'embeddings', 'context_tables': [name]}
        return result
    monkeypatch.setattr('app.main.generate', generated)
    with TestClient(create_app(sqlserver.settings)) as client:
        response = client.post('/api/ask', json={'question': 'Show three rows'})
        assert response.status_code == 200, response.text
        data = response.json()
        columns, rows, _ = sqlserver.execute(data['sql'])
        assert data['columns'] == columns and data['rows'] == jsonable_encoder(rows)


def test_security_context_cannot_revert(sqlserver):
    with sqlserver.engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT USER_NAME()').scalar() == sqlserver.settings.mssql_readonly_user
        try:
            connection.exec_driver_sql('REVERT')
        except Exception:
            connection.rollback()
        assert connection.exec_driver_sql('SELECT USER_NAME()').scalar() == sqlserver.settings.mssql_readonly_user


@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_writes_are_denied_without_validator(sqlserver, operation):
    target, name = table_target(sqlserver)
    quote = sqlserver.engine.dialect.identifier_preparer.quote_identifier
    column = quote(next(iter(sqlserver.schema[name]['columns'])))
    sql = (f'UPDATE {target} SET {column}={column} WHERE 1=0' if operation == 'update'
           else f'DELETE FROM {target} WHERE 1=0')
    with pytest.raises(Exception, match='229|permission'):
        sqlserver.execute(sql)


def test_result_cap(sqlserver):
    database = Database(replace(sqlserver.settings, max_rows=2))
    try:
        columns, rows, truncated = database.execute('SELECT 1 AS n UNION ALL SELECT 2 UNION ALL SELECT 3')
        assert columns == ['n'] and rows == [[1], [2]] and truncated
    finally:
        database.engine.dispose()


def test_query_timeout(sqlserver):
    database = Database(replace(sqlserver.settings, timeout_ms=1000))
    try:
        with pytest.raises(Exception, match='HYT00|timeout|Timeout|timed out'):
            database.execute("WAITFOR DELAY '00:00:04'; SELECT 1")
    finally:
        database.engine.dispose()
