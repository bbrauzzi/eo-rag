# Security

## Reporting a vulnerability

Please report security issues privately through
[GitHub's private vulnerability reporting](https://github.com/bbrauzzi/eo-rag/security/advisories/new)
rather than opening a public issue.

Include what you did, what happened, and what you expected. A proof of concept helps. You
should get an acknowledgement within a few days; this is a small project maintained in
spare time, so please allow reasonable time for a fix before disclosing publicly.

## What this software does and does not protect

EO-RAG is a self-hosted application. **Deploying it exposes an endpoint that spends money
on your behalf** — every `/ask` request buys Claude tokens and Bedrock embeddings. Read
this section before putting it on a public address.

### There is no authentication

Nothing in this repository authenticates or authorizes callers. `/ask`, `/ask/stream`,
`/preview`, `/items` and `/mcp` are all open to anyone who can reach the port. The
Terraform in `infra/` deploys exactly that: an internet-facing load balancer with no
authentication and no TLS.

That is an accepted trade for a short-lived demo behind a URL you share deliberately. It is
**not** suitable for anything else. Before running this anywhere durable, put
authentication in front of it — an authenticating proxy, an ALB authentication action, an
API gateway — and terminate TLS.

### What the built-in controls actually bound

Two mechanisms exist and they solve different problems. Neither is an access control.

**The conversation budget** (`MAX_CONVERSATION_TURNS`, `MAX_CONVERSATION_COST_USD`) is
keyed on a `conversation_id` **the client chooses**. A caller that omits it, or sends a
fresh one every request, is bounded by nothing. It exists to stop an honest conversation
running away, not to stop an attacker.

It also bounds an *estimate*: prices in `app/agents/cost.py` are a transcribed copy of
published list prices that nothing verifies at runtime. Your bill is what your provider
says it is. An unrecognised `CLAUDE_MODEL` is deliberately priced at the most expensive
model known, so a misconfiguration ends conversations early rather than disabling the cap.

**The rate limiter** (`app/api/ratelimit.py`) is keyed on the peer address, which is the
part the client does not choose. Note three limits on it:

- It is **in process**. It dies with the process, and each worker keeps its own tally — N
  workers means an effective limit of N x the configured value.
- `X-Forwarded-For` is **ignored** unless `RATE_LIMIT_TRUST_PROXY_HEADER=true`, because a
  header the client sets is a header the client can forge. Only turn it on when a proxy you
  control terminates the connection. When it is on, the rightmost entry is used.
- `/health` and the static UI are **deliberately unlimited**.

**The step cap** (`MAX_AGENT_STEPS`) bounds one turn, not a conversation and not a caller.

### Server-side request forgery

The preview and asset endpoints fetch remote URLs, so they are the obvious SSRF surface.
The containment is that **they take an item id, never a URL**: the href is resolved through
`fetch_item` against the configured `STAC_API_URL`, so the only thing either endpoint can
fetch is what that catalog returned for that id.

Consequently, **`STAC_API_URL` is a trusted input**. Pointing it at a catalog you do not
trust gives that catalog control over what your server will fetch. Keep
`ALLOWED_COLLECTIONS` populated.

`/preview` additionally refuses anything that is not a browser-renderable image type and
caps the response body. `/items/{id}/assets/{key}` has **no size cap** by design — a
Sentinel-1 band is legitimately hundreds of megabytes — so it can be used to pull
significant bandwidth through your instance. That is what its rate-limit tier is for.

### Prompt injection

Retrieved document chunks and catalog responses are placed into the model's context. If you
ingest a document you do not control, or point at a catalog you do not control, treat
anything the model says or does afterwards as influenced by that content. The tools
available to the model are read-only and narrow, which bounds the damage, but the answer
text itself should not be treated as trustworthy in that situation.

### Secrets

- `.env` is gitignored and must stay that way. `.env.example` carries placeholders only.
- `infra/env.sh` is gitignored; `infra/env.sh.example` is the template.
- Terraform state, `*.tfvars` and `.terraform/` are gitignored. **State files contain
  secrets in plaintext** — the S3 backend is configured with versioning and encryption for
  that reason.
- In the deployed stack the database password is a `random_password` and both it and the
  Anthropic API key live in Secrets Manager, injected into the task rather than baked into
  the image.
- Those secrets use `recovery_window_in_days = 0`, so `undeploy.sh` really deletes them and
  the names can be reused immediately. That means **no recovery window** — deletion is
  final. Reconsider before using this stack for anything long-lived.

### Local development defaults

`docker-compose.yml` uses `eorag` / `eorag` as the Postgres username and password, and the
database port is published on the host. These are convenience defaults for local
development. **Never deploy them.** The Terraform stack does not use them.
