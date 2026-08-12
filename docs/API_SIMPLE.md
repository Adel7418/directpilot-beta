# DirectPilot Beta — простая документация API

DirectPilot Beta — отдельное API-first приложение для безопасной подготовки и проверки рекламных кампаний Яндекс Директ.

## Текущий режим

DirectPilot ведется в live-first продуктовом режиме. На текущем этапе разработки это **активное live/read-only тестирование**:

- `DIRECTPILOT_MODE=live_readonly` — чтение production-данных Яндекс Директа;
- токены и секреты не выводятся в ответах;
- write-like действия возможны только через явные `approved`, `idempotency_key` + режим `live_write`.

Что не является продуктовым/API-first путём:

- `mock` и `sandbox` — legacy/dev fallback, если ещё остаются в коде;
- legacy- и fallback-эндпойнты `/yandex/keywords-research/wordstat/create` и `/yandex/keywords-research/wordstat/{report_id}` (`GET`/`DELETE`) — deprecated для обратной совместимости;
- legacy/временные отчёты, которые пока не покрывают продакт-цепочку.

OpenAPI (видимые API endpoints):

```text
http://127.0.0.1:8000/openapi.json
```

`/`, `/demo/yandex-status`, `/demo/campaigns`, `/demo/report`,
`/demo/recommendations`, `/demo/tools`, `/demo/security-approval` — 7
retired demo/UI paths. They are not product API endpoints and are excluded from
OpenAPI; the app keeps only explicit non-product guard handlers that return 404
for old links.

---

## 1. Health / статус интеграции

### Проверить приложение

```http
GET /health
```

### Проверить статус Yandex Direct интеграции

```http
GET /integrations/yandex/direct/status
```

Показывает, настроена ли конфигурация. Секреты редактируются/маскируются.

---

## 2. Кампании и черновики

### Список кампаний

```http
GET /campaigns
```

Возвращает внутренний список кампаний/черновиков приложения. Для реальных кампаний Директа используйте `/yandex/campaigns`.

### Создать черновик кампании

```http
POST /campaign-drafts
```

Пример:

```json
{
  "name": "Кондиционеры Казань",
  "business_type": "ремонт и обслуживание кондиционеров",
  "region": "Казань",
  "monthly_budget": 30000,
  "landing_url": "https://example.ru/remont-kondicionerov"
}
```

### Список черновиков

```http
GET /campaign-drafts
```

### Получить один черновик

```http
GET /campaign-drafts/{draft_id}
```

### Изменить основные настройки черновика

```http
PATCH /campaign-drafts/{draft_id}
```

Можно передавать только нужные поля:

```json
{
  "name": "Кондиционеры Казань — поиск",
  "region": "Казань",
  "monthly_budget": 45000,
  "landing_url": "https://example.ru/kondicionery"
}
```

---

## 3. Ключевые фразы

### Полностью заменить список ключей

```http
PATCH /campaign-drafts/{draft_id}/keywords
```

```json
{
  "keywords": [
    "ремонт кондиционеров Казань",
    "обслуживание кондиционеров Казань",
    "чистка кондиционеров Казань"
  ]
}
```

### Добавить ключи к существующим

```http
POST /campaign-drafts/{draft_id}/keywords
```

```json
{
  "keywords": [
    "заправка кондиционеров Казань",
    "срочный ремонт кондиционеров Казань"
  ]
}
```

Повторы дедуплицируются.

### Удалить ключи

```http
DELETE /campaign-drafts/{draft_id}/keywords
```

```json
{
  "keywords": [
    "слишком широкий ключ"
  ]
}
```

---

## 4. Минус-слова

### Заменить список минус-слов

```http
PATCH /campaign-drafts/{draft_id}/negative-keywords
```

```json
{
  "negative_keywords": [
    "бесплатно",
    "своими руками",
    "вакансии",
    "скачать",
    "инструкция"
  ]
}
```

---

## 5. Группы объявлений

### Создать группу

```http
POST /campaign-drafts/{draft_id}/ad-groups
```

```json
{
  "name": "Ремонт кондиционеров",
  "keywords": [
    "ремонт кондиционеров Казань",
    "срочный ремонт кондиционеров Казань"
  ]
}
```

### Изменить группу

```http
PATCH /campaign-drafts/{draft_id}/ad-groups/{group_id}
```

```json
{
  "name": "Срочный ремонт кондиционеров",
  "keywords": [
    "срочный ремонт кондиционера Казань",
    "мастер по кондиционерам Казань"
  ]
}
```

### Удалить группу

```http
DELETE /campaign-drafts/{draft_id}/ad-groups/{group_id}
```

---

## 6. Объявления

### Создать объявление

```http
POST /campaign-drafts/{draft_id}/ads
```

```json
{
  "ad_group_id": "adg_001",
  "title": "Ремонт кондиционеров в Казани",
  "text": "Диагностика, чистка, заправка фреоном. Выезд мастера.",
  "landing_url": "https://example.ru/remont-kondicionerov",
  "display_link_path": "remont-kondicionerov"
}
```

### Изменить объявление

```http
PATCH /campaign-drafts/{draft_id}/ads/{ad_id}
```

```json
{
  "title": "Срочный ремонт кондиционеров",
  "text": "Мастер по кондиционерам. Ремонт, обслуживание, чистка."
}
```

### Удалить объявление

```http
DELETE /campaign-drafts/{draft_id}/ads/{ad_id}
```

---

## 7. Автогенерация структуры

### Сгенерировать группы, ключи, минус-слова и объявления

```http
POST /campaign-drafts/{draft_id}/generate-structure
```

```json
{
  "topic": "ремонт и обслуживание кондиционеров",
  "region": "Казань",
  "group_count": 3,
  "keywords_per_group": 5
}
```

Это service-side генератор структуры (локальный/legacy helper). Он помогает быстро получить рабочий каркас кампании, но перед отправкой в live-слой структура должна пройти валидацию человеком/маркетологом.

Важно: поле `region` в запросе используется для генерации фраз, но не перезаписывает регион самого черновика. Регион черновика меняется через `PATCH /campaign-drafts/{draft_id}`.

---

## 8. Проверка и preview перед отправкой

### Проверить черновик

```http
POST /campaign-drafts/{draft_id}/validate
```

Проверяет:

- есть ли группы;
- есть ли объявления;
- есть ли ключи;
- корректный ли landing URL;
- задан ли бюджет;
- указан ли регион;
- есть ли минус-слова;
- есть ли дубли ключей.

### Preview payload для Яндекс Директ

```http
GET /campaign-drafts/{draft_id}/preview
```

Показывает JSON, который может быть отправлен в Yandex Direct по цепочке apply.

Этот payload служит входом для live-create цепочки: можно вручную проверить структуру перед отправкой.

---

## 9. Публикация черновика в Yandex Direct (stage chain)

### Применить черновик как живую цепочку

```http
POST /yandex/campaigns/live-create
```

```json
{
  "draft_id": "draft_123",
  "approved": true,
  "idempotency_key": "lc_2026_001",
  "dry_run": false,
  "start_date": "2026-06-11",
  "counter_ids": [123456],
  "utm_config": {
    "enabled": true,
    "campaign_slug": "kondicziony-kzn",
    "overwrite": false
  },
  "reason": "Подготовка новой кампании после ревью"
}
```

`utm_config` (необязательно):
- Если передан — **переопределяет** `draft.utm_config` для этой цепочки,
  не меняя сам драфт. Позволяет задать UTM при создании, даже если драфт
  был создан без UTM, или переопределить slug/overwrite на лету.
- Если не передан — используется `draft.utm_config` как fallback.
- `enabled=false` в переопределении отключает UTM-разметку для этой цепочки,
  даже если в драфте UTM был включён.

Поведение:

- `dry_run=true`: локальный preflight-режим.
  - сети не вызываются;
  - в `payload_preview` возвращаются этапы 1–4: `campaigns.add`, `adgroups.add`, `ads.add`, `keywords.add`;
  - `applied=false`, `campaign_id=null`, `stages_executed=[]`.
- `dry_run=false` + `DIRECTPILOT_MODE=live_write` + `approved=true` + `idempotency_key`:
  - выполняет живую цепочку `campaigns.add` → `adgroups.add` → `ads.add` → `keywords.add`;
  - возвращает `campaign_id`, `ad_group_ids`, `ad_ids`, `keyword_ids`, `stages_executed`;
  - `applied=true`.
- `DIRECTPILOT_MODE=mock/sandbox/live_readonly` + `dry_run=false`: write путь отклоняется **до любого сетевого вызова** (ошибка валидации).
- `idempotency_key` имеет отдельный кеш для preview и apply (`dry_run` учитывается в ключе).

Что исключено/нормализуется в цепочке:

- `negativekeywordsharedsets.add` остаётся `not_implemented`.
- Для групповых минус-слов используется `NegativeKeywords.Items` внутри `adgroups.add`.
  Блок `NegativeKeywords` опционален на v5: если в драфте нет минус-слов — блок опускается,
  если есть — передаётся с items. Пустой `Items` не отправляется.
- Минус-фразы со слэшем (`/` или `\\`) не отправляются в `adgroups.add`: Direct v5 отклоняет их
  с `code=5002: Используются недопустимые символы`. Например, вместо `б/у` используйте `бу`.
- `TextAd.DisplayLinkPath` не отправляется в `ads.add`: текущий Direct v5 `TextAd` add payload
  отклоняет это поле как неизвестное. Поле можно хранить в локальном драфте для readability,
  но live-create пока отправляет только `Title`, `Text`, `Href`.
- `TextCampaign.BiddingStrategy` отправляется как search-only: `Search.BiddingStrategyType=HIGHEST_POSITION`,
  `Network.BiddingStrategyType=SERVING_OFF`. Сети намеренно выключены для single-intent лендингов.
- `DailyBudget` отправляется как `{Amount, Mode}` без `Currency`; `Mode` обязателен, иначе Direct
  возвращает `error_code=8000` / `Отсутствует обязательный параметр Mode`. При обновлении
  TimeTargeting (`campaigns.update`) Direct также требует `DailyBudget.Mode` — DirectPilot
  автоматически читает текущий бюджет кампании и нормализует `SpendMode` → `Mode`.

Безопасность и ошибки:

- отказоустойчивость fail-closed: на любом `AddResults.Errors`/`Error`/`missing Id` цепочка прерывается;
- запись `live_create_campaign_failed` в аудит;
- endpoint не делает автоактивацию: новая Direct-кампания остаётся в `DRAFT`, пока её объявления не отправлены на модерацию;
- **важно:** DRAFT-кампании нельзя выводить через `campaigns.resume`. Для draft-to-moderation используйте Direct `ads.moderate` с `SelectionCriteria.Ids=[ad_ids]`. `campaigns.resume` остаётся только для уже существующих остановленных/приостановленных кампаний.

Регион показа (geo targeting):

- `adgroups.add` всегда содержит `RegionIds` — без гео-таргетинга v5 отклоняет запрос.
  Идентификаторы резолвятся из `draft.region` через локальную карту
  `_REGION_NAME_TO_V5_IDS` в `app/store.py` (helper `_resolve_region_to_ids`).
  Никаких внешних вызовов.
- Поддерживаемые регионы в Beta: `Казань` → `[43]`, `Москва` → `[213]`,
  `Санкт-Петербург` / `СПб` → `[2]`, `Россия` / `Russia` → `[225]`.
  Расширение — одна строка в карте.
- Неизвестный / пустой регион отклоняется **до любого** сетевого вызова
  `campaigns.add`: helper бросает `YandexDirectError` с именем региона
  (без токена), в аудит пишется `live_create_campaign_failed`,
  endpoint возвращает 502. Тот же режим работает и в dry-run — оператор
  видит ошибку одинаково в обоих сценариях.

---

## 10. Бюджет и ставки

### Обновить бюджет

```http
PATCH /campaign-drafts/{draft_id}/budget
```

```json
{
  "daily_budget": 1500,
  "monthly_budget": 45000,
  "strategy": "max_clicks"
}
```

