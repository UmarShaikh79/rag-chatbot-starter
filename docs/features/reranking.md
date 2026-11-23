# Reranking

A second, sharper look at the retrieved passages: a reranking model reads the question and
each candidate passage **together** and scores how well the passage answers it. Retrieval
finds 20 candidates fast; the reranker puts the best 8 on top.

Back to the [docs home](../README.md) · See also: [retrieval strategies](retrieval-strategies.md)

## At a glance

| | |
| --- | --- |
| **What it does** | Re-scores `rerank.candidates` (20) retrieved passages against the question and keeps the best `top_k` (8). If a hosted reranker fails, answers from the retrieval order instead, with a warning. |
| **Turned on by** | `retrieval.rerank.enabled: true` (off by default); `retrieval.rerank.model` names a profile under `rerankers:` in [`config/models.yaml`](../../config/models.yaml) |
| **Main files** | [`providers/rerankers/__init__.py`](../../src/chatbot/providers/rerankers/__init__.py) (`apply_scores`, `RerankError`, `patient_reranking`, `rerank_cost`), [`providers/rerankers/cohere.py`](../../src/chatbot/providers/rerankers/cohere.py) (`CohereReranker`), [`providers/rerankers/cross_encoder.py`](../../src/chatbot/providers/rerankers/cross_encoder.py) (`CrossEncoderReranker`), [`providers/rerankers/lexical.py`](../../src/chatbot/providers/rerankers/lexical.py) (`LexicalReranker`), `RetrievalPipeline.retrieve` in [`retrieval/pipeline.py`](../../src/chatbot/retrieval/pipeline.py), the `Reranker` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | Any [retrieval strategy](retrieval-strategies.md); `COHERE_API_KEY` (Cohere) or `uv sync --extra rerank` (local cross-encoder) |
| **Used by** | The [weak-retrieval gate](weak-retrieval-and-calibration.md), which then gates on the rerank score |
| **Decisions** | ADR [011](../adr/011-retrieval-thresholds-are-calibrated-never-constants.md) (every score scale needs its own calibration), ADR [019](../adr/019-hybrid-retrieval-with-rank-fusion-and-a-gate-score.md) (the gate score) |

## What it does

An embedding model turns the question and the passage into vectors **separately**, then
compares them: fast enough to search millions, but coarse. A reranker (a *cross-encoder*)
reads the pair at once, so it notices that "Can I get a refund after 30 days?" is answered by
"After 30 days, annual plans are not refundable", not by a passage that merely mentions
refunds. That's too slow for the whole corpus, so it runs only on the candidates retrieval
found.

| Profile | Provider | Runs | Notes |
| --- | --- | --- | --- |
| `cohere:rerank` (default when enabled) | `cohere` | Cohere's API (`rerank-v5.0-fast`) | `COHERE_API_KEY`. Retries, then falls back. Priced if you set prices. |
| `local:bge-reranker-base` | `cross_encoder` | On your CPU/GPU (`BAAI/bge-reranker-base`) | `uv sync --extra rerank` (pulls in PyTorch); downloads the model on first use. |
| `lexical:overlap` | `lexical` | In process | Word overlap. For tests and the offline profile only. |

