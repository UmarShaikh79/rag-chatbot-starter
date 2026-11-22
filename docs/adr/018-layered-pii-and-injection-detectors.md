# ADR 018: Layered PII and injection detectors

Status: Accepted (milestone 2)

## Decision

PII and injection checks are chains of detectors chosen in config and registered by name (`pii_detector`, `injection_detector`):

- PII: `regex` (default; emails, phones, cards with Luhn, IBANs, US SSNs, IPv4) and `presidio` (optional extra; names, places and more via spaCy NER). Spans from every detector are merged, then replaced with `[LABEL]`. One configured redactor serves the input guard, persistence of questions and memory; answers (the output guard, the streaming redactor and the stored answer) use `guardrails.pii.output_detectors`, regex by default. `guardrails.pii.allow` lists strings never redacted (a public support address).
- Injection: `heuristic` (default; rules run on NFKC-normalised text with zero-width characters removed, leetspeak and spaced-out letters undone and short base64 payloads decoded, plus rules for a few other languages and for link-based exfiltration) and `llm` (optional; a classifier prompt on the `cheap` model, input stage only by default, with a fail-open or fail-closed setting). Cheap detectors run first; the model is not called once a rule fires.
- Output: on the complete answer, in every stream mode, PII redaction and a prompt-leak check (ten consecutive words shared with the system prompt make the answer a refusal). Only the template's own wording counts, not values filled into it: the clarify prompt passes in topic titles and asks the model to mention them, which is not a leak (fixed after the first live run with local models).

## Alternatives considered

Presidio on by default (the plan's proposal); an LLM check on every request; a hosted moderation API.

## Why

Presidio needs a spaCy model download (12 MB small, ~400 MB large) and adds CPU per request, which would make the default install heavier and the offline profile and CI slower. Regex plus heuristics stay free and dependency-free; the stronger detectors are one config line away.

## Consequences

The defaults still miss names, street addresses and paraphrased injections. Teams with personal data in conversations should turn on `presidio`; teams worried about injection should add `llm`. Every detector produces false positives on some corpora: the test suite checks the sample corpus passes the context guard untouched, and the same check is worth running on your own documents.

Addendum (first Presidio run): with Presidio on the output stage, answers built from the sample
documents were corrupted ("Tidepool Notes is..." -> "[PERSON] is...", "choose Request refund"
-> "choose [PERSON] refund", "(Frankfurt)" -> "([LOCATION])"). Personal data reaches the bot
through user messages, and answers are the operator's own content, so answers now use
`pii.output_detectors` (default `[regex]`): NER on what users write, patterns on what the bot
says. `null` restores one redactor for both.