Возможные стратегии:

```text
manual
max_clicks
max_conversions
weekly_budget
```

### Обновить ставки

```http
PATCH /campaign-drafts/{draft_id}/bids
```

```json
{
  "max_cpc": 80,
  "keyword_bids": {
    "ремонт кондиционеров Казань": 90,
    "чистка кондиционеров Казань": 70
  }
}
```

### Live Direct: изменить ставки существующих ключей

```http
POST /yandex/campaigns/{campaign_id}/bids
```

Безопасное изменение SearchBid и ContextBid для живых ключей по KeywordId.
Ставки указываются в **рублях** — конвертация в Direct micros (× 1 000 000) происходит внутри.

**Dry-run preview (по умолчанию, всегда безопасно):**

```json
{
  "approved": true,
  "idempotency_key": "bids-demo-001",
  "dry_run": true,
  "items": [
    {"keyword_id": 57440007797, "search_bid_rub": 250.0},
    {"keyword_id": 57440007798, "context_bid_rub": 100.0}
  ]
}
```

Ответ с `dry_run=true`: `applied=false`, `payload_preview` содержит точный v5 payload, который *был бы* отправлен (без реальной записи).

**Live apply (требует `DIRECTPILOT_MODE=live_write` + `approved=true`):**

```json
{
  "approved": true,
  "idempotency_key": "bids-apply-001",
  "dry_run": false,
  "items": [
    {"keyword_id": 57440007797, "search_bid_rub": 250.0}
  ],
  "reason": "Повышаем ставку на конверсионный ключ"
}
```

После полностью успешного apply endpoint читает текущие ставки ключей кампании и возвращает
`readback` с изменёнными `KeywordId` и текущими `Bid`/`ContextBid` в единицах Яндекс Директа
(**micros**, 250 000 000 = 250 ₽). При `partial_failure=true` readback не выполняется.

**Idempotency:** повтор с тем же `idempotency_key` возвращает кешированный
результат только при совпадении `dry_run` и эквивалентного тела запроса;
если payload/material item set отличается — возвращается `HTTP 409` (без повторного
write и без утечки тела/токена в ошибке).

**Safety gates:**
- `approved=false` → HTTP 409 до любого сетевого вызова.
- `dry_run=false` в `live_readonly` → HTTP 409.
- replay с тем же `idempotency_key`, но другим `dry_run`/payload → HTTP 409 до сетевого write.
- top-level failure от `keywordbids.set` / upstream Direct error → HTTP 502 с редактированными diagnostics.
- `dry_run=true` всегда разрешён, никогда не пишет.

### Live Direct: read keyword bids (canonical KeywordBids.get)

```http
GET /yandex/campaigns/{campaign_id}/keyword-bids
```

Canonical typed read endpoint for current Yandex Direct v5 `keywordbids.get` data.
The older `GET /yandex/campaigns/{campaign_id}/bids` remains available only as a
deprecated raw `bids.get` compatibility route; it returns `YandexRawResult`.
`POST /yandex/campaigns/{campaign_id}/bids` is still the existing manual
`keywordbids.set` endpoint for fixed SearchBid/ContextBid changes and is not
deprecated.

Query parameters are typed: optional `ad_group_ids`, `keyword_ids`,
`serving_statuses`, positive `limit`, and non-negative `offset`. The route is
always constrained by the path `campaign_id`; selector limits follow Direct:
up to 1 campaign here, up to 1,000 ad groups, and up to 10,000 keyword IDs.

Response is read-only (`read_only=true`) and includes:

- `items[]` with `campaign_id`, `ad_group_id`, `keyword_id`, `row_kind`
  (`keyword`, `autotargeting`, or `unknown`), optional keyword text, serving
  status, strategy priority, Search bid/autotargeting-auto data, auction bids,
  Network bid, and coverage;
- RUB-normalized fields plus explicit Direct micros fields;
- `limited_by` / `next_offset` for provider `LimitedBy` pagination;
- sanitized warnings only.

DirectPilot reads the current campaign strategy first and requests only safe
fields: no Search auction bids when Search is `SERVING_OFF`, and no Network
coverage when Network is `SERVING_OFF`. Row kind is enriched from `keywords.get`
(`Keyword == "---autotargeting"`); unresolved rows are reported as `unknown`,
not guessed from missing auction/coverage fields. Provider errors are sanitized.

### Live Direct: calculate automatic keyword bids (KeywordBids.setAuto)

```http
POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto
```

Typed wrapper for Yandex Direct v5 `keywordbids.setAuto`. This endpoint asks
Direct to **calculate bids** for existing campaign/ad-group/keyword targets. It
is **not** autotargeting settings, does not create/delete/convert
`---autotargeting` rows, does not switch campaign strategy, and does not change
payment model. Use `/autotargeting` for autotargeting categories and `/strategy`
for strategy conversion.

Request scopes are homogeneous: choose exactly one `scope`.

- `scope="campaign"`: target the path campaign; do not send `ad_group_ids` or
  `keyword_ids`.
- `scope="ad_group"`: send unique `ad_group_ids` (1..1,000), all owned by the
  path campaign.
- `scope="keyword"`: send unique `keyword_ids` (1..10,000), all owned by the
  path campaign; autotargeting rows are rejected for keyword scope.

Rules are a discriminated union; choose exactly one:

```json
{
  "scope": "keyword",
  "keyword_ids": [57440007797],
  "rule": {
    "type": "search_by_traffic_volume",
    "target_traffic_volume": 85,
    "increase_percent": 0,
    "bid_ceiling_rub": 250.0
  },
  "dry_run": true,
  "approved": false,
  "idempotency_key": "optional-preview-key",
  "reason": "Preview calculated search bids"
}
```

- `search_by_traffic_volume`: Direct
  `SearchByTrafficVolume.TargetTrafficVolume` is `5..100`;
  `IncreasePercent` is `0..1000`; DirectPilot requires positive
  `bid_ceiling_rub` and previews Direct `BidCeiling` in micros
  (`250.0` RUB → `250000000`). Compatible only with Search strategy
  `HIGHEST_POSITION`.
- `network_by_coverage`: Direct `NetworkByCoverage.TargetCoverage` is `0..100`;
  `IncreasePercent` is `0..1000`; positive `bid_ceiling_rub` is required.
  Compatible only with Network strategy `MAXIMUM_COVERAGE` or `MANUAL_CPM`.

`dry_run=true` is the default and never mutates Yandex. It returns
`applied=false`, `payload_preview` with the exact `setAuto` payload,
`affected_items`, warnings, and any `blockers`. Schema validation errors can be
HTTP 422 before business validation.

Real apply requires all gates: explicit human approval of the dry-run/diff,
`DIRECTPILOT_MODE=live_write`, `approved=true`, valid `idempotency_key`, and
`dry_run=false`. Idempotency fingerprints the material `setAuto` payload; exact
replay returns the cached result, while the same key with a different payload is
rejected before mutation. The endpoint never auto-switches strategy to make a
request compatible.

Safety behavior:

- Incompatible strategy, mixed selectors, ownership mismatch, missing affected
  targets, keyword-scope autotargeting, or provider `LimitedBy` before write
  blocks before mutation with `blocked=true`, `applied=false`, and sanitized
  blockers.
- Large keyword-scope requests may include up to 10,000 keyword IDs. If a
  pre-write read is truncated by Direct `LimitedBy`, the endpoint fails closed
  before `setAuto`; it does not mutate a partially verified scope.
- After a successful provider apply, DirectPilot performs a same-scope
  `keywordbids.get` readback because `setAuto` returns per-item outcomes but not
  the calculated bids.
- Per-item `SetAutoResults` errors are sanitized into `set_auto_results`; if any
  item has errors, the response reports `applied=false`, `partial_failure=true`.
- If the write succeeded but post-apply readback is truncated or fails, the
  response is truthful: `applied=false`, `partial_failure=true`,
  `readback_failed=true`, plus sanitized `verification_error`. It never claims
  rollback and never fabricates calculated bids.

### Live Direct: читать и изменять/создавать корректировки ставок

```http
POST /yandex/campaigns/{campaign_id}/bid-modifiers
```

Оба маршрута (`.../bid-modifiers` и `.../bid-modifiers/create`) по умолчанию работают в `dry_run=true` и не применяют изменения до явного разрешённого live-запуска.

Для создания новых корректировок используйте отдельный эндпоинт:

```http
POST /yandex/campaigns/{campaign_id}/bid-modifiers/create
```

Он работает через `bidmodifiers.add` и поддерживает source-backed create families: устройства (`MOBILE_ADJUSTMENT`, `TABLET_ADJUSTMENT`, `DESKTOP_ADJUSTMENT`, `DESKTOP_ONLY_ADJUSTMENT`, `SMART_TV_ADJUSTMENT`/`SMARTTV_ADJUSTMENT`), пол/возраст (`DEMOGRAPHICS_ADJUSTMENT`), ретаргетинг (`RETARGETING_ADJUSTMENT`), регионы (`REGIONAL_ADJUSTMENT`), видео (`VIDEO_ADJUSTMENT`; legacy alias `VIDEO_EXTENSION_ADJUSTMENT`), smart ads, эксклюзивное размещение (`SERP_LAYOUT_ADJUSTMENT`), платежеспособность (`INCOME_GRADE_ADJUSTMENT`) и group-level coefficient (`AD_GROUP_ADJUSTMENT`). `WEATHER_ADJUSTMENT` create отключён: live Yandex Direct вернул `error_code=8000` / unknown parameter `WeatherAdjustment`, а публичный `bidmodifiers.add` contract не содержит этот block. Existing weather rows можно менять только update-flow по `modifier_id` (`Id + BidModifier`).

Ключевые ограничения интерфейса:
- `GET /yandex/campaigns/{campaign_id}/bid-modifiers` возвращает нормализованный список всех modifier items и безопасный raw provider block.
- `bidmodifiers.set` (``.../bid-modifiers``) меняет существующую корректировку только по `Id` + `BidModifier`.
- `POST /yandex/campaigns/{campaign_id}/bid-modifiers/create` создаёт новые строки через `bidmodifiers.add` и строит documented v5 shape: singleton blocks для устройств/video/smart/ad-group, plural array blocks для `DemographicsAdjustments`, `RetargetingAdjustments`, `RegionalAdjustments`, `SerpLayoutAdjustments`, `IncomeGradeAdjustments`.
- Official enum validation на create: `OperatingSystemType=IOS|ANDROID`, `Gender=GENDER_MALE|GENDER_FEMALE`, `Age=AGE_0_17|AGE_18_24|AGE_25_34|AGE_35_44|AGE_45|AGE_45_54|AGE_55`, `SerpLayout=ALONE|SUGGEST`, `Grade=VERY_HIGH|HIGH|ABOVE_AVERAGE`.
- `AD_GROUP_ADJUSTMENT` создаётся только с `ad_group_id`; campaign-level body для этого типа отклоняется.
- `WEATHER_ADJUSTMENT` create сейчас не поддерживается и отклоняется до provider call: live Yandex Direct вернул `error_code=8000` / unknown parameter `WeatherAdjustment`, а публичный `bidmodifiers.add` contract не содержит weather block. Для погоды используйте read-first existing-modifier update только по `modifier_id` (`Id + BidModifier`), если `bidmodifiers.get` вернул существующую weather row. `humidity`/`wind` и «Уровень трат в категории» также не реализованы до подтверждённого API/provider mapping.
- `adjustment_percent` поддерживает диапазон `-100..1200` и мапится в `BidModifier=100+adjustment_percent`; альтернативно можно передать прямой `bid_modifier` `0..1300`.
- `type_hint`, `age_range`, `conditions` — операторские preview-поля; в live `bidmodifiers.set` они не отправляются.

Для live apply сначала прочитайте текущие корректировки через:

```http
GET /yandex/campaigns/{campaign_id}/bid-modifiers
```

**Dry-run preview для исключения возраста 0–17:**

```json
{
  "approved": true,
  "idempotency_key": "bidmod-demo-001",
  "dry_run": true,
  "adjustments": [
    {"age_range": "AGE_0_17", "adjustment_percent": -100}
  ]
}
```

