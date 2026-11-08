"""Request-local measurements; no questions, SQL, or result rows are recorded."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import json


active_metrics = ContextVar('active_metrics', default=None)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass
class RequestMetrics:
    calls: list = field(default_factory=list)
    caches: dict = field(default_factory=dict)

    def summary(self):
        def total(calls, key):
            values = [call[key] for call in calls]
            return sum(values) if all(value is not None for value in values) else None
        llm = [call for call in self.calls if call['kind'] == 'llm']
        embeddings = [call for call in self.calls if call['kind'] == 'embedding']
        return {'calls': self.calls, 'cache_hits': self.caches,
                'llm_calls': len(llm), 'embedding_calls': len(embeddings),
                'input_tokens': total(llm, 'input_tokens'),
                'output_tokens': total(llm, 'output_tokens'),
                'total_tokens': total(llm, 'total_tokens'),
                'embedding_tokens': total(embeddings, 'total_tokens')}


def begin_call(stage, *, schema=None, embedding=False):
    metrics = active_metrics.get()
    if metrics is None:
        return None
    call = {'stage': stage, 'kind': 'embedding' if embedding else 'llm',
            'input_tokens': None, 'output_tokens': None, 'total_tokens': None}
    if schema is not None:
        call['schema'] = {'tables': list(schema), 'table_count': len(schema),
                          'column_count': sum(len(meta['columns']) for meta in schema.values()),
                          'bytes': len(json.dumps(schema).encode('utf-8'))}
    metrics.calls.append(call)
    return call


def finish_call(call, response):
    if call is None:
        return
    usage = getattr(response, 'usage', None)
    if usage is None:
        return
    def get(name):
        return _count(usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None))
    if call['kind'] == 'embedding':
        call.update(input_tokens=get('prompt_tokens'), output_tokens=0, total_tokens=get('total_tokens'))
    else:
        call.update(input_tokens=get('input_tokens'), output_tokens=get('output_tokens'), total_tokens=get('total_tokens'))


def cache_hit(name):
    metrics = active_metrics.get()
    if metrics is not None:
        metrics.caches[name] = True
