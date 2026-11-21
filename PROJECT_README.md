# Casework — Diligent Track 1

Casework reviews transaction-monitoring cases across Bank, E-money, Investment, and Payments & FX. It considers alerts together, applies the institution's policy, and returns an evidence-cited `CLOSE`, `RFI`, or `ESCALATE` decision.

## Repository contents

- `system/` — Python decision engine, OpenRouter integration, deterministic offline fallback, reviewer UI, validator, and synthetic stress tests.
- `output/` — one JSONL result file for each 1,000-case development institution.
- `institutions/` — not included in this repository. Copy the institution folders from the event-provided dataset into this location to rerun outputs or use the UI.

## Generate outputs

Python 3.9+ and the standard library are sufficient. From the repository root, set `OPENROUTER_API_KEY` in your shell to use the model route; the key is read from the environment and is never saved to disk.

```sh
./system/run institutions/bank output/bank.jsonl
./system/run institutions/emoney output/emoney.jsonl
./system/run institutions/investment output/investment.jsonl
./system/run institutions/payments_fx output/payments_fx.jsonl
python3 system/validate_outputs.py
```

Without a key, the system runs its deterministic offline fallback. The included outputs were generated in offline mode; regenerate them with your authorized key before submission if you want model-backed development predictions.

## Reviewer UI

After placing the event dataset in `institutions/`, run:

```sh
python3 system/ui_server.py
```

Open `http://127.0.0.1:8765`. The interface is read-only and shows the case queue, linked alerts, transaction flow, source documents, cited evidence, and any held-payment consequence.

## Checks

```sh
PYTHONPATH=system python3 -m unittest discover -s system/tests -v
python3 system/validate_outputs.py
```

The stress suite covers suspicious activity overrides, partial document support, multi-alert cases, list-match identity, stale reviews, held payments, malformed metrics, output normalization, and evidence citation rules.
