"""Application settings, loaded from the environment (see `.env.example`)."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderMode = Literal["mock", "live"]

# The repo-root `.env` (the one `.env.example` documents) is read wherever the process starts;
# a `.env` in the working directory still overrides it.
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Typed runtime configuration. Values come from environment variables / `.env`."""

    model_config = SettingsConfigDict(
        env_file=(_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # a blank `KEY=` line keeps the default instead of blanking it
    )

    # postgresql+psycopg://user:pass@host:port/db  (ADR-0002)
    database_url: str = Field(
        default="postgresql+psycopg://investilens:devpassword@localhost:5432/investilens"
    )
    embedding_dimension: int = Field(default=1024)  # voyage-finance-2 (ADR-0010)
    # mock = frozen fixtures (offline, CI default, DR-004); live = real providers (later phases).
    provider_mode: ProviderMode = Field(default="mock")

    # --- SEC EDGAR ingestion (Phase 1a) ---
    # Descriptive UA with contact is mandatory (LGL-002). Not a personal address by default.
    sec_user_agent: str = Field(default="AssayDesk/0.1 (contact: dev@assay-desk.example)")
    sec_rate_limit_per_sec: float = Field(default=5.0)  # SEC fair-access ceiling (LGL-002)
    # Local object store root for raw filings (ING-008). S3/MinIO backend is Phase 5d (ADR-0012).
    storage_dir: str = Field(default=".storage")
    # Backfill policy (ING-002), configurable per deployment.
    backfill_10k_years: int = Field(default=5)
    backfill_10q_quarters: int = Field(default=12)
    backfill_8k_months: int = Field(default=24)  # 8-K and 8-K/A
    backfill_proxy_years: int = Field(default=3)  # DEF 14A, DEFA14A (FR-EX-010)
    backfill_nt_years: int = Field(default=3)  # NT 10-K, NT 10-Q late-filing notices

    # --- Prices: Tiingo (Phase 1d, ADR-0007) ---
    tiingo_api_key: str = Field(default="")  # required only when Tiingo is the price source
    # Overrides PROVIDER_MODE for prices alone (ADR-0035), so real prices need only this one key.
    price_provider: Literal["mock", "tiingo"] | None = Field(default=None)
    # Local stand-in for the price-eod schedule: catch stale prices up when the API starts.
    # Leave off wherever Celery Beat runs, or every API instance would refetch at boot.
    refresh_prices_on_startup: bool = Field(default=False)
    price_rate_limit_per_sec: float = Field(default=2.0)  # hourly cap handled by low volume+cache
    backfill_price_years: int = Field(default=5)  # 5y daily history for valuation bands

    # --- News: Finnhub (Phase 1e, ADR-0008) ---
    finnhub_api_key: str = Field(default="")  # required only when Finnhub is the news source
    # Overrides PROVIDER_MODE for news alone (ADR-0038), so real news needs only this one key.
    news_provider: Literal["mock", "finnhub"] | None = Field(default=None)
    news_rate_limit_per_sec: float = Field(default=1.0)  # Finnhub free: 60/min
    news_backfill_days: int = Field(default=30)  # recent window; free tier has 1y history
    news_relevance_threshold: float = Field(default=0.3)  # min score shown by default
    news_dup_cosine_threshold: float = Field(default=0.9)  # near-duplicate clustering
    news_dup_window_days: int = Field(default=3)  # cluster only within this time window

    # --- Embeddings: Voyage AI (Phase 2b, ADR-0010/0013) ---
    voyage_api_key: str = Field(default="")  # required only when PROVIDER_MODE=live
    embedding_model: str = Field(default="voyage-finance-2")  # ADR-0010
    embedding_batch_size: int = Field(default=128)  # Voyage caps a request at 128 inputs (EMB-002)
    embedding_rate_limit_per_sec: float = Field(default=3.0)  # Voyage free tier: 3 req/sec
    # Blended $/1M tokens for cost logging (EMB-002). Voyage voyage-finance-2 list price; an
    # estimate used only for the `llm_calls` cost column, never for financial output.
    embedding_cost_per_1m_tokens: Decimal = Field(default=Decimal("0.12"))
    # FR-EX-013: embed EX-99 press releases and proxy statements too. Off by default (ADR-0058): a
    # DEF 14A is ~1-2k chunks (about $0.10-0.30 a filing at Gemini's embedding price), and the
    # first beat run on a fresh setup would embed the whole backlog. Off, they stay keyword-only.
    embed_exhibits: bool = Field(default=False)
    # HNSW query-time breadth (EMB-004). Higher = better recall, slower. Tuned in docs/rag.md.
    hnsw_ef_search: int = Field(default=100)

    # --- RAG retrieval pipeline (Phase 2c, §16, ADR-0003/0014) ---
    rag_candidate_top_n: int = Field(default=40)  # candidates per index before fusion (RAG-010)
    rag_rerank_top_k: int = Field(default=10)  # final evidence count after rerank (RAG-012)
    rag_rrf_k: int = Field(default=60)  # Reciprocal Rank Fusion constant (RAG-011)
    rag_rerank_enabled: bool = Field(default=True)  # off = fusion order only, for latency tests
    # Minimum top reranker score to proceed; below this the pipeline abstains (RAG-018). Tuned to
    # the LexicalReranker scale (ADR-0014): zero query-term overlap floors near 0.35, so 0.4
    # abstains on irrelevant hits while any real term overlap clears it. Re-tune per reranker.
    rag_sufficiency_min_score: float = Field(default=0.4)
    rag_max_chunks_per_document: int = Field(default=3)  # diversity cap (RAG-017)
    rag_context_token_budget: int = Field(default=6000)  # context-assembly budget (RAG-020)
    rag_max_tool_calls: int = Field(default=6)  # tool-call budget per question (RAG-031)
    rag_tool_timeout_s: float = Field(default=10.0)  # per-tool wall-clock timeout (RAG-031)
    # Model routing (RAG-050): cheap for classify/rewrite, strong reserved for Phase 3 synthesis.
    llm_model_cheap: str = Field(default="gemini-3.5-flash-lite")
    llm_model_strong: str = Field(default="gemini-3.8-flash")

    # --- LLM: Google Gemini (ADR-0032) ---
    gemini_api_key: str = Field(default="")  # required only when the Gemini client is selected
    # Overrides PROVIDER_MODE for the LLM alone, so a real model can run over fixture data.
    llm_provider: Literal["mock", "gemini"] | None = Field(default=None)
    llm_rate_limit_per_sec: float = Field(default=0.25)  # free-tier pacing; raise on a paid key
    llm_thinking_level: str = Field(default="low")  # Gemini thinkingLevel; "default" sends none
    # Same idea for embeddings (ADR-0033): overrides PROVIDER_MODE for the embedder alone.
    embedding_provider: Literal["mock", "voyage", "gemini"] | None = Field(default=None)
    gemini_embedding_model: str = Field(default="gemini-embedding-2")

    # --- Citation & verification (Phase 3a, §13/§14, ADR-0015) ---
    # Rounding tolerance for the deterministic numeric-match layer (CIT-005 L2): a cited number
    # matches a source value within this relative tolerance after unit normalization.
    citation_numeric_rel_tolerance: float = Field(default=0.01)
    # Lexical-entailment overlap thresholds (CIT-005 L3): ≥ supported, ≥ partial, else unsupported.
    citation_entail_supported_overlap: float = Field(default=0.6)
    citation_entail_partial_overlap: float = Field(default=0.3)
    # Policy for a "partially" entailed claim: "soften" (downgrade confidence) or "reject".
    citation_partial_policy: str = Field(default="soften")
    # Confidence weights (HAL-002); sum need not be 1 (score is clamped to [0,1]).
    citation_w_tier: float = Field(default=0.25)
    citation_w_sources: float = Field(default=0.2)
    citation_w_retrieval: float = Field(default=0.15)
    citation_w_entailment: float = Field(default=0.3)
    citation_w_agreement: float = Field(default=0.1)
    # Label thresholds (HAL-002): ≥ strong → "strongly_supported"; ≥ supported → "supported".
    citation_label_strong: float = Field(default=0.8)
    citation_label_supported: float = Field(default=0.55)

    # --- Chat / Q&A (Phase 3c, §10.14, §16.8) ---
    # Bounded multi-turn memory window: how many prior messages inform a follow-up.
    chat_context_window_messages: int = Field(default=6)

    # --- Authentication & users (Phase 4b, §26.1, ADR-0005) ---
    # HS256 signing secret. Dev-only default; MUST be overridden via env in any shared/prod
    # deployment (SEC-001). Never logged.
    jwt_secret: str = Field(default="dev-only-insecure-change-me-32-bytes-minimum-secret")
    jwt_algorithm: str = Field(default="HS256")
    access_token_ttl_seconds: int = Field(default=900)  # 15 min access token (AUTH-002)
    refresh_token_ttl_days: int = Field(default=14)  # rotating refresh token (AUTH-002)
    # A token presented again this soon after it was rotated is a second tab restoring the same
    # session, not theft: it gets a token of its own instead of revoking the family (ADR-0030).
    # 0 turns the allowance off.
    refresh_reuse_grace_seconds: int = Field(default=10, ge=0)
    password_min_length: int = Field(default=12)  # basic policy (AUTH-001)
    email_token_ttl_hours: int = Field(default=24)  # verify/reset tokens (AUTH-003)
    # Login throttle (AUTH-006): max failed attempts per (account|IP) within the window, then a
    # lockout. In-process like the rate-limit seam (ADR-0018); Phase 5c moves it to Redis.
    auth_login_max_attempts: int = Field(default=5)
    auth_login_window_seconds: int = Field(default=300)
    auth_login_lockout_seconds: int = Field(default=300)
    # Refresh cookie (AUTH-002): httpOnly/Secure/SameSite, scoped to the auth path.
    refresh_cookie_name: str = Field(default="refresh_token")
    refresh_cookie_path: str = Field(default="/api/v1/auth")
    refresh_cookie_secure: bool = Field(default=True)  # relaxed to False only for local http dev

    # --- API endpoints (Phase 4a, §24.1) ---
    api_default_page_size: int = Field(default=50)  # cursor pagination default (API-002, ADR-0016)
    api_max_page_size: int = Field(default=200)  # hard cap on ?limit=
    # Rate-limit seam (ADR-0018). Off by default; real limits + Redis land below (Phase 5c).
    rate_limit_enabled: bool = Field(default=False)
    rate_limit_per_minute: int = Field(default=120)

    # --- Observability (Phase 5c, §20, §28, ADR-0021) ---
    # Unset = console span exporter (no infra needed); set to a collector URL for a real deploy.
    otel_exporter_otlp_endpoint: str = Field(default="")
    # Alert thresholds (OBS-003), evaluated by app/observability/alerts.py.
    alert_ingestion_lag_minutes: int = Field(default=60)
    alert_job_failure_rate_threshold: float = Field(default=0.2)  # fraction of recent jobs failed
    alert_job_failure_window: int = Field(default=20)  # how many recent jobs to look at
    alert_sec_429_count_threshold: int = Field(default=5)  # retryable-429 log lines in the window
    alert_error_rate_threshold: float = Field(default=0.05)  # fraction of recent requests erroring
    # Global circuit-breaker signal (OBS-003), distinct from the per-user ai_budget_month_usd cap.
    llm_daily_budget_usd: Decimal = Field(default=Decimal("25.00"))

    # --- CORS & security headers (Phase 5c, §29, SEC-005/012) ---
    cors_allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    # HSTS is only meaningful (and only sent) over a real TLS deployment; disabled for local http.
    hsts_enabled: bool = Field(default=False)

    # --- Rate limiting & AI budget (Phase 5c, §25.4, SEC-008, ADR-0022) ---
    # "memory" (default, existing in-process token bucket — deterministic in tests/CI, ADR-0018)
    # or "redis" (real cross-worker limiting via REDIS_URL, ADR-0022).
    rate_limit_backend: Literal["memory", "redis"] = Field(default="memory")
    redis_url: str = Field(default="redis://localhost:6379/0")
    rate_limit_ai_per_hour: int = Field(default=20)  # spec §25.4 example, applied to chat/research
    # New users get this monthly AI budget (NFR-008); existing NULL rows mean "unlimited" (never
    # auto-backfilled — an operator decision, not a migration's).
    default_ai_budget_month_usd: Decimal = Field(default=Decimal("5.00"))

    # --- LLM cost estimation (Phase 5c, OBS-002/005) ---
    # Blended $/1M tokens, same spirit as embedding_cost_per_1m_tokens above: an estimate for the
    # `llm_calls.cost_usd` column only, never financial output. Gemini paid-tier list prices
    # (ADR-0032; the strong tier doubles on 2027-01-01). Free-tier calls cost nothing but are
    # still counted at list price, so per-user budgets keep limiting usage.
    llm_cost_per_1m_input_tokens_cheap: Decimal = Field(default=Decimal("0.30"))
    llm_cost_per_1m_output_tokens_cheap: Decimal = Field(default=Decimal("2.50"))
    llm_cost_per_1m_input_tokens_strong: Decimal = Field(default=Decimal("0.75"))
    llm_cost_per_1m_output_tokens_strong: Decimal = Field(default=Decimal("3.75"))

    # --- Privacy & retention (Phase 5c, §29, SEC-014) ---
    chat_message_retention_days: int = Field(default=365)

    # --- Background jobs: Celery (Phase 5d, §25.1-2, JOB-001..006, ADR-0024) ---
    # Broker + result backend both ride on Redis (one dependency). Default to the same local
    # Redis as the cache/rate-limiter; a real deploy points these at managed Redis.
    celery_broker_url: str = Field(default="redis://localhost:6379/1")
    celery_result_backend: str = Field(default="redis://localhost:6379/2")
    # Per-queue soft/hard task time limits (JOB-004). Report generation is the long pole.
    celery_task_soft_time_limit_s: int = Field(default=600)
    celery_task_time_limit_s: int = Field(default=660)
    celery_max_retries: int = Field(default=3)  # JOB-001 max attempts per task
    # Off in tests/CI/local-without-worker: `POST /research` and admin refresh create the `jobs`
    # row exactly as Phase 4a did, without dispatching to a broker. On in a real deploy (a worker
    # is running) so the enqueued job actually executes. Mirrors the cache/rate-limit seams.
    background_jobs_enabled: bool = Field(default=False)
    # EDGAR new-filing poll cadence for watched/seed companies (≤10 min target, §21.3).
    edgar_poll_interval_minutes: int = Field(default=10)
    news_poll_interval_minutes: int = Field(default=15)

    # --- Redis cache (Phase 5d, §25.3, CACHE-001..005, ADR-0024) ---
    cache_enabled: bool = Field(default=False)  # off in tests/CI; on in a real deploy
    cache_key_version: str = Field(default="v1")  # bump to invalidate everything (CACHE-001)
    cache_ttl_company_seconds: int = Field(default=86400)  # metadata ~24h
    cache_ttl_prices_seconds: int = Field(default=3600)  # intra-day; EOD refresh invalidates
    cache_ttl_metrics_seconds: int = Field(default=86400)  # until data_version changes
    cache_ttl_news_seconds: int = Field(default=600)  # 5-15 min window
    cache_ttl_research_seconds: int = Field(default=86400)  # popular reports
    cache_singleflight_lock_seconds: int = Field(default=10)  # stampede lock TTL (CACHE-003)

    # --- Notifications / alert dispatch (Phase 5d, §26.2, ADR-0025) ---
    # "log" (default — no email provider configured; delivery is logged + persisted in-app) or
    # "smtp" (real email via the SMTP_* settings below).
    notification_email_backend: Literal["log", "smtp"] = Field(default="log")
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_user: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="alerts@assay-desk.example")
    # Alert framework v2 (Phase 7a, ADR-0039). The price-move threshold is now a per-alert param.
    alert_max_per_user: int = Field(default=50)  # keeps the 5-minute dispatch loop bounded
    alert_digest_threshold: int = Field(default=20)  # more events than this in a run -> 1 digest

    # --- Filing diff (Phase 6b, ADR-0027) ---
    # "lexical" (word-bigram overlap; offline default) or "embedding" (cosine via EmbeddingClient).
    # The two thresholds below are tuned for "lexical" on NVDA 10-Ks; re-tune if you switch.
    diff_similarity: Literal["lexical", "embedding"] = Field(default="lexical")
    diff_match_threshold: float = Field(default=0.45)  # >= this: same passage, reworded
    diff_borderline_threshold: float = Field(default=0.20)  # below this: unrelated passages
    diff_max_llm_adjudications: int = Field(default=8)  # LLM calls per diff, borderline pairs only
    # System default peer sets (Phase 7h, FR-PS-002): same SIC, market cap within this band of the
    # target's, at most this many members.
    peer_market_cap_band_low: float = Field(default=0.25)
    peer_market_cap_band_high: float = Field(default=4.0)
    peer_set_max_default: int = Field(default=8)

    # --- Earnings briefs (Phase 7j, FR-EB-012) ---
    # Briefs call the model, so they are made only for watched companies and these seed tickers,
    # at most `earnings_brief_daily_cap` new versions a day (UTC) across all companies, and a
    # user may ask for at most `earnings_brief_regen_per_hour` regenerations an hour.
    earnings_brief_seed_tickers: list[str] = Field(default=["NVDA", "AAPL", "JPM"])
    earnings_brief_daily_cap: int = Field(default=50)
    earnings_brief_regen_per_hour: int = Field(default=5)
    earnings_preview_lead_days: int = Field(default=3)

    # --- Thesis tracker (Phase 7k, FR-TH-010) ---
    thesis_draft_per_hour: int = Field(default=10)  # "Draft from my notes" calls per user

    # --- Object storage (Phase 5d, ADR-0012) ---
    # "filesystem" (MVP default, storage_dir) or "s3" (MinIO locally / S3 in prod, via the
    # object_storage_* settings). The ObjectStorage interface is unchanged either way.
    object_storage_backend: Literal["filesystem", "s3"] = Field(default="filesystem")
    object_storage_endpoint: str = Field(default="localhost:9000")  # MinIO; omit scheme
    object_storage_access_key: str = Field(default="")
    object_storage_secret_key: str = Field(default="")
    object_storage_bucket: str = Field(default="investilens-filings")
    object_storage_secure: bool = Field(default=False)  # True for real S3/https

    # --- Demo mode (Phase 5d scope #12, wired in Phase 7l, ADR-0050) ---
    # When on: registration is off and visitors share one sandbox account reset nightly; the LLM
    # and embedder replay recorded/offline output only; rate limits are on and keyed by IP.
    demo_mode: bool = Field(default=False)
    # Largest request body accepted on any route, in bytes (NFR-ME-009). The biggest legitimate
    # body is a 20,000-character thesis (≤ 80 KB as UTF-8), so 256 KiB leaves ample room.
    max_request_bytes: int = Field(default=262_144)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
