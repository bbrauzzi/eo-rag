# Live verification

The test suite runs fully offline by design: no AWS, no network, no database. That bar
is worth keeping, but it means a whole class of bug is structurally invisible to it —
the external service behaving differently from how we imagined it. Three were found this
way and none could have been caught by a unit test:

- Earth Search rejects `2024-01-01` with a 400; a bare date sent as an *instant* matches
  only scenes acquired exactly at midnight, returning zero results with no error.
- `MAX_TOKENS` was still at step 2's value of 1000, so an answer combining two tools was
  cut off mid-sentence with nothing in the response to say so.
- Earth Search declares `offset: -0.1` on every sentinel-2-l2a item, but the
  sentinel-cogs COGs behind it hold **unshifted** DNs. Applying the offset as advertised
  put 68% of the red band at negative reflectance and sent NDVI to -4.8e11. The offline
  tests could not see it because they invent the metadata and the pixels together, so
  they always agree — see step 9.

Run this after changing the request shape of a tool, the graph, or the prompts.

Steps 1-2 cost nothing. Steps 3-7 each spend a handful of Claude tokens (fractions of a
cent) plus one Titan embedding call. Step 9 spends no tokens but downloads real pixels.

---

## Prerequisites

```bash
docker compose up -d
curl -s http://localhost:8000/health          # {"status":"ok"}
```

No image rebuild is needed unless dependencies changed: `./app` is bind-mounted and
uvicorn runs with `--reload`. When they *do* change, `--reload` is actively dangerous —
it picks up the new import against the old site-packages and the API dies at startup,
which is exactly how step 5 took the service down (`ModuleNotFoundError: No module named
'numpy'`). Rebuild:

```bash
docker compose up -d --build api
```

Confirm the container picked up your edits:

```bash
docker logs eo-rag-api --tail 5               # look for "WatchFiles detected changes"
```

`.env` must carry `ANTHROPIC_API_KEY` and the AWS keys. Note that `~/.aws` is **not**
mounted into the container, so a credentials file that works on the host does nothing
inside it — boto3 there resolves from the environment only.

Check the key and the configured model without spending anything:

```bash
uv run python -c "
import httpx
from app.config import settings
r = httpx.get('https://api.anthropic.com/v1/models?limit=100',
              headers={'x-api-key': settings.anthropic_api_key,
                       'anthropic-version': '2023-06-01'}, timeout=30)
ids = [m['id'] for m in r.json()['data']]
print(settings.claude_model, 'valid:', settings.claude_model in ids)
"
```

---

## Step 1 — Retrieval, no LLM (free)

Isolates embeddings + pgvector. This is what `scripts/retrieve_test.py` is for.

```bash
docker compose exec api python -m scripts.retrieve_test --top-k 3
```

**Expect** `Indexed chunks: <n>` and similarities above ~0.5 on the on-topic questions.
If this passes, any later failure is in the loop or in Claude, not in retrieval.

From the host instead of the container, override the URL — `.env` points at `db:5432`:

```bash
DATABASE_URL="postgresql://eorag:eorag@localhost:5432/eorag" uv run python -m scripts.retrieve_test
```

## Step 2 — Bedrock in isolation (only if step 1 fails)

```bash
docker compose exec api python -c "
from app.rag.embeddings import embed_text
print('ok, dim', len(embed_text('hello')))
"
```

**Expect** `ok, dim 1024`. A `RuntimeError` naming `AccessDeniedException` means model
access is not enabled for your `AWS_REGION` in the Bedrock console.

## Step 3 — The `rag_lookup` path

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the required fields of a STAC Item?"}' | python3 -m json.tool
```

**Expect** `sources` to contain the ingested document name (`stac-spec-core`). An answer
with empty `sources` means no tool was called and the model answered from memory —
a prompt problem, not a code problem.

## Step 4 — The `stac_search` path

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?"}' \
  | python3 -m json.tool
```

**Expect** `sources` to contain the catalog URL, and real scene identifiers in the
answer. This is the query that used to fail with a 400 before dates were normalized.

Cross-check the identifiers against the catalog directly, bypassing the model entirely:

```bash
uv run python -c "
from app.tools.stac_search import stac_search
r = stac_search(bbox=[12.35,41.75,12.65,42.0], datetime='2024-01-01/2024-01-31',
                collections=['sentinel-2-l2a'], limit=3, max_cloud_cover=20)
print(r['count'], [i['id'] for i in r['items']])
"
```

## Step 5 — Both tools in one conversation

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the assets field of a STAC Item, and which assets does a real Sentinel-2 scene over Rome actually have?"}' \
  | python3 -m json.tool
```

**Expect** *both* sources. This is the most informative check: the model chooses to chain
the two tools on its own.

Read the end of the answer, not just the start — a reply cut off mid-sentence means it
hit `MAX_TOKENS` in `app/agents/graph.py`, and nothing in the response signals that.

## Step 6 — Conversational memory

The capability added in step 4, and the one the offline tests can only approximate.
Ask a follow-up that is meaningless without history — a pronoun, no context repeated:

```bash
CID=$(curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 scenes cover Rome in January 2024 with less than 20% cloud?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["conversation_id"])')

curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d "{\"question\": \"Which of those has the least cloud, and what is its exact acquisition time?\", \"conversation_id\": \"$CID\"}" \
  | python3 -m json.tool
```

**Expect** the second answer to resolve "those" against the first, without searching
again, and `sources` to be **empty** — no tool ran that turn, which is correct.

Then confirm isolation by asking the same follow-up with no `conversation_id`: a fresh
thread must say it has no previous results to refer to.

Remember the history is in-process. `docker compose restart api` wipes every
conversation, and so does a `--reload` triggered by editing a file under `app/`.

## Step 7 — The step cap

The cap does not trigger on ordinary questions, so force it. In `.env`:

```
MAX_AGENT_STEPS=1
```

```bash
docker compose up -d --force-recreate api   # env_file is read at start; --reload does NOT reload it
```

Re-run step 5. The model gets a single round of tools and is then forced to conclude
with what it has. **Expect** a complete answer, not an error. Restore `MAX_AGENT_STEPS=5`
afterwards.

## Step 8 — Datetime handling against the live catalog

The regression that motivated most of this file:

```bash
uv run python -c "
from app.tools.stac_search import stac_search
BBOX = [12.35, 41.75, 12.65, 42.0]
for label, dt in [('bare interval', '2024-01-01/2024-01-31'),
                  ('single day', '2024-01-30'),
                  ('open ended', '2024-01-01/..'),
                  ('no datetime', None)]:
    r = stac_search(bbox=BBOX, collections=['sentinel-2-l2a'], limit=3,
                    max_cloud_cover=20, **({'datetime': dt} if dt else {}))
    print(f'{label:15} count={r[\"count\"]}')
