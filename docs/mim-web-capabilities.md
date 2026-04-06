# MIM Web Capabilities

Date: 2026-03-17
Scope: Website access and summarization support in MIM.

## What MIM Can Do

MIM can fetch and summarize public web pages via:

- `POST /gateway/web/summarize`

MIM can also research a plain-language web question via:

- `POST /gateway/web/research`

This is intended for prompts like:

1. `MIM, what's the best brand of toothpaste proven to whiten teeth?`
2. `MIM, research the best entry-level mirrorless camera under $1000.`
3. `MIM, compare the top password managers for families.`

This is intended for prompts like:

1. `MIM, give me a summary of this website: https://example.com`
2. `MIM, summarize this article and keep it short.`
3. `MIM, extract key points from this URL.`

## Safety and Access Rules

1. Web access must be enabled: `ALLOW_WEB_ACCESS=true`.
2. Supported schemes: `http` and `https`.
3. Local/private targets are blocked (for example `localhost`, `127.0.0.1`, `.local`).
4. Response size and extract size are bounded.

## API Usage

Example request:

```bash
curl -sS -X POST http://127.0.0.1:18001/gateway/web/summarize \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "timeout_seconds": 12,
    "max_extract_chars": 12000,
    "max_summary_sentences": 4
  }'
```

Example response shape:

```json
{
  "ok": true,
  "url": "https://example.com",
  "title": "Example Domain",
  "summary": "Page title: Example Domain. ...",
  "excerpt": "...",
  "content_type": "text/html; charset=UTF-8",
  "status_code": 200,
  "memory_id": 123
}
```

## How This Connects to Cross-Domain Reasoning

Summarized pages are persisted into memory class `external_web_summary`.
This allows downstream context systems to treat web summaries as external information signals.

## Capability Discovery

To inspect MIM capabilities and endpoints:

```bash
curl -sS http://127.0.0.1:18001/manifest
```

Look for:

1. capability `web_page_summarization`
2. endpoint `/gateway/web/summarize`

## Quick Operator Flow

1. Start MIM backend on `:18001`.
2. Ensure `ALLOW_WEB_ACCESS=true` in runtime environment.
3. Ask a plain-language research question through `/gateway/intake/text` or call `/gateway/web/research` directly.
4. Use `/gateway/web/summarize` when you already know the URL.
5. Use the returned answer or summary in conversation or workflow notes.

Research responses now also include forward-looking guidance:

1. `next_steps` in `POST /gateway/web/research` responses.
2. A `Next step:` sentence inside conversational research answers so MIM suggests how to keep moving.

## Validation Commands

Use these commands to keep web summarization covered in regular test validation.

Focused web-summary regression:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
python3 -m unittest tests.integration.test_objective75_web_summary_gateway -v
```

Optional full objective integration sweep:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
python3 -m unittest discover -s tests/integration -p 'test_objective*.py'
```

Bulk research simulation sweep:

```bash
python3 scripts/run_mim_web_research_sweep.py \
  --base-url http://127.0.0.1:18001 \
  --mode mixed \
  --total 500
```

For deep troubleshooting runs, increase this to `500`, `1000`, or `2000`:

```bash
python3 scripts/run_mim_web_research_sweep.py \
  --base-url http://127.0.0.1:18001 \
  --mode mixed \
  --total 2000 \
  --sample-limit 100 \
  --retry-passes 3 \
  --retry-backoff-seconds 3 \
  --retry-concurrency-scale 0.5
```

For rate-limit and no-result troubleshooting, the sweep now uses a deferred retry chain well:

1. first pass keeps moving forward across the whole corpus
2. retryable failures are parked instead of retried immediately
3. later passes revisit only that parked set with exponential backoff
4. retry passes can also reduce concurrency so the retry lane is gentler than the first pass

This writes `runtime/reports/mim_web_research_sweep.json` with:

1. success ratio
2. average source count
3. proactive next-step coverage
4. sample failures to inspect
5. status buckets by HTTP/result code
6. error buckets grouped by failure reason
7. family-level pass/fail counts so weak query domains stand out quickly
8. pass-by-pass retry summaries, including recovered-on-retry counts and exhausted retries
