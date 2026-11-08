from types import SimpleNamespace

import pytest

from app.retrieval import RetrievedSchema
from app.selection import (SelectionPlan, TableSelection, JoinSelection, InvalidSelection,
                           apply_selection, select_schema, SelectedSchema)
from app.validation import UnsafeSQL, validate_sql
from app.generation import GeneratedQuery


def meta(columns, primary_key=(), foreign_keys=()):
    return {'columns': columns, 'primary_key': list(primary_key), 'foreign_keys': list(foreign_keys),
            'description': None}


@pytest.fixture
def candidates():
    schema = {
        'people': meta({'id':'INT','name':'NVARCHAR','email':'NVARCHAR'}, ['id']),
        'purchases': meta({'id':'INT','person_id':'INT','amount':'DECIMAL','status':'VARCHAR'}, ['id'],
                          [{'columns':['person_id'],'table':'people','referred_columns':['id']}]),
        'items': meta({'id':'INT','purchase_id':'INT','product_id':'INT','line_amount':'DECIMAL'}, ['id'],
                     [{'columns':['purchase_id'],'table':'purchases','referred_columns':['id']},
                      {'columns':['product_id'],'table':'products','referred_columns':['id']}]),
        'products': meta({'id':'INT','name':'NVARCHAR','stock':'INT','price':'DECIMAL'}, ['id']),
    }
    return RetrievedSchema(schema, 'embeddings', [{'table':'products','score':0.5}])


def plan(tables, joins=()):
    return SelectionPlan(tables=[TableSelection(table=name,columns=columns) for name,columns in tables],
                         joins=list(joins), clarification=None)


def test_inventory_keeps_one_table_and_only_needed_columns(candidates):
    result=apply_selection(candidates, plan([('products',['name','stock'])]))
    assert set(result.schema) == {'products'}
    assert result.schema['products']['columns'] == {'name':'NVARCHAR','stock':'INT'}
    assert result.schema['products']['primary_key'] == []
    assert result.metadata()['candidate_table_count'] == 4
    assert result.metadata()['selected_table_count'] == 1
    assert result.metadata()['selected_column_count'] == 2
    assert set(candidates.schema['products']['columns']) == {'id','name','stock','price'}


def test_join_preserves_only_necessary_keys(candidates):
    join=JoinSelection(source_table='purchases',source_columns=['person_id'],target_table='people',target_columns=['id'])
    result=apply_selection(candidates, plan([('people',['name']),('purchases',['amount','status'])], [join]))
    assert set(result.schema) == {'people','purchases'}
    assert set(result.schema['people']['columns']) == {'id','name'}
    assert set(result.schema['purchases']['columns']) == {'person_id','amount','status'}
    assert result.schema['purchases']['primary_key'] == []
    assert result.schema['people']['primary_key'] == ['id']
    assert len(result.schema['purchases']['foreign_keys']) == 1


def test_join_chain_adds_bridge_table_without_extra_attributes(candidates):
    joins=[JoinSelection(source_table='items',source_columns=['purchase_id'],target_table='purchases',target_columns=['id']),
           JoinSelection(source_table='items',source_columns=['product_id'],target_table='products',target_columns=['id'])]
    result=apply_selection(candidates, plan([('purchases',['status']),('products',['name'])], joins))
    assert set(result.schema) == {'purchases','products','items'}
    assert set(result.schema['items']['columns']) == {'purchase_id','product_id'}
    assert 'people' not in result.schema


def test_independent_aggregates_do_not_force_relationships(candidates):
    result=apply_selection(candidates, plan([('purchases',['amount']),('products',['price'])]))
    assert set(result.schema) == {'purchases','products'}
    assert all(not meta['foreign_keys'] for meta in result.schema.values())


@pytest.mark.parametrize('tables', [[('invented',['id'])],[('products',['secret'])],
                                   [('products',['stock']),('products',['name'])],[]])
def test_invalid_identifiers_and_empty_selections_fail_closed(candidates, tables):
    with pytest.raises(InvalidSelection):
        apply_selection(candidates, plan(tables))


def test_invented_join_is_rejected(candidates):
    join=JoinSelection(source_table='purchases',source_columns=['id'],target_table='people',target_columns=['id'])
    with pytest.raises(InvalidSelection):
        apply_selection(candidates, plan([('people',['name'])],[join]))


def test_reverse_join_is_normalized_to_recorded_fk(candidates):
    join=JoinSelection(source_table='people',source_columns=['id'],target_table='purchases',target_columns=['person_id'])
    result=apply_selection(candidates,plan([('people',['name']),('purchases',['amount'])],[join]))
    assert result.schema['purchases']['foreign_keys'] == candidates.schema['purchases']['foreign_keys']
    assert set(result.schema['people']['columns']) == {'id','name'}