try:
    stac_search(bbox=BBOX, datetime='not-a-date')
except RuntimeError as e:
    print('rejected ok:', str(e)[:80])
"
```

**Expect** a non-zero count on all four, and the malformed one to raise a `RuntimeError`
carrying the catalog's own message. A count of 0 on `single day` is the silent-failure
mode: it means bare dates are reaching the catalog as instants again.

## Step 9 — The `compute_index` path

The only tool that reads pixels, and the one with the widest gap between what the offline
suite proves and what the live data does. Run it from inside the container: the host may
not have `rasterio`, and GDAL's HTTP egress is what is being checked.

The item is re-resolved through `stac_search` rather than hardcoded, so a catalog change
shows up as a search failure instead of a confusing raster failure.

```bash
docker compose exec api python -c "
import time
from app.tools.stac_search import stac_search
from app.tools.compute_index import compute_index, MAX_PIXELS

r = stac_search(bbox=[12.35,41.75,12.65,42.0], datetime='2024-01-01/2024-01-31',
                collections=['sentinel-2-l2a'], limit=1, max_cloud_cover=20)
item = r['items'][0]['id']
print('item:', item)

def show(label, *a, **k):
    t = time.perf_counter(); res = compute_index(*a, **k); dt = time.perf_counter() - t
    s = res['statistics']
    print(f'{label:20} {dt:5.1f}s res={res[\"resolution_m\"]}m px={res[\"pixels\"][\"read\"]} '
          f'offset_applied={res[\"reflectance\"][\"offset_applied\"]}')
    print(f'{\"\":20} mean={s[\"mean\"]:+.4f} med={s[\"median\"]:+.4f} '
          f'range=[{s[\"min\"]:+.4f}, {s[\"max\"]:+.4f}]')
    assert -1 <= s['min'] <= s['max'] <= 1, 'OUT OF RANGE'

show('ndvi suburb', item, [12.45, 41.85, 12.50, 41.90])
show('ndvi lake',   item, [12.21, 42.09, 12.27, 42.14])
show('ndwi lake',   item, [12.21, 42.09, 12.27, 42.14], index='ndwi')
show('ndvi whole tile', item, [12.0, 41.5, 13.1, 42.4])
print('MAX_PIXELS =', MAX_PIXELS)
"
```

**Expect**, in order of what each line tells you:

- **In range.** Every statistic within [-1, 1] — the `assert` above is the whole point. A
  normalized difference cannot leave that interval, so a value outside it means the
  reflectance conversion is wrong, not that the scene is unusual. This is the check that
  would have caught the offset bug, and the only one whose failure is unmistakable.
- **The suburb is green-ish**: NDVI mean around +0.3, median +0.24, at `res=10.0m`.
- **The lake flips sign**: NDVI negative (about -0.12) and NDWI clearly positive (about
  +0.60) over the same water. The cheapest proof the two bands are not swapped — no
  arithmetic error survives producing the right sign on both indices at once.
- **`offset_applied=False`** on Earth Search, on every scene. That is the tool overriding
  the catalog's `offset: -0.1` because the pixels contradict it. Seeing `True` here means
  either the metadata changed or `_offsets_fit_the_pixels` stopped working — check the
  range line before trusting the numbers.
- **The whole tile decimates and stays fast**: `res` of 40 m, `px` at or under
  `MAX_PIXELS`, and *not slower* than the full-resolution suburb read. That is GDAL
  serving the read from the COGs' internal overviews, which is why the pixel cap is the
  only guardrail the tool needs — decimating a huge window does not mean downloading one.

To confirm the offset really is contradicted rather than merely unhelpful, look at the
DNs. Under a genuinely applied +1000 BOA shift, reflectance zero *is* DN 1000, so no
valid pixel can sit below it:

```bash
docker compose exec api python -c "
import numpy as np, rasterio
from app.tools.stac_search import fetch_item
from app.tools.compute_index import _read_window, _band_scaling, GDAL_ENV

item = fetch_item('S2B_33TTG_20240130_0_L2A')
for k in ('nir', 'red'):
    a = item['assets'][k]; s, o, nd = _band_scaling(a)
    with rasterio.Env(**GDAL_ENV):
        d, _ = _read_window(a['href'], [12.45, 41.85, 12.50, 41.90])
    d = np.ma.masked_equal(d, nd).compressed()
    print(f'{k}: declared offset={o} DN min={d.min():.0f} '
          f'DN<1000={100 * float((d < 1000).mean()):.1f}% '
          f'-> refl {np.median(d) * s + o:+.4f} vs {np.median(d) * s:+.4f} unshifted')
"
```

**Expect** `DN<1000` far above zero — measured 18% for `nir` and 68% for `red`, with
minima of DN 88 and 54. Anything near 0% would mean this catalog had started serving
shifted pixels, and the check would then correctly apply the offset instead.

Finally, the chain the tool exists for, through HTTP:

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How green was the vegetation around Rome at the end of January 2024? Find a low-cloud Sentinel-2 scene and measure NDVI over a small area."}' \
  | python3 -m json.tool
```

**Expect** two entries in `sources`: the bare catalog URL from `stac_search` and the same
URL suffixed with the item id from `compute_index`. The prose must quote the tool's own
figures — an NDVI stated without the item id in `sources` was invented.

---

## Seeing which tools ran

Nothing logs tool calls yet — that is roadmap step 8. Until then `sources` is the only
external signal, and it works because each tool contributes a distinct provenance:

| `sources` contains | means |
|---|---|
| the ingested document name | `rag_lookup` ran |
| the `STAC_API_URL` value, bare | `stac_search` ran |
| the `STAC_API_URL` value followed by an item id in parentheses | `compute_index` ran |
| nothing | no tool ran, or every call failed |

