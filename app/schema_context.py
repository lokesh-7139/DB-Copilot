"""Use the complete metadata of retrieved tables, without an LLM selection step."""
from dataclasses import dataclass

from app.retrieval import RetrievedSchema


@dataclass
class SchemaContext:
    schema: dict
    retrieved: RetrievedSchema
    clarification: str | None = None

    def metadata(self):
        columns = {name: list(meta['columns']) for name, meta in self.schema.items()}
        count = sum(map(len, columns.values()))
        return {**self.retrieved.metadata(),
                'stage': 'full_schema' if self.retrieved.mode == 'full_schema' else 'retrieved',
                'context_tables': list(self.schema),
                'selected_columns': columns,
                'selected_column_count': count}


def retrieve_context(question, database):
    retrieved = database.retriever.retrieve(question)
    if not retrieved.schema:
        return SchemaContext({}, retrieved, 'No accessible tables were found. Check the database and reader permissions.')
    # No column pruning: filters, PKs, FK keys, types and descriptions stay intact.
    return SchemaContext(retrieved.schema, retrieved)
