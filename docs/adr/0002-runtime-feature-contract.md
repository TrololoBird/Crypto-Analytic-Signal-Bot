# ADR 0002: Стабильный публичный feature-контракт runtime-пути

- Статус: Accepted
- Дата: 2026-04-28

## Контекст

В runtime-пути `SignalBot -> SymbolAnalyzer -> StrategyEngine -> strategies` feature surface
долго оставался неформализованным: список полей снимка `prepared` был «по факту реализации»,
а не через явный контракт.

Это создавало риски:

- тихого дрейфа схемы при рефакторингах;
- попадания экспериментальных/scaffold-полей в production payload;
- отсутствия guard-тестов на missing/extra поля.

## Решение

1. Введён единый публичный контракт в `bot/feature_contract.py`:
   - `PUBLIC_FEATURE_SCHEMA_VERSION = "v1"`;
   - `PUBLIC_FEATURE_FIELDS` как канонический порядок/набор полей;
   - strict-валидация (`validate_public_feature_payload`).
2. `build_prepared_feature_snapshot()` теперь нормализует payload через контракт.
3. Контракт покрыт тестами на:
   - стабильность схемы;
   - negative cases (missing field, extra field);
   - сторожевой запрет scaffold/experimental imports в runtime call path.

## Последствия

- Контракт feature payload стал явной публичной границей.
- Любое неожиданное расширение или урезание схемы детектируется тестами.
- Экспериментальные scaffold-компоненты изолированы от production imports на критическом runtime-пути.
