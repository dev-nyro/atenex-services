import asyncpg
from sqlalchemy import create_engine, MetaData, Column, Uuid as SqlAlchemyUuid, Integer, Text, String, DateTime, UniqueConstraint, ForeignKeyConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from typing import Optional
from app.core.config import settings
import json
import structlog

log = structlog.get_logger(__name__)

_db_pool: Optional[asyncpg.Pool] = None
_sync_engine: Optional[Engine] = None
_sync_engine_dsn: Optional[str] = None

async def _init_asyncpg_connection(conn):
    log.debug("Initializing asyncpg connection with type codecs.")
    await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog', format='text')
    await conn.set_type_codec('json', encoder=json.dumps, decoder=json.loads, schema='pg_catalog', format='text')
    log.debug("Asyncpg type codecs set for json/jsonb.")

async def get_db_pool() -> asyncpg.Pool:
    global _db_pool
    if _db_pool is None or _db_pool._closed:
        dsn = f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        log.info("Creating PostgreSQL async connection pool.", dsn_host=settings.POSTGRES_SERVER, db_name=settings.POSTGRES_DB)
        try:
            _db_pool = await asyncpg.create_pool(
                dsn=dsn,
                init=_init_asyncpg_connection,
                min_size=2,
                max_size=10,
                timeout=30.0,
                command_timeout=60.0,
                statement_cache_size=0 
            )
            log.info("PostgreSQL async connection pool created successfully.")
        except Exception as e:
            log.critical("Failed to create PostgreSQL async connection pool", error=str(e), exc_info=True)
            _db_pool = None 
            raise
    return _db_pool

async def close_db_pool():
    global _db_pool
    if _db_pool and not _db_pool._closed:
        log.info("Closing PostgreSQL async connection pool.")
        await _db_pool.close()
        _db_pool = None
        log.info("PostgreSQL async connection pool closed.")

def get_sync_engine() -> Engine:
    global _sync_engine, _sync_engine_dsn
    if _sync_engine is None:
        if not _sync_engine_dsn:
            _sync_engine_dsn = f"postgresql+psycopg2://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
        log.info("Creating SQLAlchemy synchronous engine.", dsn_host=settings.POSTGRES_SERVER, db_name=settings.POSTGRES_DB)
        try:
            _sync_engine = create_engine(
                _sync_engine_dsn,
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
                pool_recycle=1800,
                json_serializer=json.dumps,
                json_deserializer=json.loads
            )
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            log.info("SQLAlchemy synchronous engine created and tested successfully.")
        except Exception as e:
            log.critical("Failed to create SQLAlchemy synchronous engine", error=str(e), exc_info=True)
            _sync_engine = None
            raise
    return _sync_engine

def dispose_sync_engine():
    global _sync_engine
    if _sync_engine:
        log.info("Disposing SQLAlchemy synchronous engine pool.")
        _sync_engine.dispose()
        _sync_engine = None
        log.info("SQLAlchemy synchronous engine pool disposed.")

metadata_obj = MetaData()

document_chunks_table = Table(
    'document_chunks',
    metadata_obj,
    Column('id', SqlAlchemyUuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column('document_id', SqlAlchemyUuid(as_uuid=True), nullable=False),
    Column('company_id', SqlAlchemyUuid(as_uuid=True), nullable=False),
    Column('chunk_index', Integer, nullable=False),
    Column('content', Text, nullable=False),
    Column('metadata', JSONB),
    Column('embedding_id', String(255)),
    Column('created_at', DateTime(timezone=True), server_default=text("timezone('utc', now())")),
    Column('vector_status', String(50), default='pending'),
    UniqueConstraint('document_id', 'chunk_index', name='uq_document_chunk_index'),
    ForeignKeyConstraint(['document_id'], ['documents.id'], name='fk_document_chunks_document', ondelete='CASCADE'),
    Index('idx_document_chunks_document_id', 'document_id'),
    Index('idx_document_chunks_company_id', 'company_id'),
    Index('idx_document_chunks_embedding_id', 'embedding_id'),
)