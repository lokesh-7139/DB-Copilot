"""Schema-only vector search, with a persistent local index and foreign-key paths."""
import hashlib
import json
import logging
import math
import os

import tempfile
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from openai import OpenAI, PermissionDeniedError, AuthenticationError

from app.config import ROOT

logger = logging.getLogger(__name__)
INDEX_VERSION = 1


class RetrievalUnavailable(ValueError):
    """A safe, user-facing retrieval failure without provider response details."""


def _unit(vector):
    if not vector or any(isinstance(v, bool) or not isinstance(v, (int, float))
                         or not math.isfinite(v) for v in vector):
        raise ValueError('Invalid embedding vector.')
    norm = math.sqrt(sum(v * v for v in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('Invalid embedding magnitude.')
    return [v / norm for v in vector]


def table_document(name, metadata, dialect, schema_name):
    # Embedding input is schema metadata only. Generation still receives the full metadata.
    document = json.dumps({'table': f'{schema_name or "main"}.{name}', 'dialect': dialect,
                           **metadata}, sort_keys=True, ensure_ascii=False)
    # <=6000 bytes also bounds tokens below the per-input embedding token limit.
    return document.encode('utf-8')[:6000].decode('utf-8', errors='ignore')


def relationship_context(schema, roots):
    """Connect ranked roots using undirected FK paths and include their FK ancestors."""
    graph = {name: set() for name in schema}
    for name, metadata in schema.items():
        for fk in metadata.get('foreign_keys', []):
            parent = fk['table']
            if parent in graph:
                graph[name].add(parent)
                graph[parent].add(name)
    selected = set(roots)
    for index, start in enumerate(roots):
        for target in roots[index + 1:]:
            queue = deque([(start, [start])])
            visited = {start}
            while queue:
                current, path = queue.popleft()
                if current == target:
                    selected.update(path)
                    break
                for neighbor in sorted(graph[current] - visited):
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))
    parents = deque(sorted(selected))
    while parents:
        name = parents.popleft()
        for fk in schema[name].get('foreign_keys', []):
            parent = fk['table']
            if parent in schema and parent not in selected:
                selected.add(parent)
                parents.append(parent)
    names = list(dict.fromkeys(roots + sorted(selected - set(roots))))
    return {name: schema[name] for name in names}


@dataclass
class RetrievedSchema:
    schema: dict
    mode: str
    matches: list

    def metadata(self):
        roots = {match['table'] for match in self.matches}
        return {'mode': self.mode, 'matched_tables': self.matches,
                'context_tables': list(self.schema),
                'relationship_tables': [name for name in self.schema if name not in roots]}


class SchemaRetriever:
    def __init__(self, schema, dialect, schema_name, settings, *, embedder=None, cache_dir=None):
        self.schema = schema
        self.settings = settings
        self.mode = 'embeddings'
        self.names = sorted(schema)
        self.documents = [table_document(name, schema[name], dialect, schema_name) for name in self.names]
        signature = json.dumps({'version': INDEX_VERSION, 'model': settings.embedding_model,
                                'schema': schema, 'dialect': dialect, 'namespace': schema_name},
                               sort_keys=True, ensure_ascii=False)
        self.fingerprint = hashlib.sha256(signature.encode('utf-8')).hexdigest()
        self.cache_file = Path(cache_dir or ROOT / 'data' / 'schema-index') / f'{self.fingerprint}.json'
        self.embedder = embedder or self._openai_embed
        self.vectors = None
        self.lock = Lock()
        self.question_cache = OrderedDict()

    def status(self):
        return {'mode': self.mode, 'embedding_model': self.settings.embedding_model if self.mode == 'embeddings' else None,
                'indexed_tables': len(self.names) if self.vectors is not None else 0,
                'top_k': self.settings.schema_top_k}

    def _openai_embed(self, texts):
        try:
            with OpenAI(api_key=self.settings.api_key, timeout=30, max_retries=1) as client:
                result = client.embeddings.create(model=self.settings.embedding_model,
                                                   input=texts, encoding_format='float')
            ordered = sorted(result.data, key=lambda entry: entry.index)
            if [entry.index for entry in ordered] != list(range(len(texts))):
                raise ValueError('Embedding response is incomplete.')
            return [entry.embedding for entry in ordered]
        except (PermissionDeniedError, AuthenticationError) as exc:
            raise RetrievalUnavailable(
                'Schema embedding access was denied. Check your API key and enable Write access '
                'to /v1/embeddings in its permissions.'
            ) from exc
        except Exception as exc:
            raise RetrievalUnavailable(
                'Schema embedding search is unavailable. Check API connectivity, credits and '
                'EMBEDDING_MODEL.'
            ) from exc

    def _load_or_build(self):
        if self.vectors is not None:
            return
        try:
            cached = json.loads(self.cache_file.read_text(encoding='utf-8'))
            if (cached['fingerprint'] != self.fingerprint or cached['tables'] != self.names
                    or len(cached['vectors']) != len(self.names)):
                raise ValueError('Stale index.')
            vectors = [_unit(vector) for vector in cached['vectors']]
            if len({len(vector) for vector in vectors}) > 1:
                raise ValueError('Inconsistent dimensions.')
            self.vectors = vectors
            return
        except (OSError, ValueError, TypeError, KeyError):
            pass
        vectors = []
        for offset in range(0, len(self.documents), 32):
            batch = self.documents[offset:offset + 32]
            embedded = self.embedder(batch)
            if len(embedded) != len(batch):
                raise RetrievalUnavailable('Schema embedding response is incomplete. Try again.')
            vectors.extend(_unit(vector) for vector in embedded)
        if len({len(vector) for vector in vectors}) > 1:
            raise RetrievalUnavailable('Schema embeddings have inconsistent dimensions.')
        self.vectors = vectors
        # Cache writes are atomic. If disk caching fails, this process can still use its index.
        temporary = None
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.cache_file.parent,
                                             suffix='.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({'fingerprint': self.fingerprint, 'tables': self.names,
                           'model': self.settings.embedding_model, 'vectors': vectors}, handle)
            os.replace(temporary, self.cache_file)
        except OSError:
            logger.warning('Schema index is in memory; persistent cache write failed.')
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def retrieve(self, question):
        if not self.schema:
            return RetrievedSchema({}, self.mode, [])
        count = min(self.settings.schema_top_k, len(self.names))
        # Serializes first build and coalesces repeated questions without persisting questions.
        with self.lock:
            self._load_or_build()
            if question in self.question_cache:
                vector = self.question_cache.pop(question)
            else:
                embeddings = self.embedder([question])
                if len(embeddings) != 1:
                    raise RetrievalUnavailable('Question embedding response is incomplete.')
                vector = _unit(embeddings[0])
            if len(vector) != len(self.vectors[0]):
                raise RetrievalUnavailable('Question and schema embedding dimensions do not match.')
            self.question_cache[question] = vector
            if len(self.question_cache) > 128:
                self.question_cache.popitem(last=False)
            scores = [(name, sum(a * b for a, b in zip(vector, stored)))
                      for name, stored in zip(self.names, self.vectors)]
        ranked = sorted(scores, key=lambda pair: (-pair[1], pair[0]))[:count]
        roots = [name for name, score in ranked]
        return RetrievedSchema(relationship_context(self.schema, roots), self.mode,
                               [{'table': name, 'score': round(score, 6)} for name, score in ranked])