`compute_index`'s provenance is deliberately not the bare URL: the pixels of one named
scene are what the numbers came from, and the id is what makes them reproducible. So the
two catalog tools stay distinguishable in a single answer that used both.

For the round count, bypass HTTP and call the loop directly:

```bash
docker compose exec api python -c "
from app.agents.graph import answer_question
from app.db.session import SessionLocal
r = answer_question(SessionLocal(), 'Which Sentinel-2 scenes cover Rome in January 2024?')
print('steps  :', r.steps)
print('sources:', r.sources)
print('thread :', r.conversation_id)
print(r.text)
"
```

`steps == 1` means the model answered without touching a tool.

---

## Step 10 — The streaming endpoint and the map

The offline suite cannot see either half of this. It fakes the graph to test the SSE
framing and fakes the catalog to test the footprints, so it never observes whether frames
actually *arrive progressively*, nor whether the coordinates a real catalog returns end up
the right way round on a real map.

**10a — the stream, frame by frame**

```bash
curl -N -X POST http://localhost:8000/ask/stream -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 L2A scenes cover Rome between 1 and 31 January 2024 with less than 20% cloud?"}'
```

`-N` is what makes this check mean anything: without it curl buffers, and a stream is
indistinguishable from a slow 200.

**Expect** a `start` frame first, carrying a `conversation_id`; a `tool_start` /
`tool_end` pair for `stac_search` with `"ok": true` and its own `ms`; exactly one
`features` frame holding as many features as the answer names scenes — **and arriving
before the first `token` of the answer**, which is what puts the footprints on the map
while the prose is still being written; then `token` frames spread over several seconds;
then `done`, whose `sources` matches what `/ask` returns for the same question.

**10b — the geometry stays out of the model's context**

The whole point of the split, and the one thing no offline test can confirm, because the
offline fixtures invent the geometry and the projection together.

```bash
docker compose exec api python -c "
from app.agents.graph import _run_tool
content, sources, features = _run_tool('stac_search',
    {'bbox': [12.35,41.75,12.65,42.0], 'collections': ['sentinel-2-l2a'], 'limit': 2}, None)
print('geometry in the model view :', 'geometry' in content)
print('model view bytes           :', len(content))
print('footprints                 :', len(features), features[0]['geometry']['type'])
print('first coordinate           :', features[0]['geometry']['coordinates'][0][0])
"
```

**Expect** `False`, two `Polygon` footprints, and a first coordinate near
`[12.x, 41.x]`. A pair reading `[41.x, 12.x]` is the lat/lon inversion — it would draw
the footprints in the Gulf of Guinea without anything failing.

**10c — the interface**

```bash
cd frontend && npm install && npm run dev     # http://localhost:5173, proxied to :8000
```

Ask the question from 10a. **Expect** the tokens to arrive word by word rather than in
one block; the tool chip to go from a pulsing dot to a green check with its elapsed time;
the footprints to appear *before* the answer finishes, and the camera to fly to them
**once**.

Then ask **"which of those has the least cloud?"** — **expect no `features` frame at all
and the footprints to stay exactly where they are**, for the same reason `sources` is
empty there (step 6): no tool ran. Hover a card and the matching polygon brightens; click
one and the camera frames that scene alone. Switch the basemap to Imagery: the footprints
stay drawn and the Esri attribution appears next to OpenFreeMap's.

**10d — stopping a turn**

The check that matters is not the stop itself but the turn *after* it.

Ask something long, wait until prose is actually flowing, press **Stop**. **Expect** the
partial answer to stay on screen, a quiet grey "Stopped." rather than the red error box,
and the composer to re-enable.

Then, **on the same conversation**, ask something else. **Expect a normal answer.** A
`400 ... tool_use ids were found without tool_result blocks` here means
`_repair_interrupted_turn` is not running: the stop abandoned the graph between `agent`
and `tools`, and the thread is now unusable for good. Press Stop again on that second turn
too — it must still work, which is what says the interrupted turn did not disarm it.

Finally, press **New chat** mid-stream and immediately ask something different. **Expect
the new answer to open with its own first words**, not with a fragment of the abandoned
one — that buffer is flushed on a frame, and discarding it is what keeps it from surfacing
at the top of the next turn.

**10e — the quicklooks**

Click a footprint on the map. **Expect** the scene's quicklook to appear inside it, with
the coastline in the image lining up with the coastline on the basemap — a visible offset
or a mirrored image means `imageCorners` ordered the corners wrong, which nothing else
reports. Click it again and it goes away; the badge counts what is drawn. Turn on two
overlapping tiles and check the cyan outlines are still drawn **over** the images.

Then the proxy those images come through, which is the whole reason they load at all:

```bash
curl -s -o /dev/null -w 'status=%{http_code} type=%{content_type} bytes=%{size_download}\n' \
  http://localhost:8000/preview/S2B_33TTG_20240130_0_L2A
curl -s -o /dev/null -w 'status=%{http_code}\n' http://localhost:8000/preview/not-a-real-item
```

**Expect** `200 image/jpeg` with the same byte count as fetching the catalog's href
directly, and `404` for an id the catalog does not know. In the browser, every `<img>`
`src` and every quicklook URL should read `/preview/…` — **nothing** should be requested
from the asset host directly. A `sentinel-cogs.s3…` URL in the network tab means
something bypassed the proxy, and it will work until it meets a catalog that sends no
CORS headers (see `app/api/preview.py` for the full reasoning).

**10f — the asset downloads**

Ask for Sentinel-1 scenes — that is the case a Sentinel-2-only check cannot see, because
S1 publishes every asset as `s3://` and S2 does not. Click **⤓ assets** on a card.

**Expect** the popover to open *over the map*, fully inside the viewport, listing every
asset with its key, title and type. Two things it is testing at once: the strip is
`overflow-x-auto`, so a popover rendered inside the card would be clipped to 168px, and
the card is a `<button>`, so a nested `<a>` would not activate at all — a list that opens
but whose rows do nothing means the portal is gone. Open the one on the **last** card
too, and on a card near the bottom of the window: it must flip and stay on screen.

Click an asset. **Expect** a download named after the scene, not after the band:

