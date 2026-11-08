import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Literal

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import ROOT, Settings
from app.database import Database
from app.generation import generate, generate_without_rag
from app.validation import UnsafeSQL, validate_sql
from app.retrieval import RetrievalUnavailable
from app.schema_context import retrieve_context
from app.metrics import RequestMetrics, active_metrics

logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    mode: Literal['rag', 'full'] = 'rag'


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = Database(settings)
        try:
            yield
        finally:
            app.state.db.engine.dispose()

    app = FastAPI(title='DB Copilot', lifespan=lifespan)
    app.mount('/static', StaticFiles(directory=ROOT / 'app' / 'static'), name='static')

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(ROOT / 'app' / 'static' / 'index.html')

    @app.get('/api/schema')
    def schema(force: bool = False):
        db = app.state.db.snapshot(force=force)
        return {'dialect': db.dialect,
                'database': db.engine.url.database or 'Connected database',
                'tables': db.schema,
                'max_rows': settings.max_rows, 'retrieval': db.retriever.status()}

    @app.post('/api/schema/refresh')
    def refresh_schema():
        try:
            return schema(force=True)
        except Exception:
            return JSONResponse({'error': 'Could not refresh database schema. Check database connectivity and read permissions.'}, status_code=502)

    @app.post('/api/ask')
    def ask(request: AskRequest):
        started = perf_counter()
        measurements = RequestMetrics()
        metrics_token = active_metrics.set(measurements)
        payload = {'question': request.question, 'sql': None, 'columns': [], 'rows': [],
                   'row_count': 0, 'truncated': False, 'explanation': '', 'error': None,
                   'retrieval': None, 'mode': request.mode}
        status = 200
        try:
            db = app.state.db.snapshot()
            generator = generate if request.mode == 'rag' else generate_without_rag
            generated = generator(request.question.strip(), db, settings)
            payload['retrieval'] = generated._retrieval or None
            payload['explanation'] = generated.explanation
            payload['sql'] = generated.sql
            if generated.sql:
                validation_schema = db.schema
                if generated._schema is not None:
                    validation_schema = {
                        name: {**meta, 'columns': {column: type_ for column, type_ in meta['columns'].items()
                                                 if column in db.schema[name]['columns']}}
                        for name, meta in generated._schema.items() if name in db.schema
                    }
                elif generated._retrieval:
                    validation_schema = {name: db.schema[name]
                                         for name in generated._retrieval['context_tables']
                                         if name in db.schema}
                sql = validate_sql(generated.sql, validation_schema,
                                   db.dialect, settings.db_schema,
                                   allow_wildcards=generated._schema is None)
                payload['sql'] = sql
                columns, rows, truncated = db.execute(sql)
                payload.update(columns=columns, rows=rows, row_count=len(rows), truncated=truncated)
        except UnsafeSQL as exc:
            status = 422
            payload['error'] = str(exc)
        except RetrievalUnavailable as exc:
            status = 502
            payload['error'] = str(exc)
        except Exception:
            # Never send database credentials, driver traces or provider secrets to the browser.
            logger.warning('Question failed; check database connectivity, query syntax and provider settings.')
            status = 502
            payload['error'] = (
                'Could not generate or execute this query. Check your provider and database '
                'settings, or rephrase the question. The query may also have exceeded its timeout.'
            )
        finally:
            active_metrics.reset(metrics_token)
        payload['elapsed_ms'] = round((perf_counter() - started) * 1000)
        payload['resources'] = measurements.summary()
        return JSONResponse(jsonable_encoder(payload), status_code=status)

    @app.post('/api/schema/select')
    def selected_schema(request: AskRequest):
        started = perf_counter()
        try:
            selected = retrieve_context(request.question.strip(), app.state.db.snapshot(), settings)
            return {'question': request.question, 'schema': selected.schema,
                    'retrieval': selected.metadata(), 'clarification': selected.clarification,
                    'elapsed_ms': round((perf_counter() - started) * 1000), 'error': None}
        except RetrievalUnavailable as exc:
            return JSONResponse({'schema': {}, 'error': str(exc)}, status_code=502)
        except Exception:
            return JSONResponse({'schema': {}, 'error': 'Could not select schema. Check API settings or rephrase your question.'}, status_code=502)

    return app

