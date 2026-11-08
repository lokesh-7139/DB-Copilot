const $ = (id) => document.getElementById(id);
const results = {rag: null, full: null};
let activeMode = 'rag';
const field = (mode, id) => document.querySelector(`#${mode}-panel [data-field="${id}"]`);
for (const mode of ['rag', 'full']) {
  const panel = document.createElement('div');
  panel.id = `${mode}-panel`;
  panel.setAttribute('role', 'tabpanel');
  panel.setAttribute('aria-labelledby', `${mode}-tab`);
  panel.hidden = mode !== activeMode;
  panel.append($('answer-template').content.cloneNode(true));
  panel.querySelectorAll('[id]').forEach((element) => { element.dataset.field = element.id; element.removeAttribute('id'); });
  $('answer-panels').append(panel);
  field(mode, 'copy-sql').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(results[mode]?.sql || ''); field(mode, 'copy-sql').textContent = 'Copied'; }
    catch { field(mode, 'copy-sql').textContent = 'Select SQL to copy'; }
  });
}
function activate(mode) {
  activeMode = mode;
  for (const name of ['rag', 'full', 'compare']) {
    $(`${name}-panel`).hidden = name !== mode;
    $(`${name}-tab`).setAttribute('aria-selected', String(name === mode));
    $(`${name}-tab`).tabIndex = name === mode ? 0 : -1;
  }
}
for (const mode of ['rag', 'full', 'compare']) {
  $(`${mode}-tab`).addEventListener('click', () => activate(mode));
  $(`${mode}-tab`).addEventListener('keydown', (event) => {
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const visible = ['rag', 'full', 'compare'].filter((name) => !$(`${name}-tab`).hidden);
      const index = visible.indexOf(mode);
      const next = event.key === 'Home' ? visible[0] : event.key === 'End' ? visible[visible.length - 1]
        : visible[(index + (event.key === 'ArrowLeft' ? -1 : 1) + visible.length) % visible.length];
      activate(next); $(`${next}-tab`).focus();
    }
  });
}
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