def test_shared_parent_join_restores_only_verified_bridge_keys():
    schema={'parent':meta({'id':'INT','private':'TEXT'},['id']),
            'a':meta({'parent_id':'INT','value':'INT'},foreign_keys=[{'columns':['parent_id'],'table':'parent','referred_columns':['id']}]),
            'b':meta({'parent_id':'INT','amount':'INT'},foreign_keys=[{'columns':['parent_id'],'table':'parent','referred_columns':['id']}])}
    candidates=RetrievedSchema(schema,'embeddings',[])
    join=JoinSelection(source_table='a',source_columns=['parent_id'],target_table='b',target_columns=['parent_id'])
    result=apply_selection(candidates,plan([('a',['value']),('b',['amount'])],[join]))
    assert set(result.schema) == {'a','b','parent'}
    assert result.schema['parent']['columns'] == {'id':'INT'}


def test_composite_join_keeps_every_key_column():
    schema={'parent':meta({'a':'INT','b':'INT','label':'TEXT'},['a','b']),
            'child':meta({'x':'INT','y':'INT','value':'INT'},foreign_keys=[
                {'columns':['x','y'],'table':'parent','referred_columns':['a','b']}])}
    candidates=RetrievedSchema(schema,'embeddings',[])
    join=JoinSelection(source_table='child',source_columns=['x','y'],target_table='parent',target_columns=['a','b'])
    result=apply_selection(candidates,plan([('parent',['label']),('child',['value'])],[join]))
    assert result.schema['parent']['primary_key'] == ['a','b']
    assert set(result.schema['child']['columns']) == {'x','y','value'}


def test_clarification_returns_no_schema(candidates):
    selected=apply_selection(candidates,SelectionPlan(tables=[],joins=[],clarification='Which measure?'))
    assert selected.schema == {} and selected.clarification == 'Which measure?'


def test_omitted_columns_are_rejected_by_sql_validation(candidates):
    selected=apply_selection(candidates,plan([('products',['name','stock'])]))
    with pytest.raises(UnsafeSQL):
        validate_sql('SELECT price FROM products',selected.schema,allow_wildcards=False)
    assert validate_sql('SELECT name FROM products WHERE stock<15',selected.schema,allow_wildcards=False)


@pytest.mark.parametrize('sql',['SELECT name FROM products ORDER BY price',
                              'SELECT name FROM products WHERE price>0',
                              'SELECT COUNT(*) FROM products GROUP BY price'])
def test_omitted_columns_are_rejected_in_all_clauses(candidates, sql):
    selected=apply_selection(candidates,plan([('products',['name'])]))
    with pytest.raises(UnsafeSQL):
        validate_sql(sql,selected.schema,allow_wildcards=False)


@pytest.mark.parametrize('sql',['SELECT * FROM products','SELECT p.* FROM products p',
                              'WITH p AS (SELECT * FROM products) SELECT name FROM p'])
def test_wildcards_cannot_restore_omitted_columns(candidates, sql):
    selected=apply_selection(candidates,plan([('products',['name'])]))
    with pytest.raises(UnsafeSQL,match='wildcard'):
        validate_sql(sql,selected.schema,allow_wildcards=False)
    assert validate_sql('SELECT COUNT(*) FROM products',selected.schema,allow_wildcards=False)


def test_selector_receives_only_rag_candidates(candidates, options, monkeypatch):
    captured={}
    class FakeClient:
        def __init__(self,**kwargs): self.responses=self
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def parse(self,**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_parsed=plan([('products',['name','stock'])]))
    monkeypatch.setattr('app.selection.OpenAI',FakeClient)
    db=SimpleNamespace(retriever=SimpleNamespace(retrieve=lambda _: candidates))
    selected=select_schema('Find low stock products',db,options)
    assert set(selected.schema) == {'products'}
    assert 'candidate_schema' in captured['input']


def test_clarification_skips_sql_generation(candidates, options, monkeypatch):
    from app.generation import generate
    monkeypatch.setattr('app.generation.select_schema',lambda *args: SelectedSchema({},candidates,'Which measure?'))
    monkeypatch.setattr('app.generation.OpenAI',lambda **kwargs: pytest.fail('Unexpected SQL generation call'))
    result=generate('Ambiguous question',None,options)
    assert result.sql is None and result.explanation == 'Which measure?'


def test_api_enforces_selected_columns(client, database_stub, monkeypatch):
    result=GeneratedQuery(sql='SELECT name FROM records',explanation='')
    result._schema={'records':{**database_stub.schema['records'],'columns':{'id':'INT'}}}
    monkeypatch.setattr('app.main.generate',lambda *args: result)
    response=client.post('/api/ask',json={'question':'Show ids'})
    assert response.status_code == 422
    database_stub.execute.assert_not_called()


def test_schema_only_endpoint_never_executes_sql(client, database_stub, monkeypatch):
    candidates=RetrievedSchema(database_stub.schema,'embeddings',[])
    selected=apply_selection(candidates,plan([('records',['id'])]))
    monkeypatch.setattr('app.main.select_schema',lambda *args: selected)
    response=client.post('/api/schema/select',json={'question':'Show ids'})
    assert response.status_code == 200
    assert response.json()['schema']['records']['columns'] == {'id':'INT'}
    assert response.json()['retrieval']['selected_column_count'] == 1
    database_stub.execute.assert_not_called()