```bash
ID=S1A_IW_GRDH_1SDV_20230130T165925_20230130T165950_047016_05A3B9
curl -s "http://localhost:8000/items/$ID/assets" | head -c 200
curl -s -o /dev/null -D - "http://localhost:8000/items/$ID/assets/safe-manifest" \
  | grep -i 'content-disposition\|content-length'
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:8000/items/$ID/assets/nir"
```

**Expect** `attachment; filename="…_safe-manifest.safe"` and `404` for a key the item does
not have. Then the one that only a real band shows — `HEAD` is not proxied, so ask for the
`vv` band and interrupt it:

```bash
curl -s -o /dev/null -D - --max-time 3 "http://localhost:8000/items/$ID/assets/vv"
```

**Expect** the headers to come back with a `content-length` of several hundred megabytes
(721,276,476 when this was written) *immediately*, seconds before the body could possibly
have arrived. If it hangs instead, the response is being buffered rather than streamed,
and a Sentinel-1 band is now sitting in the API's memory.

Finally, the same UI with no Vite in the loop:

```bash
docker compose up -d --build api && open http://localhost:8000/
```

**Expect** the identical interface served by FastAPI, and `/health`, `/ask` and
`/ask/stream` still answering — the static mount sits at `/` and must not shadow them.
Note that `/items` has to be in the Vite proxy list for the dev server, or it answers the
asset list with `index.html` under a **200** and the popover fails as
`Unexpected token '<'`.

## Step 11 — Guardrails (step 7)

The offline suite covers all of this. What it cannot cover is the half that only exists
against a live model: that the numbers in `MODEL_PRICING` bear any relation to what the
account is actually billed, and that the model reads the collection allowlist off the
tool schema instead of guessing.

### 11a — The allowlist reaches the model

```bash
curl -s -X POST http://localhost:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "Which Sentinel-2 collections can you search? Name them exactly."}' | jq -r .answer
```

**Expect** the real ids (`sentinel-2-l2a`, `sentinel-2-l1c`, `sentinel-2-c1-l2a`,
`sentinel-2-pre-c1-l2a`) rather than plausible inventions like `sentinel-2` or `S2_L2A`.
It should answer without calling a tool at all — the ids are in the schema it was given.

Then that the enforcement still bites when it ignores them:

```bash
docker compose exec api python -c "
from app.tools.stac_search import stac_search
try:
    stac_search([12.35, 41.75, 12.65, 42.0], collections=['sentinel2'])
except ValueError as e:
    print(e)
"
```

**Expect** `Unknown collection(s) 'sentinel2'. Available: ...` and **no HTTP request** —
this is the failure that otherwise returns zero results and gets reported as fact.

### 11b — The cost estimate against a real bill

Ask one question that chains two tools, then read what the thread was charged:

```bash
curl -s -X POST http://localhost:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "What does STAC call a scene, and which ones cover Rome in January 2024?", "conversation_id": "cost-check"}' | jq -r .answer

docker compose exec api python -c "
from app.agents.graph import _spent, _turn_config
turns, usd = _spent(_turn_config('cost-check'))
print(f'{turns} turn(s), \${usd:.4f}')
"
```

**Expect** one turn and a figure in the region of \$0.03–\$0.08 on `claude-sonnet-4-6`.
Then **check it against the Anthropic console's usage for the same minute** — this is the
only step that can. A figure that is out by a factor rather than a rounding means
`MODEL_PRICING` is stale or the model was silently served by a different one; a figure of
exactly \$0.00 means `usage` did not arrive on the response and every cap is now inert.

### 11c — The cap actually stops a conversation

```bash
docker compose exec -e MAX_CONVERSATION_TURNS=2 api python -c "
from app.agents.graph import answer_question, ConversationBudgetExceeded
from app.db.session import SessionLocal
db = SessionLocal()
for i in range(3):
    try:
        print(i, answer_question(db, 'hello', 'cap-check').text[:40])
    except ConversationBudgetExceeded as e:
        print(i, 'REFUSED:', e)
"
```

**Expect** two answers and then `REFUSED: This conversation has reached its limit of 2
turns.` — and note the refusal is instant, because the check runs before the model call.
Over HTTP the same thing is a **429** with that message in `detail`, on both `/ask` and
`/ask/stream`; on the stream it must be a real 429 with a JSON body, **not** a 200
carrying an error frame. In the UI the message itself should appear, not "HTTP 429".

### 11d — The rate limiter

Unlike the rest of step 11 this one needs no live model, so it is cheap to re-run. Start
the API with a limit low enough to reach:

```bash
RATE_LIMIT_ASK_PER_MINUTE=2 uv run uvicorn app.main:app --port 8077
```

```bash
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code} " -X POST http://localhost:8077/ask \
    -H 'Content-Type: application/json' -d '{"question":"hi"}'
done; echo
for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{http_code} " http://localhost:8077/health; done
```

**Expect** `200 200 429 429` on `/ask` and five `200`s on `/health` — the healthcheck is
untiered on purpose, and a limiter that trips it takes the container down. The refusal
carries a `Retry-After` that *decreases* as the window slides:

```bash
curl -s -i -X POST http://localhost:8077/ask -H 'Content-Type: application/json' \
  -d '{"question":"hi"}' | grep -i 'HTTP/\|retry-after\|detail'
```

Then the two properties that are easy to get wrong, with no model call spent — a limit of
0 refuses outright, so nothing downstream ever runs:

```bash
RATE_LIMIT_ASK_PER_MINUTE=0 RATE_LIMIT_PROXY_PER_MINUTE=3 uv run uvicorn app.main:app --port 8079
```

```bash
# A body with no `question` at all: invalid, and it must still be the limiter that answers.
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8079/ask \
  -H 'Content-Type: application/json' -d '{}'

for i in 1 2 3 4 5; do curl -s -o /dev/null -w "%{http_code} " http://localhost:8079/preview/nonexistent; done
```

**Expect** `429` for the malformed body — **not 422**. A 422 would mean the request was
routed and validated before being refused, which is the whole point of the middleware
being outermost: a request that will be turned away must never open a database session or
reach a model. And **expect `404 404 404 429 429`** on the proxy tier: the first three
reach the router (404 because the item does not exist, which is correct), and the tier
kept its own budget while `/ask` sat at zero.

Last, the header that is a bypass if it is trusted by mistake:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8079/ask \
  -H 'X-Forwarded-For: 9.9.9.9' -H 'Content-Type: application/json' -d '{"question":"hi"}'