Ответ с `dry_run=true`: `applied=false`, `payload_preview` показывает операторскую семантику и Direct коэффициент:

```json
{
  "BidModifiers": [
    {"CampaignId": 710691939, "AgeRange": "AGE_0_17", "AdjustmentPercent": -100, "BidModifier": 0}
  ]
}
```

**Live apply:** передайте `modifier_id` существующей корректировки из readback и `dry_run=false`:

```json
{
  "approved": true,
  "idempotency_key": "bidmod-apply-001",
  "dry_run": false,
  "adjustments": [
    {"modifier_id": 987654, "age_range": "AGE_0_17", "adjustment_percent": -100}
  ]
}
```

Direct payload будет минимальным и безопасным:

```json
{"BidModifiers": [{"Id": 987654, "BidModifier": 0}]}
```

**Пример preview для существующей погодной корректировки:**

```json
{
  "approved": true,
  "idempotency_key": "bidmod-weather-001",
  "dry_run": true,
  "adjustments": [
    {
      "modifier_id": 112233,
      "type_hint": "Weather",
      "bid_modifier": 80,
      "conditions": {"WeatherType": "RAIN"}
    }
  ]
}
```

Для update live apply DirectPilot всё равно отправит только `{"Id": 112233, "BidModifier": 80}`. Создание новой погодной корректировки через `POST .../bid-modifiers/create` отключено: Direct `bidmodifiers.add` не признаёт `WeatherAdjustment` (`error_code=8000`). Не выдумывайте alternative shape; пока нет подтверждённого provider/API mapping, можно только менять коэффициент уже существующей weather row по `modifier_id`.

Применение доступно только при:
- `DIRECTPILOT_MODE=live_write`;
- `approved=true`;
- `idempotency_key`;
- `dry_run=false`;
- явном согласовании от пользователя (маркетолог/оператор показывают dry-run/diff до apply).

После `applied=true` endpoint делает readback через `bidmodifiers.get`; Direct `bidmodifiers.get` должен отправлять `SelectionCriteria.Levels=["CAMPAIGN","AD_GROUP"]`, иначе sandbox/live возвращает `error_code=8000` / missing `Levels`. `bidmodifiers.add` возвращает `AddResults[].Ids` (plural), это нормальный provider envelope.

Опциональный sandbox smoke для проверки реального `bidmodifiers.set` shape находится в `tests/test_yandex_bidmodifiers_sandbox_smoke.py`. Он по умолчанию пропущен и запускается только с `DIRECTPILOT_YANDEX_SANDBOX_SMOKE=bidmodifiers_set` и `YANDEX_DIRECT_SANDBOX_TOKEN`. Для выбора строки можно передать `YANDEX_DIRECT_SANDBOX_MODIFIER_ID`; если ID не задан, smoke делает `bidmodifiers.get` по `YANDEX_DIRECT_SANDBOX_CAMPAIGN_ID`, опционально фильтрует по `YANDEX_DIRECT_SANDBOX_MODIFIER_TYPE`, берёт существующий `Id`, выполняет no-op set текущего `BidModifier` и readback. Weather create в smoke не выполняется.

**Ошибки и диагностика:**
- Если `modifier_id` отсутствует при `dry_run=false`, endpoint возвращает `HTTP 409` до сетевого write.
- Провайдерские ошибки и непредвиденные исключения (`RuntimeError`/`Exception`) падают как `HTTP 502` с редактированными diagnostics и без токенов/секретов в теле.
- В таком кейсе добавляется аудит-событие `yandex_bid_modifiers_failed`.

**Pitfalls:**
- Не отправляйте `CampaignId/AgeRange/type_hint/conditions` в `bidmodifiers.set`: метод принимает только `Id + BidModifier` для существующего modifier.
- `approved=true` остаётся техническим флагом; сначала показывайте dry-run пользователю и ждите явного подтверждения.
**Human Approval Contract:**
`approved=true` — технический флаг, а не самосогласование агента.
Оператор/агент обязан сначала показать dry-run diff и получить явное
подтверждение пользователя перед apply.

**Форма v5 payload:**

Минимальный item для известного `KeywordId` (без `CampaignId`/`AdGroupId`):

```json
{"KeywordId": 57440007797, "SearchBid": 250000000}
```

Где `SearchBid` = `250 000 000` micros = `250 ₽`.

**Pitfalls:**
- Не смешивайте `CampaignId` + `AdGroupId` + `KeywordId` в одном item —
  Direct возвращает `error_code=9300`.
- Для автотаргетинга `AutotargetingSearchBidIsAuto="NO"` добавляется
  автоматически при ручной search-ставке (можно переопределить явно).
- Не меняйте `NetworkBid`, если РСЯ выключена (`SERVING_OFF`).
- При автостратегии (`WB_MAXIMUM_CONVERSION_RATE`) Direct может
  проигнорировать per-keyword ставки с warning 10160 —
  `provider_warnings` в ответе содержит предупреждения.
  Используйте `WeeklySpendLimit`/`BidCeiling` через strategy endpoint.

**Per-item SetResults (live apply):**

После live apply (`source="yandex"`, `dry_run=False`) ответ содержит
`set_results` — per-item исход каждого keyword из v5 `SetResults`:

```json
"set_results": [
  {"keyword_id": 1, "has_errors": false, "has_warnings": false, "errors": [], "warnings": []},
  {"keyword_id": 2, "has_errors": true,  "has_warnings": false,
   "errors": [{"code": 52, "message": "Ставка не задана", "details": ""}], "warnings": []}
]
```

- `has_errors=true` — этот keyword **не был изменён**. Если хотя бы один item
  имеет ошибки, общий `applied=false` и `partial_failure=true`.
- `has_warnings=true` — keyword изменён, но Direct вернул нефатальные
  предупреждения (например, `10160` — ставка проигнорирована автостратегией).
  Эти предупреждения также попадают в `provider_warnings`.
- Ошибки/предупреждения редиректятся: только `code`, `message`, `details`;
  сырой v5 envelope не включается.
- Если `SetResults` отсутствует в успешном ответе, `set_results=null`.
- Если верхний уровень v5 вернул `ok=false` / provider error, endpoint возвращает HTTP 502 и не считает apply успешным.
- `partial_failure=false` + `applied=true` — все item'ы применены успешно.

### Live Direct: стратегия максимум конверсий

Чтобы перевести поисковую текстовую кампанию с ручной стратегии `HIGHEST_POSITION` на максимум конверсий, обновите `TextCampaign.BiddingStrategy.Search`:

```json
{
  "method": "update",
  "params": {
    "Campaigns": [
      {
        "Id": 710691939,
        "TextCampaign": {
          "BiddingStrategy": {
            "Search": {
              "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
              "WbMaximumConversionRate": {
                "GoalId": 567732835,
                "WeeklySpendLimit": 7000000000,
                "BidCeiling": 1500000000
              }
            },
            "Network": {
              "BiddingStrategyType": "SERVING_OFF"
            }
          }
        }
      }
    ]
  }
}
```

`WeeklySpendLimit` и `BidCeiling` тоже указываются в микроденежных единицах: `7000000000` = `7000 ₽`, `1500000000` = `1500 ₽`.

**Pitfall:** Direct может вернуть warning `10162` / `Дневной бюджет сброшен`. Это ожидаемо: дневной бюджет имеет смысл для ручных стратегий, а максимум конверсий использует недельный бюджет `WeeklySpendLimit`.

**Pitfall:** when migrating a campaign from multi-goal `PriorityGoals` back to a single `goal_id`, Direct v5 must receive `TextCampaign.PriorityGoals=null` to clear the prior multi-goal set. Do not use `PriorityGoals.Items=[]`, and do not omit `PriorityGoals`; neither is the documented clear contract. For non-empty `PriorityGoals.Items`, every item must include `Operation="SET"` together with `GoalId` and `Value`.

**Pitfall:** for strategy live apply, a missing or null current `DailyBudget` is acceptable only when the readback strategy confirms `Search.WbMaximumConversionRate.BudgetType="WEEKLY_BUDGET"`. If DirectPilot cannot prove that shape, it fails closed before sending `campaigns.update`.

**Pitfall:** при автостратегии `WB_MAXIMUM_CONVERSION_RATE` ставки в `keywords.add` могут вернуться с warning `10160` / `Ставка не будет применена`: `Bid` не применяется из-за автобюджетной стратегии, `ContextBid` не применяется при выключенной РСЯ. Это нормально; управление идёт через `WeeklySpendLimit` и `BidCeiling` стратегии.

---

## 11. Yandex Direct read-only facade

В режиме `live_readonly` эти методы читают реальные production-данные Яндекс Директа и возвращают `source="yandex"`, `read_only=true`. В `mock`/`sandbox` fallback режимах возможен локальный демо-результат (`source="mock"`) для сравнения и отладки.

### Баланс аккаунта

```http
GET /yandex/account/balance
```

DirectPilot читает баланс через legacy Live v4, потому что это штатный метод для денег аккаунта:

- endpoint: `https://api.direct.yandex.ru/live/v4/json/`
- HTTP method: `POST`
- JSON method: `AccountManagement`
- action: `Get`

Минимальная форма upstream-запроса:

```json
{
  "method": "AccountManagement",
  "token": "[REDACTED_OAUTH_TOKEN]",
  "param": {
    "Action": "Get",
    "SelectionCriteria": {
      "Logins": ["ВАШ_ЛОГИН"],
      "AccountIDS": []
    }
  }
}
```

Важный нюанс Live v4: ответ может прийти как envelope `data.Accounts`, а не как список напрямую.
DirectPilot нормализует обе формы в список аккаунтов и парсит числовые строки (`"2781.27"`) как деньги.
Токен никогда не логируется и не возвращается наружу.

### Кампании

```http
GET /yandex/campaigns
```

### Группы кампании

```http
GET /yandex/campaigns/{campaign_id}/ad-groups
```

### Объявления кампании

```http
GET /yandex/campaigns/{campaign_id}/ads
```

### Ключи кампании

```http
GET /yandex/campaigns/{campaign_id}/keywords
```

### Сводный отчёт

```http
GET /yandex/reports/summary
```

Query-параметры (все опциональны):

- `date_from` — ISO дата начала периода (по умолчанию: 7 дней назад от сегодня).
- `date_to` — ISO дата конца периода (по умолчанию: сегодня).
- `campaign_id` — id кампании Yandex Direct; при наличии фильтрует reports-запрос через `SelectionCriteria.Filter` (`Field=CampaignId`, `Operator=IN`).

Возвращает агрегированные `spend / clicks / impressions / ctr / cpc` за выбранный
период.

- `DIRECTPILOT_MODE=mock` → `source="mock"`, детерминированный fallback payload.
- `sandbox` / `live_readonly` / `live_write` с настроенным `YANDEX_OAUTH_TOKEN` и
  доступным Yandex client → `source="yandex"`, `read_only=true`, реальный вызов
  `CAMPAIGN_PERFORMANCE_REPORT` (поля `Date, CampaignId, CampaignName, Impressions,
  Clicks, Cost, Ctr`).
- `sandbox` / `live_readonly` / `live_write` без доступного Yandex client или
  токена → HTTP **409**. Это осознанный отказ, а не silent mock.

`period` отражает запрошенный диапазон в формате `YYYY-MM-DD..YYYY-MM-DD`.

Пример валидации: если отчёт содержит 1 строку TSV с `impressions=25`, `clicks=0`,
`spend=0.0`, то endpoint вернёт `CTR=0`, `CPC=0` и `source="yandex"`.

### Поисковые запросы

```http
GET /yandex/reports/search-queries?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD&campaign_id=...
```

Возвращает реальные поисковые запросы за период с агрегацией на уровне query.

В каждом элементе включаются (или не включаются при пустых/недоступных данных):
`query`, `campaign_id`, `campaign_name` (nullable), `ad_group_id`, `impressions`, `clicks`, `ctr`, `cost`.

