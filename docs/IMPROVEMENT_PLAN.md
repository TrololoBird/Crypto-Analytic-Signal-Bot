# Improvement plan — CLOSED for v1

> **Активный backlog:** только [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md).  
> Не использовать этот файл для новых списков улучшений.

## v1 delivered (frozen)

| Block | Status |
|-------|--------|
| Hygiene A/B/C | ✅ |
| Waves E1–F11 | ✅ |
| W0 harvest mode | ✅ |
| W1 de-bloat (partial) | ✅ |
| W2 order_block + TG hint | ✅ |
| W3/W4 harvest + calibration gate | ✅ |

## Verify (unchanged)

```bash
make check
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
```

## Next work

See backlog table: **V1.1-***, **OPS-***, **OPT-*** in [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md).