```

**Expect** `429` regardless of the header, with `RATE_LIMIT_TRUST_PROXY_HEADER` at its
default of `false`. If a varying `X-Forwarded-For` ever gets a `200`, the limiter is
trusting a value the client chose and is effectively off.

## Step 12 — Tracing (step 8)

The offline suite asserts on the records a `Turn` produces. What it cannot assert is that
those records survive the trip through uvicorn's logging configuration — which is the one
thing that was actually broken when this was written.

### 12a — The trace reaches the log at all

```bash
uv run uvicorn app.main:app --port 8082 > /tmp/trace.log 2>&1 &
curl -s -o /dev/null -X POST http://localhost:8082/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What are STAC Items?","conversation_id":"trace-check"}'
grep eo_rag.trace /tmp/trace.log
```

**Expect** a `turn_start`, one `generation` per model call, a `tool` per tool call, a
`retrieval` per `rag_lookup`, and a closing `turn_end` — each a single line of JSON.

**Expect nothing at all if `configure_logging()` has been removed or reordered.** Uvicorn
configures `uvicorn`, `uvicorn.error` and `uvicorn.access` and leaves the root logger with
no handler, so INFO records from `eo_rag.trace` propagate to nothing and are dropped by
the last-resort handler's WARNING threshold. That is not a hypothetical: it is what
happened on the first run of this step, and grep returned zero lines for a question that
had been answered correctly end to end.

### 12b — The numbers are real

Read the closing line:

```bash
grep turn_end /tmp/trace.log | tail -1 | sed 's/.*eo_rag.trace //' | python -m json.tool
```

**Expect** `input_tokens` and `output_tokens` to be the sum over every `generation` of the
turn, and `cost_usd` to match them at the rates in `app/agents/cost.py` — cross-check one
against the Anthropic console as in step 11b. **Expect `tools` to name every tool that
ran, in order**, which is the thing that had no external signal before this step.

A worked example, from the first live run of this step on a single documentation question:

| Field | Value |
|---|---|
| `steps` | 6 — five `rag_lookup` calls plus the concluding turn, i.e. **the step cap** |
| `input_tokens` / `output_tokens` | 22,841 / 1,084 |
| `cost_usd` | 0.084783 — 8.5% of the default $1.00 conversation budget, on one question |
| `ms` | 23,692 |
| `retrieval` `best` | 0.4581, 0.3396, 0.4215, 0.3964, 0.437 |

That is what a healthy-looking answer over a corpus that does not really contain it looks
like from the inside: the model rephrased and re-queried five times, never got a decisive
chunk, and answered from the best of a poor set. The response cites `stac-spec-core` and
reads fine, which is exactly why the trace is worth having.

### 12c — Retrieval distances, and what "good" is

```bash
grep '"event": "retrieval"' /tmp/trace.log | sed 's/.*eo_rag.trace //'
```

`best` is a **cosine distance**: 0 is identical, 1 is orthogonal. As a rough reading on
this corpus — below ~0.3 the chunk is genuinely on topic; 0.3-0.5 is the model working
with something adjacent; above ~0.5 it is answering despite the retrieval rather than from
it. Several lookups in one turn with nothing under 0.35 is the signature of a question the
index cannot answer, and is a chunking or ingestion problem, not a prompt one.

### 12d — Langfuse, when it is configured

```bash
uv sync --extra observability
LANGFUSE_PUBLIC_KEY=pk-lf-... LANGFUSE_SECRET_KEY=sk-lf-... \
  uv run uvicorn app.main:app --port 8082
```

**Expect** one trace per turn in the Langfuse UI, named `turn`, with a nested `agent`
generation per model call carrying token counts and cost, a `tool` span per tool call, and
a `retriever` span per lookup holding every chunk's distance.

Then the property that matters more than the dashboard — telemetry must not be able to
break a request:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-anything LANGFUSE_SECRET_KEY=sk-lf-anything \
  LANGFUSE_HOST=http://127.0.0.1:1 uv run uvicorn app.main:app --port 8082
```

**Expect** questions to be answered exactly as before, the local trace lines to be
unaffected, and the only sign of trouble to be OTLP export warnings on a background
thread. If a request ever fails because Langfuse did, `_safe()` in `app/obs/tracing.py`
has been bypassed.

## Step 13 — The eval harness (step 9)

This step *is* the live check, so unlike the others there is no separate manual version:
`scripts/eval.py` is what `VERIFY.md` has been doing by hand since step 1, written down.
What still needs a human is deciding whether a number is good.

### 13a — Are the live services up?

```bash
python -m scripts.eval --smoke
```

**Expect** four `[ ok ]` lines: the database with a non-zero chunk count, Bedrock
returning a vector of exactly `EMBEDDING_DIM`, the catalog returning at least one scene
for a search that must match, and the configured model resolving through the Models API
(which costs no tokens — this proves credentials and the model id without buying a
completion to find out).

Run this **first** whenever the eval looks broken. It is the five seconds that separates
"the prompt regressed" from "Bedrock lost model access in this region".

### 13b — Retrieval, free

```bash
python -m scripts.eval --retrieval-only
```

No model calls at all, so this is the one to run on every ingestion change. **Expect**
`recall@5` at or near 1.0 and `MRR` well above it being a coin toss. Read them knowing
what they can and cannot say:

- `recall@5` below 1.0 means the labelled section **never came back**, which is a real
  failure and the only thing recall is good for here.
- `MRR` is the number that matters. 1.0 means the right chunk was first; 0.2 means it was
  fifth, and the model reads the top of the list first.
- `precision@5` is low by construction — one relevant chunk in five is 0.2 — and is the
  number that should move if chunking improves.

**A failing case is not automatically a retrieval problem.** The first run of this harness
reported `catalog-vs-collection` as a total miss; it was the *label* that was wrong, and
the note on that case in `evals/cases.yaml` records it. Before changing any retrieval
code, look at what actually came back — the failure line prints it.

### 13c — End to end, and it costs money

```bash
python -m scripts.eval                        # everything
python -m scripts.eval --tag docs             # documentation cases only
python -m scripts.eval --case catalog-search-rome
```