`cost` — это денежные затраты в ₽, уже в валюте отчёта (`Cost` из Yandex v5 report).
`campaign_name` заполняется из поля `CampaignName`, а если оно пустое/не передано,
сервис делает `campaigns.get` и добирает имя через `CampaignId`.

Endpoint использует источник `SEARCH_QUERY_PERFORMANCE_REPORT` v5 reports.

Источник/режимы:

- `DIRECTPILOT_MODE=mock` → `source="mock"`, детерминированный fallback payload.
- `sandbox` / `live_readonly` / `live_write` с настроенным `YANDEX_OAUTH_TOKEN` и
  доступным Yandex client → `source="yandex"`, `read_only=true`, реальный вызов
  `SEARCH_QUERY_PERFORMANCE_REPORT` (`Query, CampaignId, CampaignName, AdGroupId, Impressions, Clicks, Ctr, Cost`).
- `sandbox` / `live_readonly` / `live_write` без доступного Yandex client или
  токена → HTTP **409**. Это осознанный отказ, а не silent mock.

Поведение для маркетинговой декомпозиции:

- `ad_group_id` есть всегда при валидной строке отчёта; если его нет в строке TSV — строка отбрасывается.
- `campaign_id` после парсинга нормализуется в строковый id (например `710691939`).
- `campaign_name` fallback может быть `null`, если не удалось собрать имя по `CampaignId`.
- Неудалённые/битые строки TSV (не число в `Impressions/Clicks/Ctr` или короткая строка)
  отбрасываются, но endpoint всё равно возвращает 200.
- Если для периода реально нет показов — валидный ответ `items=[]`, `source="yandex"`,
  `read_only=true` (не 502 и не mock-fallback).

Опциональные query-параметры:

- `date_from`, `date_to` (YYYY-MM-DD) — диапазон периода, по умолчанию
  последние 7 дней.
- `campaign_id` — фильтр по конкретной кампании. В Reports API v5 фильтр
  передаётся через `SelectionCriteria.Filter = [{Field: "CampaignId",
  Operator: "IN", Values: ["..."]}]`, **не** через `SelectionCriteria.CampaignIds`
  (эта форма возвращает HTTP 400 на reports endpoint).
- `ReportName` должен быть уникальным для набора параметров отчёта. Live Direct
  вернул HTTP 400 `error_code=4000`, когда один и тот же `ReportName`
  использовался с разными fields/date/filter: `Отчет с таким названием, но с
  отличающимися параметрами уже сформирован или находится в очереди. Измените
  значение в параметре ReportName`. DirectPilot добавляет стабильный hash от
  `ReportType + SelectionCriteria + FieldNames`, чтобы внешний агент не
  повторял эту ошибку.

Практический разбор для маркетинга (campaign breakdown):

1. `GET /yandex/reports/search-queries?date_from=...&date_to=...` — получить все запросы.
2. Сверху сгруппировать по `campaign_id`/`campaign_name`; это даёт вклад каждой кампании.
3. Для выбранной кампании выполнить повторный вызов с `campaign_id=...` и
   сгруппировать дополнительно по `ad_group_id`.
4. Для каждого `query` считать эффективность: `cost`, `impressions`, `clicks`, `ctr`.
   - `clicks == 0` при заметном `impressions` + `cost > 0` → кандидаты в минусы/правку объявления/цели.
   - Высокий `cost` + низкий `ctr` при малом числе `impressions` часто означает
     смещение в нецелевые intent-запросы.
   - Учитывай только живые query-перечни в текущем периоде; пустой отчёт = отсутствие трафика по поисковым запросам, а не ошибка.
5. Сформировать roadmap только по фактам отчёта + гипотезы по ключам/минусам.

`period` отражает запрошенный диапазон в формате `YYYY-MM-DD..YYYY-MM-DD`; в
mock-режиме `period="last_7_days"`.

Пример валидации: если отчёт содержит 1 строку TSV с
`Query=ремонт квартир казань, CampaignId=710691939, CampaignName=Локальный сервис, AdGroupId=1001,
Impressions=540, Clicks=22, Ctr=4.07, Cost=660.00`, endpoint вернёт
`items=[{query: "ремонт квартир казань", campaign_id: "710691939", campaign_name: "Локальный сервис", ad_group_id: "1001", impressions: 540, clicks: 22, cost: 660.0, ctr: 4.07}]` с `source="yandex"`.

> **Маркетинговый контракт.** В live-режимах mock-фразы никогда не возвращаются.
> Видеть `source="mock"` в `sandbox` / `live_readonly` / `live_write` — баг
> конфигурации, а не ожидаемое поведение.

Также доступен raw-эндпоинт `GET /yandex/reports/search-queries-live?date_from=...&date_to=...`
(возвращает `YandexRawResult` с TSV-телом ответа). В отличие от универсального
`/yandex/reports/live/{REPORT_TYPE}`, этот diagnostic endpoint запрашивает именно
search-query поля `Query, CampaignId, AdGroupId, Impressions, Clicks, Ctr, Cost`,
чтобы не получить пустой отчёт из-за campaign-summary field set.

---

## 12. Pause / resume

Это ограниченный live-control блок. В текущем `live_readonly` режиме реальные write-вызовы заблокированы; `dry_run=true` доступен для проверки сценария без изменения Директа.

**Не использовать для DRAFT-кампаний.** `POST /yandex/campaigns/{campaign_id}/resume` не переводит новую черновую кампанию в модерацию. Для DRAFT после `live-create` нужно отправить DRAFT-объявления на модерацию через Direct API `ads.moderate` (`params.SelectionCriteria.Ids=[ad_ids]`). После успешного `ModerateResults` кампания переходит в `Status=MODERATION`, `State=ON`, а объявления остаются `Status=MODERATION`, `State=OFF` до решения модерации. Показы начнутся только после принятия модерацией при валидных группах, ключах, бюджете и расписании.

### Поставить кампанию на паузу

```http
POST /yandex/campaigns/{campaign_id}/pause
```

```json
{
  "approved": true,
  "idempotency_key": "pause-001",
  "dry_run": true,
  "reason": "Проверка безопасного pause flow"
}
```

### Возобновить кампанию

```http
POST /yandex/campaigns/{campaign_id}/resume
```

```json
{
  "approved": true,
  "idempotency_key": "resume-001",
  "dry_run": true,
  "reason": "Проверка безопасного resume flow"
}
```

Правила:

- применимо только к уже созданным и остановленным/приостановленным кампаниям, не к `DRAFT`;
- без `approved=true` вернётся ошибка;
- `idempotency_key` обязателен;
- при `dry_run=true` реальное состояние не меняется;
- audit log фиксирует запрос;
- реальные внешние write calls возможны только в отдельном режиме `live_write` при `dry_run=false`.

Reference live launch result: для кампании `710691939` `campaigns.resume` вернул per-item `Code=8300` (`Кампания является черновиком`). Корректный вызов `ads.moderate` для объявлений `17747346245..17747346249` вернул `ModerateResults` без ошибок; readback: campaign `Status=MODERATION`, `State=ON`, ads `Status=MODERATION`, `State=OFF`. Network strategy осталась `SERVING_OFF`.

---

## 12.1. TimeTargeting (расписание показов по часам)

### Чтение текущего расписания

Чисто read-only эндпоинт. Возвращает текущий блок `TimeTargeting` из v5 `campaigns.get` без каких-либо write-гейтов (`approved`, `idempotency_key` не требуются). Доступен во всех режимах: `mock`, `sandbox`, `live_readonly`, `live_write`.

```http
GET /yandex/campaigns/{campaign_id}/time-targeting
```

Ответ:

```json
{
  "campaign_id": "710691939",
  "campaign_name": "Ремонт кондиционеров Казань — поиск",
  "source": "yandex",
  "read_only": true,
  "time_targeting": {
    "Schedule": {
      "Items": [
        "1,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0",
        "2,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0",
        ...
        "7,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0"
      ]
    },
    "ConsiderWorkingWeekends": "NO",
    "HolidaysSchedule": null
  },
  "schedule": {
    "days": [
      {"hours": [0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0]},
      ...
    ]
  }
}
```

Поля:
- `time_targeting` — сырой блок из v5 (формат `Schedule.Items` со строками);
- `schedule` — нормализованное 7×24 представление для чтения человеком;
- `campaign_name` — имя кампании из Direct (live) или mock-метка;
- `source` — `"yandex"` в live-режимах, `"mock"` в mock-режиме;
- `read_only` — всегда `true`.

В mock-режиме возвращает детерминированное расписание (будни 08:00-22:00, выходные 10:00-18:00), без сетевых вызовов.

В live-режимах вызывает `campaigns.get` с полем `TimeTargeting` (read-only), никогда не вызывает `campaigns.update`. Ошибки Yandex Direct возвращаются как 502 без токена.

### Обновление расписания (write)

Безопасный write-эндпойнт для обновления `TimeTargeting` (почасовое расписание ставок / расписание показов) существующей кампании через v5 `campaigns.update`. В текущем `live_readonly` режиме реальный apply заблокирован; `dry_run=true` возвращает полный preview v5-пакета, который был бы отправлен.

```http
POST /yandex/campaigns/{campaign_id}/time-targeting
```

Тело запроса — одно из двух представлений:

### Полное расписание (7 x 24)

Позиционный список 7 дней (MONDAY..SUNDAY), каждый день — массив из 24 целых `BidPercent` (0..100, где 0 = пауза в этот час, 100 = полная ставка).

```json
{
  "approved": true,
  "idempotency_key": "tt-001",
  "dry_run": true,
  "schedule": {
    "days": [
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]},
      {"hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0]}
    ]
  },
  "timezone": "Europe/Moscow",
  "reason": "Проверка безопасного time-targeting flow"
}
```

### Плоский `hours` + опциональный `days` фильтр

Удобный шорткат: один и тот же 24-часовой шаблон на указанные дни. Дни, не указанные в `days`, будут выставлены в нули (пауза) — никакого тихого carry-over старого расписания. `days: null` / отсутствие поля означает «все 7 дней»; явный `days: []` означает «ни один день» и даст полностью нулевое расписание.

```json
{
  "approved": true,
  "idempotency_key": "tt-002",
  "dry_run": true,
  "hours": [0,0,0,0,0,0,0,0, 100,100,100,100,100,100,100,100,100,100,100,100,100,100, 0,0],
  "days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
  "timezone": "Europe/Moscow"
}
```

### Правила

- `schedule` и `hours` взаимоисключающие — передавать строго одно;
- `idempotency_key` обязателен (минимум 6 символов);
- `dry_run=true` — preview без сетевого вызова `campaigns.update`; в live-режимах выполняются до двух `campaigns.get` для чтения текущего `DailyBudget` и при необходимости `TextCampaign.BiddingStrategy`; в `mock`-режиме сетевых вызовов нет (локальный preview); доступен во всех режимах;
- `dry_run=false` в режимах `mock` / `sandbox` / `live_readonly` отклоняется с HTTP 409 до любого сетевого вызова;
- реальный apply (`dry_run=false` + `approved=true` + `idempotency_key`) возможен только в `live_write`;
- перед apply читается текущий `DailyBudget` кампании через `campaigns.get`; если у кампании есть дневной бюджет, блок `DailyBudget` с нормализованным `Mode` (из `SpendMode`) включается в `campaigns.update` payload — без него Direct возвращает error_code=8000 «Отсутствует обязательный параметр Mode»;
- если `DailyBudget` пришел как `null` (smart-strategy text campaign), endpoint дополнительно читает `Type` + `TextCampaign.BiddingStrategy` через отдельный `campaigns.get` с `TextCampaignFieldNames`; без найденного `BiddingStrategy` apply отклоняется с 502 (fail-closed), так как `campaigns.update` для таких кампаний требует сохранения strategy block даже при чистом TimeTargeting update;
- `BudgetType` из read-side `TextCampaign.BiddingStrategy.*` **сохраняется** в write-side payload — реальный API Яндекса возвращает его на GET и требует на UPDATE для стратегий `WbMaximumConversionRate` / `WbMaximumClicks`; удаление `BudgetType` вызывает error_code=8000 («Отсутствует обязательный параметр»);
- после apply делается v5 `campaigns.get` с полем `TimeTargeting` и возвращается в `readback` — оператор сверяет `readback.TimeTargeting` с `schedule_applied`;
- replay с тем же `(campaign_id, idempotency_key)` возвращает кешированный результат, сеть не дёргается;
- replay с тем же ключом, но другим `dry_run` — отклоняется с 502 (типизированная `YandexDirectError`);
- токен OAuth не возвращается в response и не пишется в audit;
- `timezone` — локальная метка часового пояса (метаданные, в v5 не отправляется, в audit и response возвращается).

