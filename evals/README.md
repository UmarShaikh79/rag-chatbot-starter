# Evals

`datasets/golden.jsonl` holds 20 questions about the sample corpus: 16 answerable (with the
document that holds the answer and phrases the answer must contain) and 4 unanswerable ones,
which must come back as `kind: clarify`.

Milestone 1 uses this file in the end-to-end tests (`tests/e2e/test_golden.py`) to check
retrieval hit rate and abstention with the offline profile. Milestone 3 adds the eval runner
(`evals/run.py`), faithfulness metrics, the threshold calibration command (ADR 011) and the
CI gate (ADR 010).

Fields:

| field | meaning |
| --- | --- |
| `id` | stable ID |
| `question` | what the user asks |
| `answerable` | false when the corpus does not contain the answer |
| `expected_documents` | document IDs that contain the answer |
| `must_include` | phrases a correct answer contains (case-insensitive) |