Reranking also gives the [gate](../glossary.md#gate-score) a better score to judge by: with it
on, "answer or ask" is decided on the rerank score (`retrieval.gate: auto`). On the offline
profile with Cohere (35 questions, 2026-11-23), calibration reached answer recall 1.0 **and**
abstention 1.0; the hashing embedder alone managed 0.92 and 0.20:

```json
{
  "scorer": "rerank|rerank[rerank-v5.0-fast](dense)",
  "min_score": 0.284117,
  "min_hits": 1,
  "answer_recall": 1.0,
  "abstention": 1.0,
  "met_target": true
}
```

## How it works

[`build_pipeline`](../../src/chatbot/retrieval/pipeline.py), when `rerank.enabled`:

1. Reads the profile named by `rerank.model` and builds the reranker
   (`registry.create("reranker", profile.provider, profile)`).
2. Builds a **fallback** pipeline: the same retriever without the reranker, gated on the
   retriever's own score (`gate_signal(rerank=False)`). Only when `rerank.fallback: true`.

Per question, in `RetrievalPipeline.retrieve(query, k, filters)`:

1. **Retrieve more:** ask the retriever for `max(k, rerank.candidates)` hits (20).
2. **Rerank** inside a `rerank` span: `reranker.rerank(query, hits, k)`.
   - **Cohere** (`CohereReranker.rerank`): `POST {base_url}/v2/rerank` with the passages' text.
     429, 5xx, timeouts and connection errors are retried: twice within about 1.5 s in a chat
     turn (someone is waiting), for up to about 3 minutes inside `patient_reranking()`
     (calibrate, eval). `Retry-After` is respected. Other 4xx fail at once. What Cohere billed
     (`meta.billed_units`) is priced with `rerank_cost` and recorded as a usage call.
   - **Cross-encoder**: loads the model once, then `model.predict(pairs)` in a thread so the
     event loop isn't blocked.
   - **Lexical**: share of the question's words (and word pairs) in each passage.
   - All three finish with `apply_scores(hits, scores, top_n)`: set `score` and
     `scores["rerank"]`, keep the earlier scores, sort, keep `top_n`.
3. **If the reranker fails** (`RerankError` after its retries, or any error) and there is a
   fallback and we're not in `patient_reranking()`: mark the span failed, log a warning, count
   `chatbot.rerank.fallbacks`, and return the first `k` hits in retrieval order.

Then [`SimpleOrchestrator.retrieve`](../../src/chatbot/orchestration/simple.py) asks
`pipeline.used()`: after a fallback it's the fallback pipeline, so the turn is gated and
calibrated as a retriever-only turn, and the response gets the warning `rerank_unavailable`.
Calibrate and eval never fall back: a calibration must come from scores of the right scale.

## Inputs and outputs

**In:** the question, the candidate hits (best first), and `top_n` = `top_k`.

**Out:** `(hits, usage)`: the best `top_n` hits, each with `retriever="rerank"`,
`score = scores["rerank"]`, and the retrieval scores kept (`scores["dense"]`, …). For Cohere,
`usage.cost_usd` when prices are set.

What clients see: the same response as without reranking, plus `"warnings": ["rerank_unavailable"]`
on a fallback turn ([API](../api.md#post-v1chat)). In traces: a `rerank` span with
`chatbot.rerank.model`, `chatbot.rerank.candidates` and, for Cohere, a `rerank <model>` child
span with `chatbot.rerank.attempts`, `chatbot.rerank.billed.*` and `chatbot.rerank.cost_usd`
([observability](../observability.md)).

## Turn it on, change it, turn it off

```bash
# .env
COHERE_API_KEY=...
CHATBOT__RETRIEVAL__RERANK__ENABLED=true
```

Then calibrate twice, because a fallback turn is judged by the retriever's own calibration:

```bash
CHATBOT__RETRIEVAL__RERANK__ENABLED=false chatbot calibrate --apply   # the fallback's thresholds
chatbot calibrate --apply                                              # the rerank thresholds
```

| Setting | Default | Notes |
| --- | --- | --- |
| `retrieval.rerank.enabled` | false | |
| `retrieval.rerank.model` | `cohere:rerank` | A name under `rerankers:` in `config/models.yaml`. |
| `retrieval.rerank.candidates` | 20 | Hits handed to the reranker. More: better recall, slower and pricier. 20–50 for a cross-encoder. |
| `retrieval.rerank.fallback` | true | `false`: a reranker failure fails the request (502). |

A reranker profile ([configuration: models](../configuration.md#models-and-configmodelsyaml)):
`provider`, `model`, `api_key_env`, `base_url`, `params` (sent to Cohere with each request, or
passed to `CrossEncoder(...)`), and prices: `price_per_ksearch` (per 1,000 search units) or
`price_per_mtok.input`. Cohere publishes no rerank prices; copy yours from its dashboard. Left
`null`, rerank calls are counted but unpriced.

**Local instead of hosted:**

```bash
uv sync --extra rerank
CHATBOT__RETRIEVAL__RERANK__MODEL=local:bge-reranker-base
```

**A trial key** allows 10 rerank calls a minute: calibration on 35 questions takes a few
minutes (it waits), and a chat service falls back whenever traffic passes 10 turns a minute.
Use a production key for real traffic.

**Turning it off:** `retrieval.rerank.enabled: false`. The retriever's calibration applies
again. Remove the extra (`uv sync` without `--extra rerank`) if you installed it.

## Extend it

A reranker has a `signature` and `rerank(query, hits, top_n) -> (hits, usage)` (the `Reranker`
Protocol). Register a factory that takes the profile:

```python
# src/chatbot/domain/voyage_rerank.py
import os

import httpx

from chatbot.core.registry import registry
from chatbot.core.types import Usage
from chatbot.providers.rerankers import RerankError, apply_scores


class VoyageReranker:
    def __init__(self, profile):
        self.model = profile.model
        self.signature = f"rerank[{profile.model}]"  # part of the calibration key
        self.key_env = profile.api_key_env or "VOYAGE_API_KEY"

    async def rerank(self, query, hits, top_n):
        if not hits:
            return [], Usage()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.voyageai.com/v1/rerank",
                headers={"Authorization": f"Bearer {os.environ[self.key_env]}"},
                json={
                    "model": self.model,
                    "query": query,
                    "documents": [h.chunk.text for h in hits],
                },
            )
        if resp.status_code >= 400:
            raise RerankError(f"Voyage rerank failed: HTTP {resp.status_code}", resp.status_code)
        scores = [0.0] * len(hits)
        for item in resp.json()["data"]:
            scores[item["index"]] = item["relevance_score"]
        return apply_scores(hits, scores, top_n), Usage()


@registry.register("reranker", "voyage")
def make_voyage(profile):
    return VoyageReranker(profile)
```

Add a profile under `rerankers:` with `provider: voyage`, point `retrieval.rerank.model` at it,
and calibrate. Rules:

- Map scores back by the index the provider returns, not by position in its response.
- Retry rate limits yourself; raise `RerankError` when you give up, so chat turns fall back.
  Check `is_patient()` to wait longer during calibrate and eval.
- Never put the key or the raw provider response in the error: clients see the message.

More in [extending: add a reranker](../extending.md#add-a-reranker).

## Test it

```bash
uv run pytest tests/unit/test_retrieval_quality.py -k "rerank or cohere or cross_encoder"
```

[`test_retrieval_quality.py`](../../tests/unit/test_retrieval_quality.py) covers: the lexical
reranker reorders and records scores; the pipeline reranks the candidates down to `top_k`;
Cohere maps results by index, needs a key, retries 429/5xx/timeouts, gives up quickly in chat
and waits when patient, respects `Retry-After`, doesn't retry other 4xx; a failed rerank
answers from retrieval order with the retriever's calibration; with `fallback: false` or when
patient it fails; the cross-encoder runs off the event loop; rerank costs land in usage.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Responses carry `rerank_unavailable` | Cohere rate limit (a trial key: 10/min), outage, or a bad key | Check the `rerank` span's error; use a production key, or a local reranker. |
| `Set COHERE_API_KEY to use the Cohere reranker` | No key | Add it to `.env`. |
| `chatbot calibrate` takes minutes | Waiting out a trial key's rate limit | Expected; it never falls back. |
| `The cross_encoder reranker needs the rerank extra` | Missing extra | `uv sync --extra rerank` (with Docker: `UV_EXTRAS="--extra rerank"`). |
| Everything gets a clarifying question after enabling rerank | Rerank scores are on their own scale; the provisional `min_score` doesn't fit | `chatbot calibrate --apply`. |
| A fallback turn clarifies more than expected | The retriever has no calibration of its own | Calibrate once with `rerank.enabled: false`. |
| Usage shows rerank calls with no cost | No prices in the profile | Set `price_per_ksearch` or `price_per_mtok.input`. |

## Related

- Before: [retrieval strategies](retrieval-strategies.md).
- After: [weak retrieval and calibration](weak-retrieval-and-calibration.md) (gating on the
  rerank score).
- Costs and traces: [observability](../observability.md#cost-and-token-accounting).
- Terms: [rerank](../glossary.md#rerank), [gate score](../glossary.md#gate-score),
  [scorer](../glossary.md#scorer), [calibration](../glossary.md#calibration).
