import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify


class UnsafeSQL(ValueError):
    pass


# An intentionally small allowlist. Unknown/user-defined functions require review.
SAFE_FUNCTIONS = {
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'ROUND', 'ABS', 'COALESCE', 'NULLIF',
    'LOWER', 'UPPER', 'LENGTH', 'TRIM', 'LTRIM', 'RTRIM', 'CONCAT', 'SUBSTRING',
    'CAST', 'TRY_CAST', 'EXTRACT', 'DATE_TRUNC', 'TIMESTAMP_TRUNC', 'TIME_TO_STR',
    'STR_TO_TIME', 'DATE', 'DATETIME', 'STRFTIME', 'CURRENT_DATE', 'CURRENT_TIMESTAMP',
    'DATE_ADD', 'DATE_SUB', 'TS_OR_DS_ADD', 'TS_OR_DS_TO_DATE', 'TS_OR_DS_TO_TIMESTAMP',
    'ROW_NUMBER', 'RANK', 'DENSE_RANK', 'LAG', 'LEAD', 'FIRST_VALUE', 'LAST_VALUE',
    'IF', 'CASE', 'GREATEST', 'LEAST', 'CEIL', 'FLOOR',
    'YEAR', 'MONTH', 'DAY', 'DATE_FROM_PARTS', 'EOMONTH', 'LAST_DAY',
    'DATEDIFF', 'DATE_DIFF', 'TIMESTAMP_DIFF', 'TIME_TO_TIME_STR', 'TIME_STR_TO_TIME',
    'AND', 'OR', 'NOT', 'EXISTS',
}


def validate_sql(sql: str, schema: dict, dialect: str = 'tsql', db_schema: str = 'dbo', *, allow_wildcards: bool = True) -> str:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement is not None]
    except sqlglot.errors.ParseError as exc:
        raise UnsafeSQL('The generated SQL could not be parsed.') from exc
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeSQL('Only a single read-only SELECT query is allowed.')
    tree = statements[0]
    if not allow_wildcards:
        for star in tree.find_all(exp.Star):
            if not (isinstance(star.parent, exp.Count) and star.parent.this is star):
                raise UnsafeSQL('List the selected columns explicitly; wildcard projections are not allowed.')
    forbidden = {'insert', 'update', 'delete', 'merge', 'create', 'drop', 'alter',
                 'command', 'copy', 'into', 'lock', 'withtablehint', 'transaction', 'grant', 'revoke'}
    for node in tree.walk():
        if node.key in forbidden:
            raise UnsafeSQL('SQL contains a forbidden operation.')
        if isinstance(node, exp.Func):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else node.sql_name()
            if name not in SAFE_FUNCTIONS:
                raise UnsafeSQL(f'Function {name} is not allowed.')
        if isinstance(node, exp.Dot):
            raise UnsafeSQL('Qualified function calls are not allowed.')
    ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    resolved = []
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeSQL('Table functions are not allowed.')
        if table.catalog:
            raise UnsafeSQL('Queries may only access the connected database.')
        if not table.db and table.name in ctes:
            resolved.append(None)
            continue
        exact = f'{table.db}.{table.name}' if table.db else table.name
        key = exact if exact in schema else None
        if key is None and table.db == db_schema and table.name in schema:
            key = table.name  # Compatibility for unqualified schema fixtures.
        if key is None and not table.db:
            matches = [name for name, meta in schema.items()
                       if meta.get('table', name.rsplit('.', 1)[-1]) == table.name]
            if len(matches) > 1:
                raise UnsafeSQL(f'Table {table.name} is ambiguous; specify its schema.')
            if len(matches) == 1:
                key = matches[0]
        if key is None:
            raise UnsafeSQL(f'Table {table.name} is not available.')
        resolved.append(key)
        if schema[key].get('schema'):
            # Execute with explicit schema even when the model omitted it.
            table.set('db', exp.to_identifier(schema[key]['schema']))
    # Resolve CTE scopes, aliases and columns, rejecting unknown/ambiguous columns.
    type_schema = {name: value['columns'] for name, value in schema.items()}
    resolution_tree = tree.copy()
    for table, key in zip(resolution_tree.find_all(exp.Table), resolved):
        if key is not None:
            if not table.alias:
                table.set('alias', exp.TableAlias(this=exp.to_identifier(table.name)))
            table.set('this', exp.to_identifier(key, quoted=True))
            table.set('db', None)
    try:
        qualify(resolution_tree, dialect=dialect, schema=type_schema,
                validate_qualify_columns=True, quote_identifiers=False)
    except sqlglot.errors.OptimizeError as exc:
        raise UnsafeSQL('SQL references an unknown or ambiguous column.') from exc
    # Return the exact normalized SQL that will be executed and shown to the user.
    return tree.sql(dialect=dialect, pretty=True)