The header says how many cases will make live turns before any of them do. **Expect** each
case to report its steps, cost and latency, and the run to end with a total — a set of a
dozen cases is on the order of a dollar and several minutes, which is exactly why it is
not in `pytest`.

What the answer checks are actually asserting: `expect_tools` is a **subset** check, so a
model that calls an extra tool has been thorough rather than wrong; `must_contain` is a
case-insensitive substring match, deliberately a low bar that catches a missing field name
or an invented media type without pretending to grade prose.

### 13d — Regressions

```bash
python -m scripts.eval --save-baseline   # accept the current scores
python -m scripts.eval --compare         # judge against them; exit 1 on a regression
```

**Expect** a per-metric delta and, when something broke, the case ids naming it. Two
things are regressions and nothing else is: a case that passed and now fails, and a gated
metric falling by more than `TOLERANCE`. A **new** failing case is not one — adding a
known-failing case is how a bug gets recorded, and gating on it would mean the only way to
write a failing test is to break the build.

`--compare` returning 1 is what CI would gate on. Note that a full comparison spends money
every run, so a cheap CI gate is `--retrieval-only --compare`: it regresses on chunking
and embedding changes, which is where most retrieval damage comes from, for nothing.

## Step 14 — The MCP server (step 10)

Needs the optional extra: `uv sync --extra dev --extra mcp`.

### 14a — stdio, driven as a real client

The Inspector is the friendly version (`npx @modelcontextprotocol/inspector uv run --extra
mcp eo-rag-mcp`). To check it without a browser, drive the protocol directly: launch
`python -m app.mcp.server`, write JSON-RPC lines to its stdin and read them back from
stdout.

**Expect** `initialize` to answer with `{"name": "eo-rag", "version": "0.1.0"}`,
`tools/list` to return exactly `stac_search`, `compute_index`, `rag_lookup`,
`resources/list` to return exactly `docs://sources`, and `resources/templates/list` to
return the two `docs://document/...` and `docs://section/...` templates.

**Expect every line on stdout to be parseable JSON.** That is the real assertion here:
under stdio, stdout *is* the wire, and a single log line written there kills the session
with a parse error that names nothing useful. Diagnostics belong on stderr.

### 14b — Resources

With the DB up and the corpus ingested, read `docs://sources` and then
`docs://section/stac-spec-core/Item%20fields`.

**Expect** the index to report `stac-spec-core`, 113 chunks and 18 sections, with the
sections in **document order** — `STAC Overview`, `Foundations`, `Item Overview`, … not
alphabetical. **Expect** the section to come back starting with its own `## Item fields`
heading, and the `%20` to have been decoded by the SDK rather than left literal.

### 14c — `stac_search`, and the projection

Call it over Rome for January 2024, `limit: 2`.

**Expect** the text content to contain **no `coordinates`**, and the same item ids `/ask`
returns for the same area. Then call it again with `include_geometry: true` and expect the
polygons to appear. Measured: 2,560 bytes against 3,691 for two scenes — and the gap grows
with the scene count, which is why the default is the projection.

### 14d — `compute_index` over MCP

Take an id from 14c and compute NDVI over a few km of it. Slow on purpose: 5–15 seconds of
real raster reads.

**Expect** statistics **identical to a direct `compute_index(...)` call on the same item
and bbox** — the MCP layer is an adapter and must change nothing. Sanity-check the numbers
too: over central Rome in January, mean ≈ 0.30, median ≈ 0.21, p90 ≈ 0.72, everything
inside [-1, 1]. That spread between median and p90 is the mixed urban-and-vegetation split
the percentiles exist to show.

### 14e — `rag_lookup`

**Expect** prose beginning with `[Source: stac-spec-core - Item fields]`, **not** a JSON
object, and no cosine distance anywhere in it. This is the one tool whose result is text,
and a `{` as the first character means someone made it return the model.

### 14f — The HTTP transport, in the container

```bash
docker compose up -d --build api
```

Point a real MCP client at `http://localhost:8000/mcp` and expect the same three tools.
Then the two paths that have each been broken once:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/mcp \
  -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"c","version":"0"}}}'

curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/mcp/mcp -d '{}'
```

**Expect `307` and `404`.** A **405** on the first is the bug this step found: `Mount("/mcp")`
does not match `/mcp` itself, and with the built UI mounted at `/`, StaticFiles claims it
and refuses the method. **It only reproduces in an image with a frontend build**, which is
every deployed one and no test run. A **200 on `/mcp/mcp`** means `streamable_http_path="/"`
was lost and the endpoint moved.

Then confirm the rest of the app is untouched: `/health` is `{"status":"ok"}`, the UI at
`/` is 200, and a malformed `/ask` body is still 422.

### 14g — The rate-limit tier

**Expect a full client session to complete comfortably** at the default 60/minute — it is
about 8 HTTP requests, since a session is a handshake plus listings, not one call per tool.
Then hammer `/mcp/` past the limit and expect **429** with a `Retry-After`.

Worth doing once with `RATE_LIMIT_MCP_PER_MINUTE=5`: the handshake itself fails. That is
what sharing `/ask`'s 10/minute would do to the second client to connect, and why the tier
is separate.

### 14h — Host validation

Send an `initialize` with `Host: evil.example:8000`.

**Expect `421 Invalid Host header`.** Then set `MCP_ALLOWED_HOSTS` to that name and expect
200. Note the default patterns are `127.0.0.1:*` and `localhost:*` and **require a port**,
so a request arriving on 80 or 443 is refused even from localhost — behind any reverse
proxy this setting is not optional.

### 14i — A broken database

Run the stdio server with a deliberately unreachable `DATABASE_URL`.

**Expect** `rag_lookup` to come back as a tool error carrying the psycopg message, and
**expect the session to survive it**: `stac_search` still returns scenes and `tools/list`
still answers. Only `rag_lookup` and the documentation resources need Postgres; the other
two need nothing but network.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Empty `sources` on a documentation question | the model did not call the tool — prompt, not code |
| Empty `sources` on a *follow-up* | expected: it answered from history, no tool ran |
| A follow-up that does not remember | the process restarted, or the `conversation_id` was not passed back |
| Plausible answer, empty `sources`, on a data question | it is inventing; the system prompt is supposed to prevent this |
| Answer stops mid-sentence | `MAX_TOKENS` in `app/agents/graph.py` |
| `AccessDeniedException` | Bedrock model access not enabled for the region |
| `STAC search rejected ... (HTTP 400)` | the catalog refused the parameters; its own message is included |
| `STAC API unreachable` | network, or `STAC_API_URL` |
| `count=0` where data should exist | check datetime normalization first (step 8) |
| Slow response on step 5 | expected: more tool rounds are more sequential model calls |
| `ModuleNotFoundError` for `numpy` / `rasterio` | dependencies changed and the image was not rebuilt: `docker compose up -d --build api` |
| `ImportError: libexpat.so.1` | rasterio's wheels want the system libexpat, which python-slim omits — the Dockerfile installs `libexpat1` for this |
| An index outside [-1, 1] | the reflectance conversion, not the scene: check `reflectance.offset_applied` (step 9) |
| `Could not read the rasters` | GDAL has no HTTP egress from the container, or the asset href moved |
| `does not overlap this item's footprint` | the bbox and the chosen item disagree — the model picked a scene elsewhere |
| The stream arrives all at once | `curl` without `-N`, or something buffering `text/event-stream` — `X-Accel-Buffering: no` is set for the day nginx is in front |
| Blank map, everything else fine | the basemap style URL failed; the banner says so and the console carries the `style.load` error |
| Footprints in the Gulf of Guinea | `[lat, lon]` inversion — check the first coordinate as in step 10b |
| Footprints unchanged after a follow-up | expected: no tool ran, so no `features` frame was sent — the same reason `sources` is empty |
| Two map canvases in dev | the map was recreated on a re-render; the `useRef` guard in `MapPane` is what prevents it |
| A quicklook never appears, CORS error naming the asset host | something is loading the catalog href directly instead of `/preview/{item_id}` — the proxy exists precisely so no CORS is involved |
| `/preview/…` returns 404 on an item that clearly has a thumbnail | its preview asset is not a browser-renderable type; `overview` on Earth Search is a GeoTIFF and is refused on purpose |
| `/preview/…` returns 502 | the catalog or the asset host failed — the message says which |
| A quicklook is offset or mirrored over its footprint | corner ordering in `imageCorners` — it has a unit test, and nothing at runtime reports this |
| The UI at :8000 is stale after a frontend edit | `frontend/` is built into the image, not bind-mounted: `docker compose up -d --build api`, or use the Vite dev server |
| The model invents collection ids | they are in the tool schema's description; check `ALLOWED_COLLECTIONS` is not empty, which turns both the naming and the check off |
| `Unknown collection(s) …` on an id that exists | the catalog was repointed but `ALLOWED_COLLECTIONS` still lists the old one's ids |
| A conversation stops with a 429 far too early | an unrecognized `CLAUDE_MODEL` is priced at the dearest model on purpose — add it to `MODEL_PRICING` |
| Estimated cost stuck at $0.00 | `usage` is not reaching the agent node, so both caps are inert even though the turn cap still counts |
| A 429 arrives as a 200 with an error frame | the budget was checked inside `stream_answer` only; the route has to check before the response starts |
| Everything 429s immediately | a `RATE_LIMIT_*_PER_MINUTE` of 0 means refuse everything; `RATE_LIMIT_ENABLED=false` is how you turn the limiter off |
| The rate limit never trips under load | more than one worker, each with its own in-process window — the effective limit is N × the configured one |
| A malformed body returns 422 while over the limit | the middleware is no longer outermost; requests are being routed before being refused |
| Every client shares one bucket | requests arrive from a proxy, so the peer address is the proxy's — set `RATE_LIMIT_TRUST_PROXY_HEADER=true`, but only if you control it |
| Limits reset for no reason | in-process state: the API restarted, or `--reload` restarted it for you on a file change |
| No `eo_rag.trace` lines at all | `configure_logging()` is not running before the app is built — uvicorn leaves root without a handler, so the records go nowhere (step 12a) |
| Every trace line printed twice | a second handler on `eo_rag`, or something added one to root as well: `configure_logging` is idempotent, callers adding their own are not |
| `turn_end` present, no `turn_start` | the trace was opened after the first frame; an abandoned stream will also be missing its record |
| `cost_usd` is 0 on every generation | `usage` is not reaching the agent node — the same fault that makes the step 7 cost cap inert |
| Traces in the log but not in Langfuse | keys missing or only one set, the `observability` extra not installed, or `LANGFUSE_ENABLED=false`; the startup warning distinguishes the second case |
| A question fails only when Langfuse is configured | `_safe()` has been bypassed somewhere — telemetry must never propagate an exception into the turn |
| `retrieval` events missing while `rag_lookup` runs | `rag_lookup` was switched back to `retrieve`, which discards the distances |
| An eval case fails on retrieval | look at what *did* come back before touching retrieval — a too-narrow section label is the likelier cause, and has happened (step 13b) |
| `recall@5` is 1.0 and nothing ever fails | expected: section labels are coarse, so recall saturates. Read MRR instead |
| Every eval case errors identically | run `--smoke` — it is almost always the database, the region's Bedrock access, or the catalog, not the code |
| `--compare` says "No baseline yet" | nothing has been accepted as the standard: run `--save-baseline` once |
| `--compare` regresses on a metric right after adding cases | only if they are *shared* cases; new ids are excluded from the aggregates on purpose |
| The eval bill is a surprise | `--retrieval-only` makes no model calls at all; the run header states how many live turns it is about to spend |
| `/mcp` is a 404 but `/mcp/mcp` works | `streamable_http_path="/"` was dropped: the SDK's app already serves at /mcp, so mounting it at /mcp nests them |
| `POST /mcp` is **405** while `/mcp/` is 200 | only in an image with a frontend build — StaticFiles at `/` claims `/mcp` and serves GET/HEAD only; the explicit redirect route in `app/main.py` is what fixes it |
| `Task group is not initialized` on every /mcp request | the session manager is not being run in the app lifespan |
| `run() can only be called once per instance` | the app was started twice in one process; a session manager is single-use, which is why the MCP tests reload the modules |
| `421 Invalid Host header` | DNS-rebinding protection: set `MCP_ALLOWED_HOSTS`, and remember its default patterns require a port, so port 80/443 is refused even on localhost |
| The stdio client dies with a JSON parse error | something wrote to stdout, which is the wire — `configure_logging(stream=sys.stderr)` in `app/mcp/server.py` is what keeps it clear |
| An MCP client trips the rate limiter on connect | `RATE_LIMIT_MCP_PER_MINUTE` is too low: one session is ~8 requests before any tool is called |
| `/mcp` missing entirely, everything else fine | the `mcp` extra is not installed, so `load_mcp_server()` returned None — or `MCP_HTTP_ENABLED=false` |
| MCP tools work but `rag_lookup` errors | only that tool and the doc resources need Postgres and Bedrock; check `DATABASE_URL` and the AWS chain |