### v5 контракт payload preview

`dry_run=true` возвращает:

```json
{
  "method": "campaigns.update",
  "params": {
    "Campaigns": [
      {
        "Id": 710691939,
        "DailyBudget": {"Amount": 5000000, "Mode": "STANDARD"},
        "TimeTargeting": {
          "Schedule": {
            "Items": [
              "1,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0",
              "2,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0",
              "...",
              "7,0,0,0,0,0,0,0,0,100,100,100,100,100,100,100,100,100,100,100,100,100,100,0,0"
            ]
          },
          "ConsiderWorkingWeekends": "NO",
          "HolidaysSchedule": null
        }
      }
    ]
  }
}
```

`DailyBudget` включается в preview только если кампания имеет дневной бюджет. `Mode` нормализуется из `SpendMode` (read-side) в `Mode` (write-side). Для smart-strategy кампаний (`DailyBudget: null`) preview также включает `TextCampaign.BiddingStrategy` (read-side стратегия с сохранением `BudgetType` — реальный API возвращает его на GET и требует на UPDATE; удаление `BudgetType` вызывает error_code=8000). Если чтение бюджета/стратегии сломалось или envelope неоднозначен, apply отклоняется с 502; `DailyBudget: null` — валидный признак отсутствия дневного бюджета.

