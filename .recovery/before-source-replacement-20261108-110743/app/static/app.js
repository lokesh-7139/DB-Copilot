const $ = (id) => document.getElementById(id);
let currentSQL = '';
let busy = false;

function cell(tag, value) {
  const el = document.createElement(tag);
  el.textContent = value === null ? 'NULL' : String(value);
  return el;
}

function schemaIcon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `schema-icon ${kind}`);
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', kind === 'chevron' ? 'm6 4 4 4-4 4' : 'M2.5 2.5h11v11h-11z M2.5 6h11 M6 6v7.5');
  svg.append(path);
  return svg;
}

function compactType(type) {
  return String(type).replace(/\s+COLLATE\b[\s\S]*$/i, '').trim().toLowerCase()
    .replace(/^integer\b/, 'int').replace(/^numeric\b/, 'decimal').replace(/,\s+/g, ',');
}

async function initialize() {
  try {
    const response = await fetch('/api/schema');
    if (!response.ok) throw new Error('Could not load database metadata.');
    const data = await response.json();
    $('database-name').textContent = data.database;
    $('connection-detail').textContent = 'SQL Server · Read-only access';
    $('sql-dialect').textContent = data.dialect;
    $('table-count').textContent = Object.keys(data.tables).length;
    for (const [name, meta] of Object.entries(data.tables)) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const count = Object.keys(meta.columns).length;
      summary.title = meta.description || `${name} · ${count} columns`;
      summary.append(schemaIcon('chevron'), schemaIcon('table'), cell('span', name));
      const countBadge = cell('small', count);
      countBadge.className = 'column-count';
      countBadge.setAttribute('aria-label', `${count} columns`);
      summary.append(countBadge);
      details.append(summary);
      const list = document.createElement('ul');
      const foreignKeys = new Set((meta.foreign_keys || []).flatMap((key) => key.columns));
      for (const [column, type] of Object.entries(meta.columns)) {
        const item = document.createElement('li');
        const identity = document.createElement('span');
        identity.className = 'column-identity';
        const name = cell('span', column);
        name.className = 'column-name';
        name.title = column;
        identity.append(name);
        const key = meta.primary_key.includes(column) ? 'PK' : foreignKeys.has(column) ? 'FK' : '';
        if (key) {
          const badge = cell('span', key);
          badge.className = `column-key ${key.toLowerCase()}`;
          badge.title = key === 'PK' ? 'Primary key' : 'Foreign key';
          identity.append(badge);
        }
        const typeLabel = cell('small', compactType(type));
        typeLabel.className = 'column-type';
        typeLabel.title = type;
        item.append(identity, typeLabel);
        list.append(item);
      }
      details.append(list);
      $('schema').append(details);
    }
  } catch (error) {
    $('database-name').textContent = 'Connection unavailable';
    $('connection-detail').textContent = 'Not connected';
    $('connection-error').textContent = 'Could not connect to your database. Refresh the page to try again.';
    $('connection-error').hidden = false;
    $('ask-button').disabled = true;
  }
}

$('question-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = $('question').value.trim();
  if (busy || question.length < 3) return;
  busy = true;
  $('ask-button').disabled = true;
  $('ask-button').textContent = 'Working…';
  $('answer').hidden = false;
  $('answer').setAttribute('aria-busy', 'true');
  $('sql-panel').hidden = true;
  $('results-panel').hidden = true;
  $('message').className = 'message';
  $('message').textContent = 'Finding your answer…';
  $('timing').textContent = '';
  $('schema-context').hidden = true;
  $('schema-context').open = false;
  currentSQL = '';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question}),
    });
    const data = await response.json();
    if (!('sql' in data)) throw new Error('The request was rejected. Check your question and try again.');
    currentSQL = data.sql || '';
    $('sql').textContent = currentSQL;
    $('sql-panel').hidden = !currentSQL;
    $('copy-sql').textContent = 'Copy SQL';
    $('timing').textContent = `${(data.elapsed_ms / 1000).toFixed(2)}s`;
    $('message').textContent = data.error || data.explanation;
    $('message').className = data.error ? 'message error' : 'message';
    if (data.retrieval) {
      const trace = data.retrieval;
      const selected = trace.selected_columns || {};
      $('schema-summary').textContent = `View schema used · ${trace.context_tables.length} tables · ${trace.selected_column_count ?? Object.values(selected).flat().length} columns`;
      $('schema-reduction').textContent = trace.candidate_column_count !== undefined
        ? `Selected from ${trace.candidate_table_count} candidate tables and ${trace.candidate_column_count} columns.` : '';
      $('selected-columns').replaceChildren();
      for (const table of trace.context_tables) {
        $('selected-columns').append(cell('li', `${table}: ${(selected[table] || []).join(', ')}`));
      }
      $('schema-context').hidden = false;
    }
    if (!data.error && currentSQL) {
      $('results-panel').hidden = false;
      $('row-count').textContent = `${data.row_count} row${data.row_count === 1 ? '' : 's'}`;
      $('table-container').replaceChildren();
      const table = document.createElement('table');
      const head = document.createElement('thead');
      const headerRow = document.createElement('tr');
      data.columns.forEach((column) => headerRow.append(cell('th', column)));
      head.append(headerRow);
      const body = document.createElement('tbody');
      data.rows.forEach((row) => {
        const tr = document.createElement('tr');
        row.forEach((value) => tr.append(cell('td', value)));
        body.append(tr);
      });
      table.append(head, body);
      $('table-container').append(table);
      $('result-note').textContent = data.truncated
        ? `Showing the first ${data.row_count} rows. Additional rows were omitted.`
        : data.row_count ? '' : 'No matching rows found.';
    }
  } catch (error) {
    $('message').className = 'message error';
    $('message').textContent = error.message || 'Unable to reach the server. Please try again.';
  } finally {
    busy = false;
    $('ask-button').disabled = false;
    $('ask-button').replaceChildren(document.createTextNode('Ask question '), cell('span', '↗'));
    $('answer').setAttribute('aria-busy', 'false');
  }
});

$('question').addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') $('question-form').requestSubmit();
});
$('copy-sql').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(currentSQL);
    $('copy-sql').textContent = 'Copied';
  } catch {
    $('copy-sql').textContent = 'Select SQL to copy';
  }
});
initialize();
