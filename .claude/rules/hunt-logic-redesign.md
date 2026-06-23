# Hunter — logic redesign (2026-06)

When editing `hunt/hunt_core/deep/`, `scanner/`, `signals/`, or delivery under `runtime/`:

1. **Read** `hunt/docs/HUNT_ARCHITECTURE.md` and `hunt/docs/IMPLEMENTATION_STATUS.md`
2. **Shared spine:** `hunt_core/signals/` — both modules emit through `SignalEmitter`; dedup key is `setup_id` (thesis + structural anchor + direction), not price-derived entry/SL hash
3. **No legacy paths:** do not reintroduce `deep_change_fingerprint`, `_prospective_levels`, `target_signal_rate`/`auto_tune_*`, or `should_send_pinned_batch`
4. **Full cutover:** replace and delete old path — no compat shim or synthetic fallback when data missing (abstain / silence)
5. **Emission:** real reconciled setup only; WAIT / ranging / strong_conflict → silence (no per-tick forced verdict)
6. **Labels:** `сила`/`score` ≠ probability; queue uses «Приоритет очереди» vs header «Сила сигнала»
7. **Module boundary:** `deep/*` and `scanner/*` never import each other; share only via `signals/`, `data/`, `market/`, `track/`
8. **Verify after edits:**
   ```bash
   cd hunt && .venv/bin/python -m compileall -q hunt_core
   .venv/bin/python -m hunt_core._dev.check_imports
   .venv/bin/python -m hunt_core._dev.check_verdict_v2
   .venv/bin/python -m hunt_core._dev.check_deep
   .venv/bin/python -m hunt_core._dev.check_logic
   .venv/bin/python -m hunt_core._dev.replay_fusion
   .venv/bin/python -m hunt_core._dev.budget
   ```

Market plane rules remain in `hunt-ccxt.md` / `hunt/docs/CCXT.md`.
