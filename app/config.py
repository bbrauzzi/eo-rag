from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://eorag:eorag@localhost:5432/eorag"

    # LLM
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # Embeddings (Amazon Bedrock - credentials from the boto3 default chain)
    aws_region: str = "us-east-1"
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    embedding_dim: int = 1024

    # STAC
    stac_api_url: str = "https://earth-search.aws.element84.com/v1"

    # What `stac_search` will ask the catalog for. Defaults to the collections Earth
    # Search v1 actually advertises, so nothing that worked before is refused; an empty
    # list turns the check off, which is what a different catalog wants until its own
    # ids are listed here. Kept in configuration rather than in the tool because it is
    # a property of the deployment's catalog, not of the code.
    allowed_collections: list[str] = [
        "cop-dem-glo-30",
        "cop-dem-glo-90",
        "landsat-c2-l2",
        "naip",
        "sentinel-1-grd",
        "sentinel-2-c1-l2a",
        "sentinel-2-l1c",
        "sentinel-2-l2a",
        "sentinel-2-pre-c1-l2a",
    ]

    # App
    max_agent_steps: int = 5  # guardrail: hard cap on tool calls per conversation

    # Per-conversation budget. The step cap bounds one *turn*; these bound the whole
    # thread, which nothing did before - a conversation could be continued indefinitely,
    # each turn resending a history that only grows. Either at 0 disables that check.
    max_conversation_turns: int = 20
    max_conversation_cost_usd: float = 1.00

    # Per-client rate limiting (`app/api/ratelimit.py`). The budget above is keyed on a
    # conversation id the client picks, so a caller sending a new one every request is
    # bounded by nothing; this is keyed on the caller. Two tiers because the endpoints
    # cost different things: /ask spends money on a model, the proxies spend bandwidth.
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: float = 60.0
    rate_limit_ask_per_minute: int = 10
    rate_limit_proxy_per_minute: int = 120

    # Its own tier rather than sharing /ask's: one MCP session is many requests that are
    # not tool calls - initialize, tools/list, resources/list, resources/templates/list -
    # so at 10/minute the handshake alone would trip the limiter and the client would
    # report a broken server.
    rate_limit_mcp_per_minute: int = 60

    # Off by default because a header the client sets is a header the client can forge:
    # trusting X-Forwarded-For with no proxy in front lets anyone bypass the limiter by
    # varying it. Turn it on only when something you control terminates the connection.
    rate_limit_trust_proxy_header: bool = False

    # Ceiling on how many clients are tracked at once, so the limiter's own table cannot
    # be grown without bound by requests from many addresses.
    rate_limit_max_tracked_clients: int = 10_000

    # Observability. Tracing itself is unconditional and goes to the `eo_rag.trace`
    # logger; Langfuse is an exporter on top of it, off unless both keys are set and the
    # optional `observability` extra is installed. `langfuse_enabled` is the switch for
    # turning the exporter off without removing credentials.
    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # MCP. The stdio transport needs none of this - it is a subprocess a client launches,
    # and `eo-rag-mcp` is all it takes. These only govern the HTTP transport mounted at
    # /mcp by `app/main.py`, which is off with the switch or when the optional `mcp` extra
    # is not installed.
    mcp_http_enabled: bool = True

    # The SDK's streamable HTTP app validates the Host header and refuses anything but
    # localhost by default, which is DNS-rebinding protection and is right for a laptop.
    # Behind a real hostname it refuses everything, and the symptom looks like a broken
    # server rather than a policy - so the allowlists are here, empty by default so that
    # the safe behaviour is what you get without deciding anything.
    mcp_allowed_hosts: list[str] = []
    mcp_allowed_origins: list[str] = []

    @field_validator("database_url")
    @classmethod
    def _force_psycopg3(cls, value: str) -> str:
        """
        SQLAlchemy resolves a driverless 'postgresql://' URL to psycopg2, which we do
        not depend on: pin those URLs to psycopg v3 instead of failing on import.
        """
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return f"postgresql+psycopg://{value[len(prefix):]}"
        return value


settings = Settings()