---

## Last run

2026-08-07, all steps passing (step 9 after a fix; see the note on it below).

Step 10, same day, against the live catalog and the live model:

| Check | Result |
|---|---|
| 10a | `start` at 0.0s, `tool_start` at 3.3s, `tool_end` `ok:true` 863ms, **`features` (3 polygons) at 4.18s, first `token` at 5.24s**, `done` at 11.86s. The footprints reached the map a second before the answer began. |
| 10b | `geometry in the model view: False`, 1699 bytes, 2 `Polygon`s, first coordinate `[11.3549…, 42.3946…]` — lon then lat, over Lazio |
| 10c | 3 footprints drawn over Rome, camera flown once; the follow-up "which of those has the least cloud?" sent **no `features` frame and left the map untouched**, answering `S2B_32TQM_20240130_0_L2A` at 1.52% from history alone; card hover, card selection and the Imagery toggle all behaved |
| 10c (built) | `docker compose up -d --build api` then `:8000` — same interface with no Vite, `/health` and `/ask` unshadowed, a documentation question answered with `sources: ["stac-spec-core"]` |
| 10d | Stop mid-answer kept the 169 characters already written, showed the grey "Stopped." and no error box. The next turn **on the same conversation** answered normally, 5,369 characters with sources — **before `_repair_interrupted_turn` this 400ed**, and so did every turn after it. Stop stayed armed on that second turn. New chat mid-stream followed immediately by another question opened with "Bounding Box / A bounding box…" and carried no fragment of the abandoned answer |
| 10f | on three Sentinel-1 scenes over northern Italy: the popover listed all 10 assets over the map, flipped above the card when 274px of room was not enough for it (**it overflowed the bottom by 64px on the first attempt**, which is what replaced the fixed `max-h` with one measured from the space available), and clicking `thumbnail` downloaded `S1A_IW_GRDH_1SDV_20230130T165925_20230130T165950_047016_05A3B9_thumbnail.png`, 0 console errors. `safe-manifest` came back byte for byte identical to the catalog href (23,996 bytes); the `vv` band's headers returned at once with `content-length: 721276476`, which is the streaming; an unknown asset key and an unknown item both 404ed |
| 10e | clicking a footprint drew its quicklook registered on the tile — the Tyrrhenian coastline in the image aligned with the basemap's; clicking again removed it; three overlapping tiles drew with the outlines still on top, 0 console errors. `/preview/S2B_33TTG_20240130_0_L2A` returned 81,720 bytes of `image/jpeg`, byte for byte what the catalog href gives, and an unknown id 404ed. **This is the check that failed twice**: first with a CORS error against the asset host, then again after `crossOrigin="anonymous"` — which only spread the breakage to the cards — before the proxy removed CORS from the path entirely. The last run was made in the browser profile whose cache was still poisoned, and it passed, which is the point |

| Step | Result |
|---|---|
| 1 | 113 chunks indexed from 1 source, `vector(1024)` matching `EMBEDDING_DIM` |
| 2 | implied by step 1: those chunks only exist because Titan answered |
| 3 | `sources: ["stac-spec-core"]`, 20s |
| 4 | `sources: ["https://earth-search.aws.element84.com/v1"]`, 16s, 3 scenes — `S2B_33TTG_20240130_0_L2A`, `S2B_32TQM_20240130_0_L2A`, `S2B_33TUG_20240130_0_L2A`, cloud cover 1.58 / 1.52 / 10.57 %, identical to the direct catalog call |
| 5 | both sources, 26s, 3142 characters, complete — **truncated at 1000 tokens on the first attempt**, which is what raised `MAX_TOKENS` to 4096 |
| 6 | two-turn conversation: the follow-up resolved "those" from history, answered `S2B_32TQM_20240130_0_L2A` at 1.523485% without re-searching, `sources: []`. The same question on a fresh thread correctly reported having no previous results. |
| 8 | bare interval, single day and open ended all returned 3 scenes; `not-a-date` correctly rejected |
| 9 | see below — **failed on the first attempt**, which is what found the reflectance-offset bug |

Step 9 in detail, on `S2B_33TTG_20240130_0_L2A`, all `offset_applied=False`:

| Window | Time | Resolution | Pixels read | NDVI mean / median | NDWI mean |
|---|---|---|---|---|---|
| Rome suburb `[12.45, 41.85, 12.50, 41.90]` | 14.7s | 10 m | 244,377 | +0.3161 / +0.2448 | — |
| Lake Bracciano `[12.21, 42.09, 12.27, 42.14]` | 5.8s / 3.8s | 10 m | 293,494 | -0.1207 / -0.1176 | +0.5990 |
| Most of tile 33TTG `[12.0, 41.5, 13.1, 42.4]` | 12.3s | 40 m | 3,854,752 | +0.3654 / +0.5402 | — |

Every statistic inside [-1, 1]. The lake gives negative NDVI and strongly positive NDWI,
as water must. The whole-tile read is **faster than the full-resolution suburb read**
despite covering 300 times the area, which is GDAL using the COGs' internal overviews —
the measurement the decision not to add a wall-clock guardrail rests on.

Before the fix, the same suburb window returned a mean of **-4.8e11** with p10 -1.66 and
p90 +3.22. Refusals all correct: bbox off footprint, unknown index (`Available: ndvi,
ndwi`), nonexistent item id, and inverted bbox. Through `/ask`, the model chained
`stac_search` into `compute_index` on its own and returned both provenances, 1824
characters, complete — and step 5's two-tool question still returns both sources at 2282
characters, so the third tool did not push it back into `MAX_TOKENS`.

Scene identifiers for a fixed past month are stable; queries with no datetime or an open
ended one return whatever is most recent, so do not pin expectations on those.