Это literal shape из Direct API v5 docs (см. https://yandex.com/dev/direct/doc/ref-v5/campaigns/update.html), без выдуманных полей. `campaigns.update` для блока `TimeTargeting` — REPLACE-shaped: переданный блок атомарно заменяет предыдущее расписание; остальные поля кампании не затрагиваются.

---

## 12.2. Campaign Strategy (стратегия назначения ставок)

### Чтение текущей стратегии

Чисто read-only эндпоинт. Возвращает текущие поля кампании: `Type`, `State`, `Status`, `DailyBudget` (если есть), `CounterIds` (если есть), `TextCampaign.BiddingStrategy`. Без write-гейтов. Доступен во всех режимах.

```http
GET /yandex/campaigns/{campaign_id}/strategy
```

Ответ:

```json
{
  "campaign_id": "710691939",
  "campaign_name": "Ремонт кондиционеров Казань — поиск",
  "source": "yandex",
  "read_only": true,
  "campaign_type": "TEXT_CAMPAIGN",
  "state": "ON",
  "status": "ACCEPTED",
  "daily_budget": {"Amount": 5000000000, "SpendMode": "STANDARD"},
  "counter_ids": [123456],
  "priority_goals": null,
  "strategy": {
    "Search": {
      "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
      "WbMaximumConversionRate": {
        "GoalId": 567732835,
        "WeeklySpendLimit": 7000000000,
        "BidCeiling": 1500000000,
        "BudgetType": "WEEKLY_BUDGET"
      }
    },
    "Network": {
      "BiddingStrategyType": "SERVING_OFF"
    }
  },
  "strategy_summary": {
    "search": {
      "type": "WB_MAXIMUM_CONVERSION_RATE",
      "WbMaximumConversionRate": {
        "goal_id": 567732835,
        "weekly_spend_limit_rub": 7000.0,
        "bid_ceiling_rub": 1500.0,
        "budget_type": "WEEKLY_BUDGET"
      }
    },
    "network": {"type": "SERVING_OFF"}
  }
}
```

Поля:
- `strategy` — сырой блок `BiddingStrategy` из v5 `campaigns.get`;
- `strategy_summary` — нормализованный human-readable summary с микро→рубли конверсией;
- `priority_goals` — сырой `TextCampaign.PriorityGoals` блок (если есть multi-goal стратегия); включает `Items` с `{GoalId, Value}` в микроединицах;
- `source` — `"yandex"` в live-режимах, `"mock"` в mock-режиме;
- `read_only` — всегда `true`.

### Обновление стратегии (write)

Безопасный write-эндпоинт для обновления `TextCampaign.BiddingStrategy` существующей кампании через v5 `campaigns.update`. Поддерживает переключение поисковой стратегии на `WB_MAXIMUM_CONVERSION_RATE` (максимум конверсий) с одной или несколькими целями Метрики.

```http
POST /yandex/campaigns/{campaign_id}/strategy
```

#### Режим одной цели (backward-compatible)

Тело запроса:

```json
{
  "approved": true,
  "idempotency_key": "strategy-001",
  "dry_run": true,
  "strategy_type": "WB_MAXIMUM_CONVERSION_RATE",
  "goal_id": 567732835,
  "weekly_spend_limit": 7000.0,
  "bid_ceiling": 1500.0,
  "network": null,
  "reason": "Switch to conversion optimization"
}
```

Поля:
- `goal_id` (int, required exactly one of `goal_id`/`goal_ids`/`priority_goals`) — ID одной цели Метрики, которая станет текущим `GoalId` стратегии.

#### Режим нескольких целей (multi-goal optimization)

Оптимизация по нескольким целям одновременно. DirectPilot использует контракт Yandex Direct `TextCampaign.PriorityGoals` + `WbMaximumConversionRate.GoalId=13` (priority-goal strategy).

##### Равновесные цели (convenience path)

```json
{
  "approved": true,
  "idempotency_key": "multi-goal-001",
  "dry_run": true,
  "strategy_type": "WB_MAXIMUM_CONVERSION_RATE",
  "goal_ids": [567732835, 567732836, 567732837],
  "weekly_spend_limit": 7000.0,
  "bid_ceiling": 1500.0,
  "network": null,
  "reason": "Optimize for all three goals equally"
}
```

Поля:
- `goal_ids` (list[int], max 30, уникальные, положительные) — список id целей с равной ценностью конверсии по умолчанию 1.0 RUB за каждую.

##### Явные ценности конверсий

```json
{
  "approved": true,
  "idempotency_key": "priority-goals-001",
  "dry_run": true,
  "strategy_type": "WB_MAXIMUM_CONVERSION_RATE",
  "priority_goals": [
    {"goal_id": 567732835, "value": 1000.0},
    {"goal_id": 567732836, "value": 500.0}
  ],
  "weekly_spend_limit": 7000.0,
  "bid_ceiling": 1500.0,
  "network": null,
  "reason": "Primary and secondary conversion goals"
}
```

Поля:
- `priority_goals` (list[{goal_id, value?}], max 30, уникальные goal_id) — список целей с явной ценностью конверсии в **рублях**. Если `value` не указан, используется 1.0 RUB.

#### V5 contract для multi-goal

```json
{
  "method": "campaigns.update",
  "params": {
    "Campaigns": [
      {
        "Id": 710691939,
        "TextCampaign": {
          "BiddingStrategy": {
            "Search": {
              "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
              "WbMaximumConversionRate": {
                "GoalId": 13,
                "WeeklySpendLimit": 7000000000,
                "BidCeiling": 1500000000,
                "BudgetType": "WEEKLY_BUDGET"
              }
            },
            "Network": {
              "BiddingStrategyType": "SERVING_OFF"
            }
          },
          "PriorityGoals": {
            "Items": [
              {"GoalId": 567732835, "Value": 1000000000, "Operation": "SET"},
              {"GoalId": 567732836, "Value": 500000000, "Operation": "SET"}
            ]
          }
        }
      }
    ]
  }
}
```

`WbMaximumConversionRate.GoalId=13` — специальный маркер v5 для приоритетных целей на TEXT_CAMPAIGN. `PriorityGoals.Items` содержит до 30 целей с `Value` в микроединицах Direct (рубли × 1 000 000). Каждый элемент `Items` **обязательно** должен включать `"Operation": "SET"` — без этого поля Direct API v5 `campaigns.update` возвращает `error_code=8000` («отсутствует обязательное поле Operation»).

#### Правила

- `dry_run=true` — preview payload без сетевого вызова. Доступен во всех режимах;
- `dry_run=false` в `live_readonly`/`sandbox`/`mock` — **отклоняется с HTTP 409 до сетевого вызова**;
- реальный apply возможен только в `live_write` с `approved=true` + `idempotency_key` + `dry_run=false`;
- `weekly_spend_limit` и `bid_ceiling` — в **рублях** (публичное REST-соглашение). Конвертируются в Direct-микроединицы (× 1 000 000) строго в store-слое;
- `goal_id` **заменяет** текущий `GoalId` в стратегии кампании. Это не добавление цели в список: повторный вызов с другим `goal_id` переключит стратегию на новую цель;
- `goal_ids` и `priority_goals` используют `GoalId=13` и `PriorityGoals.Items` — Direct API v5 shape для мультицелевой оптимизации TEXT_CAMPAIGN;
- каждый элемент `PriorityGoals.Items` в multi-goal режиме **обязательно** содержит `"Operation": "SET"` — без него Direct v5 возвращает `error_code=8000`;
- `goal_id`, `goal_ids`, и `priority_goals` **взаимоисключающие** — нужно выбрать ровно один режим;
- `BudgetType` (например `WEEKLY_BUDGET`) сохраняется из readback-блока стратегии. Direct требует его при update; удаление `BudgetType` вызывает `error_code=8000`;
- Network-стратегия по умолчанию сохраняется из текущего состояния кампании. Endpoint не включает РСЯ молча;
- Для live-применения endpoint сначала делает readback кампании. Если `DailyBudget` нельзя надежно прочитать из `campaigns.get` (отсутствует или невалидная форма), apply отклоняется с `502` (fail-closed) до `campaigns.update`;
- после apply выполняется readback через `campaigns_get_full_strategy` для верификации;
- ответ включает `strategy_applied` (нормализованная конфигурация), `payload_preview` (на dry-run), `readback` (после apply), `provider_warnings`, `yandex_units`.

### Выбор цели Метрики

Для выбора `goal_id` используйте существующий read-only эндпоинт Метрики:

```http
GET /metrika/counters/{counter_id}/goals
```

Возвращает список целей счётчика. Выберите **один** нужный `goal_id` и передайте его в `POST /yandex/campaigns/{campaign_id}/strategy`. Этот `goal_id` станет единственной текущей целью оптимизации в стратегии, которую поддерживает данный endpoint.

---

## 13. Autotargeting settings (автотаргетинг)

Поисковые кампании Яндекс Директа (`TEXT_AD_GROUP` для Search / Search+YAN)
обязательно содержат автотаргетинг-строку (`---autotargeting`) на каждую
группу объявлений. Удалять или полностью выключать эту строку нельзя —
вместо этого настраиваются категории и brand-опции.

### Прочитать автотаргетинг кампании

```http
GET /yandex/campaigns/{campaign_id}/autotargeting
```

Read-only, доступен во всех режимах. Возвращает для каждой группы объявлений:

- `ad_group_id`, `ad_group_name`
- `autotargeting_keyword_id` — ID ключевой фразы `---autotargeting` в Yandex Direct
- `status`, `state`, `serving_status`
- `categories` — включённые категории автотаргетинга:
  `Exact`, `Narrow`, `Alternative`, `Accessory`, `Broader`
- `brand_options` — brand-опции:
  `WithoutBrands`, `WithAdvertiserBrand`, `WithCompetitorsBrand`
- `raw_provider` — сырой ответ Direct v5 `keywords.get` для отладки (redacted)

Пример ответа:

```json
{
  "campaign_id": "710691939",
  "source": "yandex",
  "read_only": true,
  "default_preset": "exact_narrow",
  "ad_groups": [
    {
      "ad_group_id": "12345",
      "ad_group_name": "Ремонт кондиционеров",
      "autotargeting_keyword_id": "67890",
      "status": "ACCEPTED",
      "state": "ON",
      "serving_status": "ELIGIBLE",
      "categories": {
        "Exact": "YES",
        "Narrow": "YES",
        "Alternative": "NO",
        "Accessory": "NO",
        "Broader": "NO"
      },
      "brand_options": {
        "WithoutBrands": "YES",
        "WithAdvertiserBrand": "YES",
        "WithCompetitorsBrand": "NO"
      },
      "raw_provider": {"Id": 67890, "Keyword": "---autotargeting"}
    }
  ]
}
```

### Обновить автотаргетинг кампании

```http
POST /yandex/campaigns/{campaign_id}/autotargeting
```

Стандартный write-gate контракт:
- `dry_run=true` (по умолчанию) — preview-only, показывает payload `keywords.update`
  (и `keywords.add`, если `create_missing=true`), `applied=false`, без сетевого вызова.
- `dry_run=false` требует `DIRECTPILOT_MODE=live_write`, `approved=true`
  и `idempotency_key`.

**Presets категорий:**

| Preset | Exact | Narrow | Alternative | Accessory | Broader |
|---|---|---|---|---|---|
| `exact_narrow` (default) | YES | YES | NO | NO | NO |
| `exact_narrow_broader` | YES | YES | NO | NO | YES |
| `custom` | caller-defined | | | | |

**Brand-option пресеты (по умолчанию):**
- `WithoutBrands=YES`, `WithAdvertiserBrand=YES`, `WithCompetitorsBrand=NO`
  (own-brand + no-brand — да; competitors — нет)

**Важное правило Direct API:** в `keywords.add` категории, не переданные явно,
Direct трактует как `YES` (все категории включены). DirectPilot всегда
отправляет все пять категорий и все три brand-опции явно (`YES` или `NO`),
чтобы избежать случайной активации всех категорий.

Пример запроса (dry-run, default preset):

```json
{
  "approved": true,
  "idempotency_key": "auto_001",
  "dry_run": true,
  "preset": "exact_narrow",
  "ad_group_ids": ["12345"],
  "reason": "Оставляем только Exact + Narrow для локальных услуг"
}
```

Пример запроса (custom categories):

```json
{
  "approved": true,
  "idempotency_key": "auto_002",
  "dry_run": true,
  "preset": "custom",
  "categories": {
    "exact": "YES",
    "narrow": "YES",
    "alternative": "YES",
    "accessory": "NO",
    "broader": "NO"
  },
  "brand_options": {
    "without_brands": "YES",
    "with_advertiser_brand": "YES",
    "with_competitors_brand": "NO"
  }
}
```

Минимальный Direct v5 payload для `keywords.update`:

```json
{
  "method": "update",
  "params": {
    "Keywords": [
      {
        "Id": 123,
        "AutotargetingSettings": {
          "Categories": {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "NO",
            "Accessory": "NO",
            "Broader": "NO"
          },
          "BrandOptions": {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO"
          }
        }
      }
    ]
  }
}
```

Поведение apply:

- Endpoint читает текущие `---autotargeting` строки через `keywords.get`,
  фильтрует по `ad_group_ids` (если указаны), строит `keywords.update`
  и отправляет.
- По умолчанию `create_missing=False`: если у группы нет автотаргетинг-строки —
  пропускается, пишется в `skipped_ad_group_ids`. Если ни одной строки
  не найдено — fail-closed (502) с подсказкой использовать
  `create_missing=true`.
- При `create_missing=True`: для групп без автотаргетинг-строк формируется
  `keywords.add` c `Keyword="---autotargeting"`, `AdGroupId` и явными
  `AutotargetingSettings` (все пять категорий + три бренд-опции).
  Dry-run показывает оба payload'а (`update` и `add`); apply вызывает
  `keywords.add` и `keywords.update`. Повтор с тем же `idempotency_key`
  возвращает кэшированный результат без дублирования вызовов.
  `create_missing=True` опасен — требует dry-run preview и apply gate
  (`live_write` + `approved` + `idempotency_key`).
- Используется `AutotargetingSettings`, **не** deprecated `AutotargetingCategories`.

**Маркетинговое правило:** для локальных сервисных поисковых кампаний
НЕ включайте все категории автотаргетинга по умолчанию. Default preset
`exact_narrow` безопасен. Broader — только по явному запросу пользователя
с осознанием trade-off по охвату.

---

## 14. Audit log

```http
GET /audit-log
```

Показывает события:

- создание черновика;
- изменение ключей;
- изменение групп/объявлений;
- validate/preview-related операции, где применимо;
- pause/resume запросы.

---

## 14. Рекомендации и apply flow

```http
GET  /recommendations
POST /recommendations/{recommendation_id}/approve
POST /recommendations/{recommendation_id}/reject
POST /actions/{action_id}/apply
```

`apply` требует approve и idempotency key. Для внешних изменений в Директе используется отдельный live-control слой `/yandex/campaigns/{campaign_id}/pause` и `/yandex/campaigns/{campaign_id}/resume`; общий recommendations/apply flow остаётся внутренним сценарием приложения.

---

## 15. Техническое примечание про DELETE

Некоторые DELETE endpoints в MVP принимают JSON body, например:

```http
DELETE /campaign-drafts/{draft_id}/keywords
```

Это допустимо для локального FastAPI/TestClient и удобно для agents, но при подключении внешнего API gateway/proxy это нужно пересмотреть: часть HTTP-клиентов и прокси может отбрасывать тело DELETE-запроса. Если появится такой шлюз, безопасная альтернатива — `POST .../remove`.

---

## 16. Утилиты

### UTM generator

```http
POST /utm/generate
```

### Budget simulation

```http
POST /simulations/budget
```

---

## Что НЕ реализовано специально

Пока product-live-first scope не включает:

- автоматическое создание `negativekeywordsharedsets.add` через отдельный endpoint;
- автоматическую автоактивацию/модерационный handoff после live-create.

Это сделано намеренно: текущий этап — контролируемый live-first. Для новой DRAFT-кампании следующий ручной шаг — отправить объявления на модерацию через `ads.moderate`, а не `campaigns.resume`. `campaigns.resume` используется только позже для already-created кампаний, которые были остановлены/приостановлены.


### Расширенный read-only/API-first слой Яндекс Директа

DirectPilot Beta теперь содержит обёртки для полного практического read-only и аналитического покрытия Direct API v5. Все методы работают через реальный OAuth-токен из переменной окружения, но значение токена нигде не сохраняется и не выводится.

#### Баланс, финансы и аналитика reports

```text
GET /yandex/account/balance?login=...                  -> Live v4 AccountManagement.Get
GET /yandex/campaigns/finance                          -> campaigns.get с Funds/Statistics
GET /yandex/reports/live/CAMPAIGN_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/ADGROUP_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/AD_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/CRITERIA_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/search-queries-live?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
```

Назначение: баланс общего счёта, текущий расход кампаний, статистика кампаний, групп, объявлений, условий показа и поисковых запросов. Ответ возвращается как read-only provider payload (`source=yandex`, `read_only=true`). Токен Live v4 передаётся только во внутреннем запросе и не возвращается наружу.

#### Яндекс Метрика

```text
GET /metrika/counters
GET /metrika/counters/{counter_id}/goals
GET /metrika/counters/{counter_id}/summary?date1=YYYY-MM-DD&date2=YYYY-MM-DD
GET /metrika/counters/{counter_id}/traffic-sources?date1=YYYY-MM-DD&date2=YYYY-MM-DD&limit=10
```

Назначение: счётчики, цели, визиты, пользователи, просмотры, отказы, средняя длительность визита, агрегированные достижения целей (`ym:s:anyGoalReaches`) и источники трафика (`ym:s:lastsignTrafficSource`). Метрика использует отдельный `YANDEX_METRIKA_OAUTH_TOKEN` и не зависит от Direct OAuth.

#### Настройки и диагностика кампаний

```text
GET /yandex/campaigns/{campaign_id}/keyword-bids      -> canonical keywordbids.get (typed read)
GET /yandex/campaigns/{campaign_id}/bids              -> legacy deprecated raw bids.get
POST /yandex/campaigns/{campaign_id}/bids             -> manual keywordbids.set (fixed SearchBid/ContextBid; gated)
POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto -> keywordbids.setAuto bid calculation (dry-run/apply gated)
GET /yandex/campaigns/{campaign_id}/bid-modifiers     -> bidmodifiers.get
POST /yandex/campaigns/{campaign_id}/bid-modifiers    -> bidmodifiers.set (dry-run/apply gated)
POST /yandex/campaigns/{campaign_id}/bid-modifiers/create -> bidmodifiers.add (dry-run/apply gated, documented families only; weather create unsupported)
GET /yandex/campaigns/{campaign_id}/negative-keywords?ids=1,2 -> negativekeywordsharedsets.get
GET /yandex/changes/check                             -> changes.check
GET /yandex/changes                                   -> changes.get
GET /yandex/dictionaries                              -> dictionaries.get
```

Назначение: ставки, корректировки ставок, минус-фразы, изменения в аккаунте и справочники Директа.

#### Аудит таргетингов и семантики Direct API v5

```text
GET /yandex/retargeting-lists                         -> retargetinglists.get
GET /yandex/campaigns/{campaign_id}/audience-targets  -> audiencetargets.get
GET /yandex/keywords-research/has-search-volume?keywords=...
GET /yandex/keywords-research/deduplicate?keywords=...
```

Назначение: аудит аудиторий, проверка наличия поискового объёма и дедупликация фраз. Direct API v5 `keywordsresearch` не содержит Wordstat create/get/delete; эти legacy v4 методы в текущем v5-клиенте возвращают `UNSUPPORTED_IN_V5` и не делают сетевой вызов.

#### Yandex AI Studio / Search API v2 Wordstat

Современный Wordstat API подключается отдельно через `YANDEX_SEARCH_API_KEY` и возвращает `source="yandex_search_api"`, `read_only=true`.

```text
GET /wordstat/top?phrase=ремонт&regions=43&limit=10
GET /wordstat/dynamics?phrase=ремонт&regions=43&date_from=2026-01-01T00:00:00Z&period=PERIOD_MONTHLY
GET /wordstat/regions?phrase=ремонт
GET /wordstat/regions-tree
```

Назначение: подбор и расширение семантики, динамика спроса и распределение по регионам без создания/удаления legacy v4 Wordstat-отчётов. Для `/wordstat/dynamics` даты должны соответствовать периоду API: `PERIOD_MONTHLY` начинается с первого дня месяца, `PERIOD_WEEKLY` — с понедельника и заканчивается воскресеньем.

#### Расширения объявлений и ассеты

```text
GET /yandex/campaigns/{campaign_id}/ad-assets  -> ads.get (detailed) + sitelinks.get + businesses.get + vcards.get — агрегированный аудит внешнего вида кампании (read-only)
GET /yandex/sitelinks      -> sitelinks.get (низкоуровневый helper; для аудита внешнего вида используйте /yandex/campaigns/{campaign_id}/ad-assets)
GET  /yandex/vcards        -> vcards.get
POST /yandex/vcards        -> vcards.add (dry-run по умолчанию; live-write только через approved + idempotency_key + dry_run=false; для реальной записи Direct требует campaign_id)
POST /yandex/ads/business  -> ads.update для привязки опубликованной организации BusinessId к TextAd (dry-run по умолчанию; live-write только через approved + idempotency_key + dry_run=false)
GET  /yandex/ad-images     -> adimages.get
GET /yandex/creatives      -> creatives.get
GET /yandex/feeds          -> feeds.get
GET /yandex/businesses     -> businesses.get
GET /yandex/agency-clients -> agencyclients.get
```

Назначение: агрегированный аудит внешнего вида объявлений (`ad-assets`), быстрые ссылки, визитки, BusinessId-привязка, изображения, креативы, фиды, организации и агентские клиенты. `GET /yandex/campaigns/{campaign_id}/ad-assets` — рекомендуемый endpoint для маркетолога: собирает все объявления с расширенными полями (Title, Title2, Text, Href, SitelinkSetId, BusinessId, VCardId, AdExtensions), разрешает быстрые ссылки, бизнесы и визитки в одном ответе. `GET /yandex/sitelinks` — низкоуровневый helper для прямого чтения наборов быстрых ссылок.

Scope rule: если пользователь спрашивает про **конкретную кампанию**, используйте campaign-scoped endpoints (`/yandex/campaigns/{campaign_id}/...`) или account-wide данные, явно связанные с кампанией через `CampaignId`/`AdGroupId`/`AdId`/`SitelinkSetId`/`BusinessId`/`VCardId` из campaign readback. Если пользователь спрашивает про **весь аккаунт**, используйте account-wide endpoints. Нельзя делать вывод о конкретной кампании только потому, что сущность есть в общем аккаунтном ответе (`GET /yandex/sitelinks`, `/yandex/businesses`, `/yandex/vcards`, finance/account reports и т.п.). Это правило касается не только быстрых ссылок, а всех данных: объявлений, ключей, минусов, бюджетов, отчетов, организаций, визиток и ассетов.

Практический контактный маршрут (детально: `docs/YANDEX_BUSINESS_CONTACTS.md`):

Если `POST /yandex/vcards` вернул `error_code=3500` (`Создание визиток не поддерживается`), не перебирать `vcard` payload — переходить в BusinessId-путь:

1) Подтвердите организацию:
   - `GET /yandex/businesses` и фильтр по `Id` в ответе `businesses.get`.
   - Прежде чем выполнять привязку, проверьте в найденной сущности поля: `Id`, `Name`, `Phone`, `ProfileUrl`, `IsPublished`, `Urls`, `HasOffice`.
   - Допуск только для опубликованных: `IsPublished == "YES"` (или эквивалент `true`) и совпадающего по вашему проверочному номеру телефона.

