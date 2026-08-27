---
name: Bug report
about: Something behaves differently from how it is documented
title: ''
labels: bug
assignees: ''
---

**What happened**

<!-- What you did, what you saw. -->

**What you expected**

**How to reproduce**

```bash
# the request, command or click sequence
```

**Which part**

- [ ] Ingestion / retrieval (`app/rag/`)
- [ ] A tool (`rag_lookup`, `stac_search`, `compute_index`)
- [ ] The agent loop or streaming (`app/agents/`)
- [ ] The web interface (`frontend/`)
- [ ] The MCP server (`app/mcp/`)
- [ ] Deployment (`infra/`)
- [ ] Something else

**Environment**

- How you are running it: <!-- docker compose / local uvicorn / deployed / MCP client -->
- `CLAUDE_MODEL` and `STAC_API_URL` if you changed them from the defaults:
- Optional extras installed: <!-- none / mcp / observability -->

**Logs**

<!-- `docker compose logs api | grep eo_rag.trace` is usually the useful one.
     Please redact API keys and account ids. -->