async function initialize(refresh = false) {
  try {
    const response = await fetch(refresh ? '/api/schema/refresh' : '/api/schema', refresh ? {method: 'POST'} : {});
    if (!response.ok) throw new Error('Could not load database metadata.');
    const data = await response.json();
    $('database-name').textContent = data.database;
    $('connection-detail').textContent = 'SQL Server · Read-only access';
    for (const mode of ['rag', 'full']) field(mode, 'sql-dialect').textContent = data.dialect;
    $('schema').replaceChildren();
    $('connection-error').hidden = true;
    $('ask-button').disabled = busy;
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

async function askMode(mode, question) {
  results[mode] = null;
  const panel = $(`${mode}-panel`);
  panel.setAttribute('aria-busy', 'true');
  $(`${mode}-status`).textContent = 'Working…';
  for (const id of ['sql-panel', 'results-panel', 'schema-context']) field(mode, id).hidden = true;
  field(mode, 'schema-context').open = false;
  field(mode, 'timing').textContent = '';
  field(mode, 'message').className = 'message';
  field(mode, 'message').hidden = false;
  field(mode, 'message').textContent = 'Finding your answer…';
  try {
    const response = await fetch('/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question, mode}),
    });
    const data = await response.json();
    if (!('sql' in data)) throw new Error('The request was rejected. Check your question and try again.');
    results[mode] = data;
    const currentSQL = data.sql || '';
    field(mode, 'sql').textContent = currentSQL;
    field(mode, 'sql-panel').hidden = !currentSQL;
    field(mode, 'copy-sql').textContent = 'Copy SQL';
    field(mode, 'timing').textContent = `${(data.elapsed_ms / 1000).toFixed(2)}s`;
    field(mode, 'message').textContent = data.error || (!currentSQL ? data.explanation : '');
    field(mode, 'message').hidden = !data.error && Boolean(currentSQL);
    field(mode, 'message').className = data.error ? 'message error' : 'message';
    if (data.retrieval) {
      const trace = data.retrieval;
      const selected = trace.selected_columns || {};
      field(mode, 'schema-summary').textContent = `View schema sent · ${trace.context_tables.length} tables · ${trace.selected_column_count ?? Object.values(selected).flat().length} columns`;
      field(mode, 'schema-reduction').textContent = trace.candidate_column_count !== undefined
        ? (mode === 'full' ? 'Full database schema sent directly, without retrieval or schema selection.' : `Selected from ${trace.candidate_table_count} candidate tables and ${trace.candidate_column_count} columns.`) : '';
      field(mode, 'selected-columns').replaceChildren();
      for (const table of trace.context_tables) {
        field(mode, 'selected-columns').append(cell('li', `${table}: ${(selected[table] || []).join(', ')}`));
      }
      field(mode, 'schema-context').hidden = false;
    }
    if (!data.error && currentSQL) {
      field(mode, 'results-panel').hidden = false;
      field(mode, 'row-count').textContent = `${data.row_count} row${data.row_count === 1 ? '' : 's'}`;
      field(mode, 'table-container').replaceChildren();
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
      field(mode, 'table-container').append(table);
      field(mode, 'result-note').textContent = data.truncated
        ? `Showing the first ${data.row_count} rows. Additional rows were omitted.`
        : data.row_count ? '' : 'No matching rows found.';
    }

    $(`${mode}-status`).textContent = data.error ? 'Error' : data.sql ? 'Ready' : 'Clarify';
  } catch (error) {
    field(mode, 'message').hidden = false;
    field(mode, 'message').className = 'message error';
    field(mode, 'message').textContent = error.message || 'Unable to reach the server. Please try again.';
    $(`${mode}-status`).textContent = 'Error';
  } finally {
    panel.setAttribute('aria-busy', 'false');
  }
}

function renderComparison(question) {
  $('comparison-question').textContent = question;
  const number = (value) => typeof value === 'number' ? value.toLocaleString() : 'Unavailable';
  const stage = (data, name) => data?.resources?.calls?.find((call) => call.stage === name);
  const sum = (data, key) => {
    const calls = data?.resources?.calls;
    if (!calls) return null;
    return calls.reduce((total, call) => total + (call.schema?.[key] || 0), 0);
  };
  const rows = [
    ['Outcome', (d) => !d ? 'Request failed' : d.error ? 'Error' : d.sql ? 'Answer returned' : 'Clarification'],
    ['End-to-end time', (d) => typeof d?.elapsed_ms === 'number' ? `${(d.elapsed_ms / 1000).toFixed(2)} s` : 'Unavailable'],
    ['Tables sent to SQL generation', (d) => number(stage(d, 'generation')?.schema?.table_count)],
    ['Columns sent to SQL generation', (d) => number(stage(d, 'generation')?.schema?.column_count)],
    ['Tables sent to schema selection', (d) => d?.mode === 'full' ? 'Not used' : number(stage(d, 'selection')?.schema?.table_count)],
    ['Schema payload across LLM calls', (d) => { const bytes = sum(d, 'bytes'); return bytes === null ? 'Unavailable' : `${(bytes / 1024).toFixed(2)} KiB`; }],
    ['Selection input tokens', (d) => d?.mode === 'full' ? 'Not used' : number(stage(d, 'selection')?.input_tokens)],
    ['SQL generation input tokens', (d) => number(stage(d, 'generation')?.input_tokens)],
    ['All LLM input tokens', (d) => number(d?.resources?.input_tokens)],
    ['All LLM output tokens', (d) => number(d?.resources?.output_tokens)],
    ['All LLM tokens', (d) => number(d?.resources?.total_tokens)],
    ['Embedding tokens', (d) => number(d?.resources?.embedding_tokens)],
    ['LLM calls', (d) => number(d?.resources?.llm_calls)],
    ['Embedding calls', (d) => number(d?.resources?.embedding_calls)],
    ['Embedding cache hits', (d) => d?.mode === 'full' ? 'Not used' : d?.resources ? Object.entries(d.resources.cache_hits).filter(([,hit]) => hit).map(([name]) => name === 'schema_embeddings' ? 'Schema index' : 'Question').join(', ') || 'None' : 'Unavailable'],
  ];
  const table = document.createElement('table');
  const head = document.createElement('thead');
  const header = document.createElement('tr');
  ['Resource', 'With RAG', 'Without RAG'].forEach((label) => header.append(cell('th', label)));
  head.append(header); table.append(head);
  const body = document.createElement('tbody');
  for (const [label, value] of rows) {
    const tr = document.createElement('tr');
    tr.append(cell('th', label), cell('td', value(results.rag)), cell('td', value(results.full)));
    body.append(tr);
  }
  table.append(body); $('resource-table').append(table);
  for (const mode of ['rag', 'full']) {
    const details = document.createElement('details');
    details.append(cell('summary', mode === 'rag' ? 'Schema sent with RAG' : 'Schema sent without RAG'));
    const calls = results[mode]?.resources?.calls?.filter((call) => call.schema) || [];
    if (!calls.length) details.append(cell('p', 'No schema payload measurements available.'));
    for (const call of calls) {
      details.append(cell('p', `${call.stage === 'selection' ? 'Schema selection' : 'SQL generation'}: ${call.schema.tables.join(', ')}`));
    }
    $('schema-comparison').append(details);
  }
}

$('question-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const question = $('question').value.trim();
  if (busy || question.length < 3) return;
  busy = true;
  if (activeMode === 'compare') activate('rag');
  $('compare-tab').hidden = true;
  $('compare-panel').hidden = true;
  $('resource-table').replaceChildren();
  $('schema-comparison').replaceChildren();
  $('ask-button').disabled = true;
  $('refresh-schema').disabled = true;
  $('ask-button').textContent = 'Comparing…';
  $('answer').hidden = false;
  $('answer').setAttribute('aria-busy', 'true');
  try {
    // Two independent HTTP requests; neither answer is supplied to the other.
    await Promise.allSettled([askMode('rag', question), askMode('full', question)]);
  } finally {
    busy = false;
    renderComparison(question);
    $('compare-tab').hidden = false;
    $('ask-button').disabled = false;
    $('refresh-schema').disabled = false;
    $('ask-button').replaceChildren(document.createTextNode('Ask question '), cell('span', '↗'));
    $('answer').setAttribute('aria-busy', 'false');
  }
});
$('question').addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') $('question-form').requestSubmit();
});
$('refresh-schema').addEventListener('click', async () => {
  if (busy) return;
  $('refresh-schema').disabled = true;
  $('refresh-schema').textContent = 'Refreshing…';
  $('ask-button').disabled = true;
  try { await initialize(true); }
  finally { $('refresh-schema').disabled = false; $('refresh-schema').textContent = 'Refresh'; }
});
initialize();