2) Сформируйте dry-run для `POST /yandex/ads/business`:
   - `approved: true`
   - `idempotency_key`
   - `dry_run: true`
   - `business_id` (например `11588384335`)
   - `campaign_id` или `ad_ids`

3) Применение (live-write): только после проверки dry-run и бизнес-апрува:
   - `approved: true`
   - `dry_run: false`
   - `idempotency_key` тот же (или новый по вашему process)
   - `DIRECTPILOT_MODE=live_write`

4) После apply выполните readback:
   - `GET /yandex/campaigns/{campaign_id}/ads` (или другой внутренний отчётный путь, который возвращает TextAd-поля);
   - ожидаем по каждому целевому ad_id: `TextAd.BusinessId == <business_id>`,
     `TextAd.PreferVCardOverBusiness == "NO"`, `TextAd.VCardId == null`.

Важное поведение endpoint `/yandex/ads/business`:
- если передан `campaign_id`, сервис читает все `ads.get` этой кампании;
- модифицирует только `TEXT_AD` (`Type == "TEXT_AD"`), другие типы (`IMAGE_AD` и т.д.) помечаются в `skipped`;
- всегда отправляет в `ads.update` вместе с `BusinessId` и `PreferVCardOverBusiness="NO"` поля `Title` / `Text` / `Href` (REPLACE-shape требования v5).

---

## 11. Работа с объявлениями в существующих live-кампаниях

### Просмотр и аудит минус-слов по группам кампании

```http
GET /yandex/campaigns/{campaign_id}/ad-groups/negative-keywords
```

Возвращает все группы кампании с текущим списком `negative_keywords` на уровне группы (из `NegativeKeywords.Items`) и флагом `has_negative_keywords`. Это read-only, write-гейты не применяются.

Ответные поля (каждый элемент):

- `ad_group_id` — id группы;
- `campaign_id` — id кампании;
- `name`, `status`;
- `negative_keywords` — нормализованный список;
- `has_negative_keywords` — есть ли ключи после нормализации;
- `source` (`mock`/`yandex`), `read_only=true`.

### Обновить минус-слова существующей группы

```http
POST /yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords
```

Обновляет `NegativeKeywords` для конкретной группы кампании через Direct v5 `adgroups.update`.

```json
{
  "negative_keywords": ["минус1", "минус2"],
  "operation": "add",
  "approved": true,
  "idempotency_key": "neg-2026-001",
  "dry_run": true
}
```

- `operation`: `add` (по умолчанию, дополняет текущий список с дедупликацией) или `replace` (полная замена).
- `negative_keywords` нормализуются: удаляется префикс `-`, trim, пустые и дублирующиеся значения отбрасываются.
- `dry_run=true`: preview-only, `applied=false`, `payload_preview` содержит `method="adgroups.update"` и `params.AdGroups[]` без сетевого вызова.
- `dry_run=false`: требует `approved=true`, `idempotency_key` и `DIRECTPILOT_MODE=live_write`.
- В `live_readonly/sandbox/mock` при `dry_run=false` — HTTP 409 до сети.
- На apply endpoint выполняет `adgroups.update`, возвращает `negative_keywords` (итог), `previous_negative_keywords` (исходный список), `provider_response` при успехе.

### Создать отдельную группу в существующей кампании

```http
POST /yandex/campaigns/{campaign_id}/ad-groups
```

Создаёт **только** ad-group в существующей кампании (`adgroups.add`). Не создаёт объявления, ключи и не запускает модерацию.

```json
{
  "name": "Ремонт по кухням",
  "region_ids": [213],
  "negative_keywords": ["дешево", "сделай сам"],
  "approved": true,
  "idempotency_key": "grp-2026-001",
  "dry_run": true
}
```

- `region_ids` обязателен и пробрасывается в `RegionIds`.
- `negative_keywords` (если есть) отправляются как `NegativeKeywords.Items` после нормализации.
- `dry_run=true`: preview-only, `payload_preview` + `warnings` о том, что создаётся только группа.
- `dry_run=false`: требует `approved=true`, `idempotency_key`, `DIRECTPILOT_MODE=live_write`; выполняется `adgroups.add`.
- Ответ: `ad_group_ids`, `add_results`, `provider_response`.

### Добавить объявления в live-группу

```http
POST /yandex/ad-groups/{ad_group_id}/ads
```

Добавляет текстовые объявления в существующую группу через Direct v5 `ads.add`.

Пример:

```json
{
  "approved": true,
  "idempotency_key": "ads_add_2026_001",
  "dry_run": true,
  "ads": [
    {
      "title": "Ремонт кондиционеров в Казани",
      "text": "Диагностика, чистка, заправка фреоном. Выезд мастера.",
      "href": "https://example.ru/remont-kondicionerov"
    },
    {
      "title": "Срочный ремонт кондиционеров",
      "title2": "Выезд за 30 минут",
      "text": "Мастер по кондиционерам. Ремонт, обслуживание, чистка.",
      "href": "https://example.ru/srochnyj-remont",
      "sitelink_set_id": 555,
      "business_id": 12345,
      "prefer_vcard_over_business": "NO"
    }
  ],
  "reason": "Добавляем поисковые объявления в группу «Ремонт кондиционеров»"
}
```

Поведение:

- `dry_run=true`: возвращает `payload_preview` (редактированный) и `warnings` без сетевого вызова.
- `dry_run=false` + `DIRECTPILOT_MODE=live_write` + `approved=true` + `idempotency_key`:
  выполняет живой `ads.add`, возвращает `ad_ids`, `add_results`, `readback` (если доступен).
- `DIRECTPILOT_MODE=live_readonly/sandbox/mock` + `dry_run=false`: отклоняется с HTTP 409 до сетевого вызова.

Поля объявления (Direct v5 `TextAd` add shape):

- `title` (обязательное) — заголовок.
- `text` (обязательное) — текст объявления.
- `href` (обязательное) — ссылка.
- `title2` (опционально) — второй заголовок.
- `sitelink_set_id` (опционально) — ID набора быстрых ссылок.
- `business_id` (опционально) — ID организации Яндекс Бизнес.
- `prefer_vcard_over_business` (опционально) — `"YES"` / `"NO"`.

`DisplayLinkPath` намеренно исключён — текущий Direct v5 `ads.add` отклоняет это поле как неизвестное.

Предупреждения (warnings) в ответе:

- `inherit_business_id` — предложение переиспользовать BusinessId существующих объявлений.
- `inherit_sitelink_set_id` — предложение переиспользовать SitelinkSetId.
- `keywords_not_per_ad` — ключевые слова и минус-слова управляются на уровне кампании/группы, не на уровне объявления.

Кроме локальных `warnings`, ответ также содержит `provider_warnings` — предупреждения, возвращённые самим Яндекс Директом в v5-конверте ответа (поле `Warnings[]`):
- Каждое предупреждение: `code` (int), `message` (str), `details` (str).
- Например, warning `10165` «Параметр не будет применен» означает, что Директ проигнорировал одно из переданных полей. Поле `details` содержит уточнение (например, «Параметр DisplayUrlPath не поддерживается»), по которому можно понять, какой именно параметр не был применён.
- Проверяйте `provider_warnings` чтобы диагностировать нефатальные отклонения на стороне Яндекса.

Важно: endpoint **не меняет** ключевые слова и минус-слова. Если новому объявлению нужны дополнительные ключи/минуса — используйте отдельную задачу semantic-changes.

### Отправить объявления на модерацию

```http
POST /yandex/ads/moderate
```

Отправляет объявления на модерацию через Direct v5 `ads.moderate`.

Пример:

```json
{
  "approved": true,
  "idempotency_key": "mod_2026_001",
  "dry_run": true,
  "ad_ids": [99001, 99002],
  "reason": "Отправляем новые объявления на модерацию"
}
```

Поведение:

- `dry_run=true`: preview, applied=false.
- `dry_run=false` + `live_write`: живой вызов `ads.moderate`, возвращает `ModerateResults` и `readback`.
- Используется после `live-create` для перевода DRAFT-объявлений в MODERATION.
- **Не** используйте `campaigns.resume` для новых DRAFT-кампаний — это только для already-created stopped/suspended кампаний.
- Нет автоматической модерации внутри `ads.add` — moderate вызывается отдельно и явно.
- Ответ содержит `provider_warnings` с предупреждениями от Яндекс Директа (аналогично `ads.add`) — проверяйте на нефатальные отклонения.


---

## 13. Автотаргетинг (autotargeting) — чтение и обновление

Автотаргетинг — обязательный компонент для поисковых (`Search`) и поисково-сетевых (`Search+YAN`) текстовых групп объявлений (`TEXT_AD_GROUPs`). Прямое удаление/полное отключение автотаргетинга может быть невалидным — вместо этого настраиваются категории и бренд-опции.

DirectPilot не принимает настройки автотаргетинга молча. При создании/настройке кампании или группы объявлений агенты/воркфлоу должны явно спрашивать/выбирать настройки автотаргетинга.

### Default preset: exact_narrow

Для локальных сервисных поисковых кампаний (например, «сантехник», «электрик», «ремонт») дефолтный пресет:

- **Категории:** `Exact=YES`, `Narrow=YES`, `Alternative=NO`, `Accessory=NO`, `Broader=NO`
- **Бренд-опции:** `WithoutBrands=YES`, `WithAdvertiserBrand=YES`, `WithCompetitorsBrand=NO`

Это означает: автотаргетинг работает только по точным и узким запросам, без альтернатив и сопутствующих товаров. Бренд-поиск включён для своего бренда и безбрендовых запросов, но исключены запросы конкурентов.

`Broader=YES` допустим только при явном решении о расширении охвата (trade-off).

### Прочитать текущие настройки автотаргетинга

```http
GET /yandex/campaigns/{campaign_id}/autotargeting
```

Read-only, доступен во всех режимах. Возвращает для каждой группы объявлений:

- `ad_group_id`, `ad_group_name`
- `autotargeting_keyword_id` — ID строки `---autotargeting`
- `status`, `state`, `serving_status` (если доступны)
- `categories` — все пять категорий (`Exact`, `Narrow`, `Alternative`, `Accessory`, `Broader`)
- `brand_options` — все три бренд-опции (`WithoutBrands`, `WithAdvertiserBrand`, `WithCompetitorsBrand`)
- `raw_provider` — сырой блок для отладки/саппорта (токены отредактированы)

Пример ответа:

```json
{
  "campaign_id": "710691939",
  "source": "yandex",
  "read_only": true,
  "default_preset": "exact_narrow",
  "ad_groups": [
    {
      "ad_group_id": "456",
      "ad_group_name": "Ремонт кондиционеров",
      "autotargeting_keyword_id": "123",
      "status": "ACCEPTED",
      "state": "ON",
      "serving_status": "ELIGIBLE",
      "categories": {
        "Exact": "YES",
        "Narrow": "YES",
        "Alternative": "NO",
        "Accessory": "NO",
        "Broader": "NO"
      },
      "brand_options": {
        "WithoutBrands": "YES",
        "WithAdvertiserBrand": "YES",
        "WithCompetitorsBrand": "NO"
      }
    }
  ]
}
```

### Обновить настройки автотаргетинга

```http
POST /yandex/campaigns/{campaign_id}/autotargeting
```

Стандартный контракт гейтов продукта:

- `dry_run=true` (дефолт) — preview-only, возвращает точный `keywords.update` payload, без сетевого вызова.
- `dry_run=false` требует `DIRECTPILOT_MODE=live_write`, `approved=true` и валидный `idempotency_key`.

Пример: установить дефолтный пресет `exact_narrow`:

```json
{
  "approved": true,
  "idempotency_key": "auto_2026_001",
  "dry_run": true,
  "preset": "exact_narrow",
  "reason": "Устанавливаем дефолтный автотаргетинг для сервисной кампании"
}
```

Пример с явными категориями:

```json
{
  "approved": true,
  "idempotency_key": "auto_2026_002",
  "dry_run": true,
  "preset": "custom",
  "categories": {
    "exact": "YES",
    "narrow": "YES",
    "alternative": "YES",
    "accessory": "NO",
    "broader": "NO"
  },
  "brand_options": {
    "without_brands": "YES",
    "with_advertiser_brand": "YES",
    "with_competitors_brand": "NO"
  }
}
```

### Пресеты категорий

| Пресет | Exact | Narrow | Alternative | Accessory | Broader |
|--------|-------|--------|-------------|-----------|---------|
| `exact_narrow` (дефолт) | YES | YES | NO | NO | NO |
| `exact_narrow_broader` | YES | YES | NO | NO | YES |
| `custom` | явно указаны | явно указаны | явно указаны | явно указаны | явно указаны |

### Пресеты бренд-опций

| Пресет (в коде) | WithoutBrands | WithAdvertiserBrand | WithCompetitorsBrand |
|-----------------|---------------|---------------------|----------------------|
| `own_no_competitors` (дефолт) | YES | YES | NO |

### Dry-run payload

Dry-run (и apply) всегда отправляет все пять категорий и все три бренд-опции **явно** (`YES` или `NO`). Это предотвращает pitfall Direct API: в `keywords.add` категории, не указанные явно, трактуются как включённые (`YES`).

Минимальный Direct payload для `keywords.update`:

```json
{
  "method": "update",
  "params": {
    "Keywords": [
      {
        "Id": 123,
        "AutotargetingSettings": {
          "Categories": {
            "Exact": "YES",
            "Narrow": "YES",
            "Alternative": "NO",
            "Accessory": "NO",
            "Broader": "NO"
          },
          "BrandOptions": {
            "WithoutBrands": "YES",
            "WithAdvertiserBrand": "YES",
            "WithCompetitorsBrand": "NO"
          }
        }
      }
    ]
  }
}
```

### Важные правила

1. Endpoint делает read-before-write: читает текущие строки `---autotargeting` через `keywords.get` и строит `keywords.update` по `Id`.
2. Если группа не имеет автотаргетинг-строки, эндпойнт **пропускает** её (пишет в `skipped_ad_group_ids`) при `create_missing=False`. Если ни одной строки не найдено — fail-closed (502) с подсказкой использовать `create_missing=true`. При `create_missing=True` создаются новые строки через `keywords.add` (явные категории + бренд-опции, dry-run/apply gate).
3. **Не использует** deprecated поле `AutotargetingCategories` — только `AutotargetingSettings` с `Categories` + `BrandOptions`.
4. `ad_group_ids` (опционально) — список ID групп для таргетинга. Если не указан — обрабатываются все группы кампании с автотаргетинг-строками.
5. Пустой `ad_group_ids` отклоняется валидацией (HTTP 422).

---

## UTM-разметка (audit / plan / apply)

UTM-метки — параметры в URL для отслеживания источников трафика в Яндекс Метрике и других системах аналитики.

### Конвенция по умолчанию

| Параметр | Значение |
|---|---|
| `utm_source` | `yandex` |
| `utm_medium` | `cpc` |
| `utm_campaign` | `<campaign_slug>` (генерируется из названия + id кампании, или задаётся вручную) |
| `utm_content` | `{ad_id}` (автоподстановка из данных объявления) |
| `utm_term` | `{keyword}` *(planned)* — в текущей версии лимит: авто-заполнение keyword-level не реализовано, поле обычно пустое |

Поддерживаются `custom_params` — дополнительные параметры запроса (напр. `utm_custom=extra`, `ref=dp`), которые не переопределяют пять основных UTM.

### Аудит

```http
GET /yandex/campaigns/{campaign_id}/utm-audit
```

Read-only. Возвращает статус UTM по каждому объявлению (`entity_type=ad`) и каждой быстрой ссылке (`entity_type=sitelink`):

```json
{
  "campaign_id": "710382063",
  "source": "yandex",
  "read_only": true,
  "ads_total": 3,
  "sitelinks_total": 4,
  "complete_count": 1,
  "partial_count": 1,
  "missing_count": 5,
  "mismatch_count": 0,
  "items": [
    {
      "entity_type": "ad",
      "entity_id": "12345",
      "url": "https://example.com/page",
      "utm_status": "missing",
      "present_params": {},
      "missing_params": ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"],
      "wrong_values": {}
    }
  ],
  "warnings": []
}
```

### План/preview

```http
POST /yandex/campaigns/{campaign_id}/utm-plan
```

**Всегда dry_run** — никогда не пишет в Яндекс. Всегда `applied=false`.

```json
{
  "campaign_slug": "remont-kvartir-710382063",
  "overwrite": false,
  "custom_params": {"utm_custom": "extra"},
  "include_sitelinks": true
}
```

Ответ: `UtmPlanResult` с `items` (объявления), `sitelink_items` (быстрые ссылки old_url → new_url), `payload_preview` (v5 `ads.update` payload), `warnings`, `not_implemented`.

Если `campaign_slug` не передан — генерируется автоматически из названия + id кампании.
**Кириллические названия:** если имя кампании содержит только кириллицу,
автоматический slug вырождается в ``campaign-{id}`` (транслитерация отбрасывает
не-ASCII символы). Для читаемых отчётов в Метрике передавайте `campaign_slug`
явно — латиницей или смысловым slug.

### Применение

```http
POST /yandex/campaigns/{campaign_id}/utm-apply
```

Write-gated:
- `dry_run=true` — preview, `applied=false`.
- `dry_run=false` требует **всех** условий:
  1. `DIRECTPILOT_MODE=live_write`
  2. `approved=true`
  3. `idempotency_key` (>= 6 символов)

```json
{
  "approved": true,
  "idempotency_key": "utm-2026-06-14-001",
  "dry_run": false,
  "campaign_slug": "remont-kvartir-710382063",
  "overwrite": false,
  "include_sitelinks": true,
  "reason": "Плановая разметка UTM для кампании апрель 2026"
}
```

Ответ: `UtmApplyResult` с `ad_ids`, `readback` (подтверждение новых URL объявлений), `sitelink_items`, явным `sitelink_readback` после успешного `sitelinks.update`, `provider_warnings`, `not_implemented`.

### Обработка URL

- Сохраняются существующие query-параметры и fragment (`#prices`, `#services`).
- `overwrite=false` (по умолчанию) — не трогает существующие UTM-параметры.
- `overwrite=true` — перезаписывает все UTM-параметры.
- Если UTM уже complete и `overwrite=false` — объявление пропускается с warning.

### Безопасность

- `ads.update` (REPLACE-shaped) — все обязательные поля (Title, Text, Href) переотправляются.
- `sitelinks.update` получает существующую структуру набора быстрых ссылок с изменённым только `Href`; `Title`/`Description` и структура набора сохраняются.
- `BusinessId`, `SitelinkSetId`, `VCardId`, `Title2` сохраняются.
- Токены никогда не появляются в ответах, логах или preview.

### Не реализовано (not_implemented)

- **utm_term автоподстановка:** параметр `utm_term={keyword}` пока не заполняется автоматически — требуется keyword-level mapping.

---

## 21. Миграция landing URL существующих объявлений и быстрых ссылок

Эти endpoint предназначены для безопасной замены URL уже существующих `TEXT_AD`. По умолчанию запросы выполняют только preflight/preview (`dry_run=true`) и не вызывают write-методы Яндекс Директа.

### URL основных объявлений

```http
POST /yandex/campaigns/{campaign_id}/ads/landing-urls
```

Тело содержит непустой список `items` с `ad_id`, `expected_href` и `target_href`. `expected_href` — optimistic-concurrency guard: если фактический `TextAd.Href` уже отличается, запрос завершается с HTTP 409 и ничего не записывает. Endpoint принимает только объявления указанной кампании и типа `TEXT_AD`.

### URL быстрых ссылок

```http
POST /yandex/campaigns/{campaign_id}/sitelinks/migrate-urls
```

Тело содержит `source_sitelink_set_id`, точный снимок `expected_items` и новый список `target_items`; у списков должно быть одинаковое число элементов (1–8). DirectPilot требует полного совпадения текущего source set с `expected_items`, затем сканирует все ссылки на набор в аккаунте с пагинацией.

Миграция является clone-and-reattach:
1. новый набор создаётся через документированный `sitelinks.add`;
2. к объявлениям выбранной кампании, которые ссылались на исходный набор, привязывается новый `SitelinkSetId` через `ads.update`;
3. исходный набор не изменяется и не удаляется.

Этот workflow не вызывает `sitelinks.update` и не перепривязывает объявления других кампаний. Все найденные связи видны в `reference_scan`.

### Единая миграция

```http
POST /yandex/campaigns/{campaign_id}/landing-url-migrations
```

Тело объединяет `ad_items` и `sitelink_migration`. Операция не транзакционная: сначала при необходимости создаётся и проверяется clone быстрых ссылок, затем выполняется `ads.update` для URL и/или нового `SitelinkSetId`.

### URL preflight и защита

Перед preview/apply каждый `target_href` проверяется:
- только HTTPS и hostname из `DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS`;
- запрещены credentials в URL, private/non-public DNS addresses и уход redirect на другой host;
- не более трёх redirect; конечный ответ должен быть 2xx (404 и другие ошибки блокируют операцию);
- fragment проверяется по реальному `id`/`name` anchor на странице;
- query-параметры должны стоять до fragment.

Пустой allowlist блокирует операцию fail-closed.

### Apply gates и результат

Реальная запись разрешена только при одновременном выполнении всех условий:
1. `DIRECTPILOT_MODE=live_write`;
2. `approved=true`;
3. `dry_run=false`;
4. непустой `idempotency_key` длиной не менее 6 символов;
5. пользователь увидел точный preview/diff и явно подтвердил именно этот apply.

Ответ `UrlMigrationResult` содержит `changes`, `payload_preview`, стадии, per-item `provider_results`, `readback`, `reference_scan`, `new_sitelink_set_id` и `recovery_note`. Ошибка хотя бы одного provider item или несовпадение readback даёт `applied=false`, `completed=false`, `partial_failure=true`; слепой rollback запрещён — сначала нужен новый read/preflight. Idempotency cache сейчас process-local и теряется после рестарта, поэтому ключ не является межпроцессной гарантией.
