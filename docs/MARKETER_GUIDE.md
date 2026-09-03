# DirectPilot Beta — guide for marketer

Цель: маркетолог работает с рекламными и аналитическими данными через DirectPilot, а не напрямую через Yandex Direct/Metrika/Wordstat API.

## Главный принцип

Продуктовый путь сейчас — `live-only` + active testing в `live_readonly`:

- маркетолог использует **только живые DirectPilot endpoints (`/yandex/*`, `/metrika/*`, `/wordstat/*`) в режиме чтения**;
- `mock`/`sandbox`/`/demo/*` — legacy fallback и не используются в рабочем маркетинг-процессе;
- live-write допускается только через отдельный контролируемый режим `live_write` с `approved=true`, `dry_run=false`, `idempotency_key` и audit-log.

DirectPilot — единая прослойка для маркетолога:

- читает реальные данные Яндекс Директа в безопасном режиме `live_readonly`;
- читает Метрику через отдельные endpoints DirectPilot;
- читает Wordstat/Search API через отдельные endpoints DirectPilot;
- скрывает токены, OAuth headers и сырые API-ошибки;
- живые изменения только через отдельную систему одобрения.

Маркетолог не должен сам выбирать raw Yandex API methods. Он выбирает задачу → endpoint DirectPilot.

## Decision map

| Задача маркетолога | Endpoint DirectPilot | Что использовать в выводе |
|---|---|---|
| Проверить, живо ли приложение | `GET /health` | статус приложения |
| Проверить Direct-интеграцию | `GET /integrations/yandex/direct/status` | ok/status без секретов |
| Посмотреть реальные кампании | `GET /yandex/campaigns` | id, name, status, type |
| Посмотреть ключи | `GET /yandex/campaigns/{campaign_id}/keywords` | семантика, минус-гипотезы, дубли |
| Посмотреть группы кампании | `GET /yandex/campaigns/{campaign_id}/ad-groups` | структура групп |
| Посмотреть объявления | `GET /yandex/campaigns/{campaign_id}/ads` | тексты, ссылки, статусы, business/vcard fields если есть |
| Посмотреть ставки ключей кампании | `GET /yandex/campaigns/{campaign_id}/keyword-bids` | canonical typed `keywordbids.get`; use this instead of deprecated raw `GET .../bids` |
| Посмотреть прогноз аукциона по ключам | `GET /yandex/campaigns/{campaign_id}/auction-forecast` | read-only per-keyword `TrafficVolume` levels + `forecast_status`/`forecast_reason` normalization |
| Рассчитать авто-ставки для ключей | `POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto` | `keywordbids.setAuto` bid calculation only; dry-run default; apply requires explicit human approval + `live_write` + `approved=true` + idempotency |
| Посмотреть демографические ставки кампании | `GET /yandex/campaigns/{campaign_id}/bid-modifiers` | текущие `AgeRange`/`BidModifier` для `Age` сегментов |
| Обновить демографические корректировки | `POST /yandex/campaigns/{campaign_id}/bid-modifiers` | `dry_run=true` для preview; apply — `live_write` + `approved=true` + `idempotency_key`; read existing rows через GET и передавайте `modifier_id` |
| Создать новую корректировку ставок | `POST /yandex/campaigns/{campaign_id}/bid-modifiers/create` | `dry_run=true` для preview + readback контракт; apply только с `live_write` + `approved=true` + `idempotency_key` + `dry_run=false`; documented families покрывают устройства включая Smart TV, пол/возраст, аудитории, регионы, видео/формат, платежеспособность и group-level; `WEATHER_ADJUSTMENT` create отключён после live `error_code=8000` unknown `WeatherAdjustment` |
| Создать группу в существующей кампании | `POST /yandex/campaigns/{campaign_id}/ad-groups` | создаёт только группу: name/region_ids/optional negative_keywords; затем отдельные шаги для `ads`, ключей и модерации |
| Посмотреть минус-слова по группам | `GET /yandex/campaigns/{campaign_id}/ad-groups/negative-keywords` | текущие `negative_keywords` и `has_negative_keywords` по `ad_group_id` |
| Обновить минус-слова группы | `POST /yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords` | `operation=add|replace`, `approved=true`, `idempotency_key`, `dry_run`; `dry_run=false` только в `live_write`; preview/readback через ответ endpoint |
| Сводка по рекламе | `GET /yandex/reports/summary` | показы, клики, расходы, CTR/CPC; **live** в `sandbox`/`live_readonly`/`live_write` (источник `CAMPAIGN_PERFORMANCE_REPORT`); mock — только при `DIRECTPILOT_MODE=mock` |
| Поисковые запросы | `GET /yandex/reports/search-queries` | реальные поисковые запросы с разрезом на `campaign_id` / `campaign_name` / `ad_group_id`; `cost` в ₽, `impressions`, `clicks`, `ctr` — для fast breakdown; **live** в `sandbox`/`live_readonly`/`live_write` (источник `SEARCH_QUERY_PERFORMANCE_REPORT`); mock — только при `DIRECTPILOT_MODE=mock`; пустой live-отчёт = `items=[]` с `source="yandex"` (не mock-fallback) | Data shape: `query`, `campaign_id`, `campaign_name` (nullable), `ad_group_id`, `impressions`, `clicks`, `ctr`, `cost` |

| Опубликовать черновик в live-direct | `POST /yandex/campaigns/live-create` | Используйте `approved=true`, `idempotency_key`, `dry_run`; проверяйте `stages_executed`, `not_implemented`, `ad_group_ids`/`ad_ids`/`keyword_ids` |
| Добавить объявления в существующую группу | `POST /yandex/ad-groups/{ad_group_id}/ads` | `dry_run=true` для preview; в ответе смотрите `warnings` — наследование BusinessId/SitelinkSetId; ключи/минуса отдельно; также проверяйте `provider_warnings` для нефатальных отклонений Яндекса (напр. `code: 10165` — параметр проигнорирован) — `details` покажет какой именно параметр не был применён; `dry_run=false` только в `live_write` с `approved=true` + `idempotency_key` |
| Отправить объявления на модерацию | `POST /yandex/ads/moderate` | `dry_run=true` для preview; `dry_run=false` + `live_write` для живой отправки; для новой DRAFT-кампании используйте **этот** endpoint, **не** `campaigns.resume`; `campaigns.resume` — только для already-created stopped/suspended кампаний |
| Проверить аудит после публикации | `GET /audit-log` | `live_create_campaign_*`, `live_create_campaign_failed`; для DRAFT-запуска ожидайте отдельный факт отправки ads на модерацию |
| Отправить DRAFT в модерацию | Direct `ads.moderate` по `ad_ids` | для новой кампании после live-create: **не** `campaigns.resume`; ожидаемый readback: campaign `Status=MODERATION`, ads `Status=MODERATION` |
| Возобновить остановленную кампанию | `POST /yandex/campaigns/{campaign_id}/resume` | только для уже созданных stopped/suspended кампаний; не для DRAFT-to-moderation |
| Обновить почасовое расписание показов | `POST /yandex/campaigns/{campaign_id}/time-targeting` | v5 `campaigns.update TimeTargeting`; endpoint читает текущий `DailyBudget` (нормализация `SpendMode`→`Mode`) и, если `DailyBudget: null` у smart-стратегии (`TEXT_CAMPAIGN`), дополнительно читает `TextCampaign.BiddingStrategy` и включает её в payload; отсутствие необходимых полей в таком кейсе приводит к fail-closed с 502 до `campaigns.update`; `dry_run=true` доступен в любом режиме; `dry_run=false` + apply только в `live_write`; `readback` после apply — v5 `campaigns.get TimeTargeting`; payload preview в ответе |
| Посмотреть текущее расписание показов | `GET /yandex/campaigns/{campaign_id}/time-targeting` | read-only; возвращает текущий `TimeTargeting` блок (v5 `campaigns.get`) + нормализованное 7×24 расписание; без write-гейтов; доступен во всех режимах (`mock`/`sandbox`/`live_readonly`/`live_write`) |
| Посмотреть стратегию кампании | `GET /yandex/campaigns/{campaign_id}/strategy` | тип, статус, бюджет, цели, BiddingStrategy; read-only |
| Обновить стратегию кампании | `POST /yandex/campaigns/{campaign_id}/strategy` | `dry_run=true` для preview (рубли); для live-apply нужны `approved=true`, `idempotency_key`; выбрать один `goal_id` ИЛИ `goal_ids` (равновесные цели) ИЛИ `priority_goals` (явные ценности) из `GET /metrika/counters/{counter_id}/goals` (counter_id берется из `GET /yandex/campaigns/{campaign_id}/strategy -> counter_ids`). Одна цель заменяет текущую; `goal_ids`/`priority_goals` используют Direct API `PriorityGoals` + `GoalId=13` |
| Посмотреть автотаргетинг | `GET /yandex/campaigns/{campaign_id}/autotargeting` | категории и бренд-опции автотаргетинга по группам; read-only |
| Настроить автотаргетинг | `POST /yandex/campaigns/{campaign_id}/autotargeting` | `dry_run=true` для preview; дефолтный пресет `exact_narrow` (Exact+Narrow only); `dry_run=false` только в `live_write` с `approved=true` + `idempotency_key`; не включать все категории по умолчанию — спрашивать пользователя |
| Аудит UTM-разметки | `GET /yandex/campaigns/{campaign_id}/utm-audit` | статус UTM по каждому объявлению и быстрой ссылке; complete/partial/missing/mismatch; **read-only**, без write-гейтов |
| Спланировать UTM (preview) | `POST /yandex/campaigns/{campaign_id}/utm-plan` | old_url → new_url для каждого объявления; **всегда dry_run**, никогда не пишет в Яндекс; показывает payload для будущего apply |
| Применить UTM | `POST /yandex/campaigns/{campaign_id}/utm-apply` | `dry_run=true` для preview; `dry_run=false` требует `DIRECTPILOT_MODE=live_write` + `approved=true` + `idempotency_key`; при `include_sitelinks=true` быстрые ссылки обновляются через `sitelinks.update` и возвращают `sitelink_readback` |
| Баланс общего счета | `GET /yandex/account/balance` | безопасная финансовая сводка |
| Счетчики Метрики | `GET /metrika/counters` | доступные сайты/счетчики |
| Цели Метрики | `GET /metrika/counters/{counter_id}/goals` | список целей |
| Сводка Метрики | `GET /metrika/counters/{counter_id}/summary` | visits/users/pageviews/goals |
| Источники трафика | `GET /metrika/counters/{counter_id}/traffic-sources` | source mix |
| Расширить семантику | `GET /wordstat/top?phrase=...&regions=...&limit=...` | похожие запросы и спрос |
| Динамика спроса | `GET /wordstat/dynamics?phrase=...&regions=...&date_from=...&date_to=...&period=...` | сезонность/тренд |
| Региональный спрос | `GET /wordstat/regions?phrase=...` | где спрос выше |
| Найти id региона | `GET /wordstat/regions-tree` | region id/name |
| Аудит внешнего вида объявлений | `GET /yandex/campaigns/{campaign_id}/ad-assets` | заголовки, тексты, быстрые ссылки, визитки, организации, уточнения — всё для оценки внешнего вида кампании |
| Быстрые ссылки (низкоуровневый helper) | `GET /yandex/sitelinks` | только чтение наборов быстрых ссылок — для аудита внешнего вида используйте `ad-assets` |
| Визитки/креативы/организации | `GET /yandex/vcards`, `/yandex/ad-images`, `/yandex/creatives`, `/yandex/businesses` | аудит контактной привязки и креативов |
| Финансы кампаний | `GET /yandex/campaigns/finance` | бюджет, расход/остатки, дневной бюджет |
| Посмотреть настройки автотаргетинга | `GET /yandex/campaigns/{campaign_id}/autotargeting` | категории и brand-опции автотаргетинга для каждой группы; read-only |
| Обновить настройки автотаргетинга | `POST /yandex/campaigns/{campaign_id}/autotargeting` | `dry_run=true` для preview; apply — `live_write` + `approved` + `idempotency_key`; default preset `exact_narrow` |
Важно по `GET /yandex/reports/summary`:

- В `sandbox`/`live_readonly`/`live_write` endpoint возвращает `source="yandex"` при рабочей интеграции.
- `source="mock"` ожидается только при `DIRECTPILOT_MODE=mock`.
- В live-режимах HTTP **409** означает, что Yandex Direct client/токен недоступен; это не скрытая подмена mock-данными.
Важно по `GET /yandex/reports/search-queries`:

- В `sandbox` / `live_readonly` / `live_write` с настроенной интеграцией endpoint
  возвращает `source="yandex"` и реальный результат `SEARCH_QUERY_PERFORMANCE_REPORT` (`Query / CampaignId / CampaignName / AdGroupId /
  Impressions / Clicks / Ctr / Cost`). В ответе API-контрактные поля:
  `query`, `campaign_id`, `campaign_name`, `ad_group_id`, `impressions`, `clicks`, `ctr`, `cost` (где `cost` может быть `null`/`0`, `campaign_name` — nullable).
  Если `CampaignName` отсутствует, endpoint дополняет имя через
  `campaigns.get` по `CampaignId`.
  Источник: `SEARCH_QUERY_PERFORMANCE_REPORT`.
- `source="mock"` ожидается только при `DIRECTPILOT_MODE=mock`; если в
  live-режимах вы видите старые mock-фразы (`сантехник на дом казань`,
  `вызов электрика недорого`, `ремонт квартир под ключ`) — это баг
  конфигурации, а не ожидаемое поведение.
- Пустой live-отчёт (нет строк за период) — **валидный** ответ:
  `items=[]`, `source="yandex"`, `read_only=true`. Не 502, не mock-fallback.
- Фильтр `campaign_id` уходит в Reports API как
  `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN",
  Values: ["..."]}]`, **не** `SelectionCriteria.CampaignIds` (эта форма
  возвращает HTTP 400 на reports endpoint).

### Как анализировать search-queries по разрезам (campaign / ad_group)

Шаблонный маркетинговый workflow:

1. Получите общий срез:

```http
GET /yandex/reports/search-queries?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
```

2. Отсортируйте/агрегируйте по `campaign_id`, `campaign_name`:

- `cost` и `impressions` по кампании.
- Выявите кампании с дисбалансом `cost` vs `clicks` или нулевой конверсией в клики.

3. Для каждой проблемной кампании сделайте drill-down:

```http
GET /yandex/reports/search-queries?campaign_id=<campaign_id>&date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
```

4. Смотрите `ad_group_id` в ответе:

- high-`cost` / high-`impressions` + `clicks==0` → кандидаты для минус-слов;
- низкий `ctr` при заметном трафике → пересмотреть объявления/соответствие оффера;
- низкая конкуренция (малый impressions, нулевой cost) обычно отбрасывать из срочных правок, пока нет статистики.

5. Подготовьте 1:1 рекомендации: список query→action (keep / add as key / minus / needs data).

Не интерпретируйте `items=[]` как ошибку интеграции — это валидный «пустой» live период.



## Что использовать дополнительно (advanced)

Для задач с углубленной диагностикой используй только DirectPilot endpoints ниже:

- `/yandex/campaigns/{campaign_id}/bids`
- `/yandex/campaigns/{campaign_id}/bid-modifiers`
- `/yandex/campaigns/{campaign_id}/negative-keywords`
- `/yandex/campaigns/{campaign_id}/ad-groups/negative-keywords`
- `/yandex/campaigns/{campaign_id}/auction-forecast`
- `/yandex/campaigns/{campaign_id}/ad-groups/{ad_group_id}/negative-keywords`
- `/yandex/campaigns/{campaign_id}/ad-groups`
- `/yandex/changes`, `/yandex/changes/check`
- `/yandex/dictionaries`
- `/yandex/retargeting-lists`, `/yandex/campaigns/{campaign_id}/audience-targets`
- `/yandex/keywords-research/has-search-volume`, `/yandex/keywords-research/deduplicate`
- `/yandex/reports/live/{report_type}`, `/yandex/reports/search-queries-live`
- `/yandex/campaigns/{campaign_id}/ad-assets` — аудит внешнего вида объявлений (заголовки, тексты, быстрые ссылки, визитки, организации)
- `/yandex/account/balance`, `/yandex/campaigns/finance`
- `/metrika/counters`, `/metrika/counters/{counter_id}/goals`, `/metrika/counters/{counter_id}/summary`, `/metrika/counters/{counter_id}/traffic-sources`
- `/wordstat/top`, `/wordstat/dynamics`, `/wordstat/regions`, `/wordstat/regions-tree`

### Изменение ставок на живых ключах

`POST /yandex/campaigns/{campaign_id}/bids` — безопасное изменение SearchBid/ContextBid
для живых ключей. Ставки в рублях, конвертация в Direct micros автоматическая.

**Процесс для маркетолога:**
1. Сначала `dry_run=true` — получить `payload_preview` (точный v5 payload, который *был бы* отправлен).
2. Показать diff пользователю.
3. Только после явного подтверждения — apply с `dry_run=false`, `approved=true`, `DIRECTPILOT_MODE=live_write`.

**Когда НЕ менять per-keyword ставки:**
- Кампания на автостратегии `WB_MAXIMUM_CONVERSION_RATE` — Direct может проигнорировать
  ставки с warning 10160. Вместо этого управляйте расходами через `WeeklySpendLimit`/`BidCeiling`
  через `/yandex/campaigns/{campaign_id}/strategy`.
- РСЯ выключена (`SERVING_OFF`) — не задавайте `context_bid_rub`.

**После live apply:** проверяйте `set_results` — per-item исход по каждому keyword_id.
Если `partial_failure=true` или `has_errors=true` у отдельных item, ставки не применены.
Item-ошибки редиректятся (только `code`/`message`/`details`), сырой v5 payload не показывается.
Предупреждения (код 10160 и др.) дублируются в `provider_warnings` и `set_results[].warnings`.


### Auction forecast read (per-keyword)

Use `GET /yandex/campaigns/{campaign_id}/auction-forecast` for search-auction competitiveness diagnostics:

- read-only, zero mutation (`read_only=true`, `source="yandex"`)
- query params: repeated `keyword_ids`, `limit` (1..1000, default 200), `page_token` (decimal non-negative string)
- paginates by `next_page_token` string; pass it as `page_token` on next call
- safe batch shape: manual keyword ids are fetched from `keywords.get`, then `keywordbids.get` for auction rows by batches of at most **200** ids

`forecast_status` output values:

- `AVAILABLE` — valid auction data for the keyword (`auction_bids` non-empty)
- `NOT_APPLICABLE` — autotargeting row (`---autotargeting`) with reason `AUTOTARGETING`
- `UNAVAILABLE` — no data currently available / not serving / search-off
- `ERROR` — malformed data or upstream row mismatch

`forecast_reason` is normalized and safe (examples: `RARELY_SERVED`, `KEYWORD_NOT_SERVING`, `SEARCH_SERVING_OFF`, `NO_AUCTION_DATA`, `UPSTREAM_BATCH_ERROR`, `AUCTION_ROW_MISSING`, `INVALID_AUCTION_DATA`).

Field interpretation:

- `current_search_bid_rub/micros` — current keyword search bid from `keywords.get` (`Bid`) for the row.
- `auction_bids[].bid` / `auction_bids[].price` — from `keywordbids.get` `AuctionBidItems[].Bid/.Price` at each `traffic_volume` level.
- `auction_bids[].traffic_volume` — **integer only**. Do not treat it as percent and do not round/fabricate it.
- Values like `83,86`, `130,75` are RUB examples from `Bid`/`Price` micros (`83_860_000` etc.) when returned in rubles, not `TrafficVolume`.


### Reading and calculating keyword bids

Use `GET /yandex/campaigns/{campaign_id}/keyword-bids` as the canonical
read-only bid endpoint. It is backed by Yandex Direct v5 `keywordbids.get` and
returns typed rows with `row_kind`, Search/Network bid values in RUB and micros,
auction/coverage data only when the current strategy permits it, and
`limited_by`/`next_offset` pagination. The legacy `GET .../bids` route is
deprecated raw `bids.get`; keep it only for compatibility diagnostics. Existing
`POST .../bids` is still manual `keywordbids.set` for fixed
SearchBid/ContextBid changes.

Use `POST /yandex/campaigns/{campaign_id}/keyword-bids/set-auto` when the goal
is to ask Direct to calculate bids from a rule. This is **bid calculation**, not
autotargeting setup, strategy conversion, payment-model change, or automatic
Network/Search enablement. It never switches strategy to make a request pass;
read `/strategy` first if compatibility is unclear.

Safe marketer workflow:

1. Read current strategy: `GET /yandex/campaigns/{campaign_id}/strategy`.
2. Read current bid rows: `GET /yandex/campaigns/{campaign_id}/keyword-bids`.
3. Build a dry-run setAuto request with one homogeneous scope: `campaign`,
   `ad_group` (unique `ad_group_ids`, up to 1,000), or `keyword` (unique
   `keyword_ids`, up to 10,000; excludes `---autotargeting` rows).
4. Choose one rule only:
   - `search_by_traffic_volume`: `target_traffic_volume=5..100`, optional
     `increase_percent=0..1000`, positive `bid_ceiling_rub`; compatible only
     with Search `HIGHEST_POSITION`.
   - `network_by_coverage`: `target_coverage=0..100`, optional
     `increase_percent=0..1000`, positive `bid_ceiling_rub`; compatible only
     with Network `MAXIMUM_COVERAGE` or `MANUAL_CPM`.
5. Show the dry-run `payload_preview` and impact to the user/operator. `BidCeiling`
   is previewed in Direct micros (`RUB * 1,000,000`).
6. Apply only after explicit human approval with `DIRECTPILOT_MODE=live_write`,
   `approved=true`, valid `idempotency_key`, and `dry_run=false`.
7. Inspect `set_auto_results`, `partial_failure`, and the post-apply
   `keywordbids.get` readback. If readback failed or was truncated, treat the
   response as not business-verified.

Fail-closed safety notes:

- Schema validation can return HTTP 422 before business logic.
- Incompatible strategy, mixed selectors, ownership mismatch, keyword-scope
  autotargeting, or missing targets returns a blocked non-mutating response.
- Pre-write `campaign` and `ad_group` scopes remain fail-closed on any
  `LimitedBy`/truncation. A `keyword` scope is verifiable only when every
  requested `KeywordId` appears exactly once and each row's `CampaignId` matches
  the campaign in the URL; when those checks pass, `LimitedBy` alone does not
  block. Missing IDs return `setAuto readback does not contain requested keyword
  IDs: [...]`; duplicate, mismatched, or otherwise unverifiable IDs return
  `setAuto ownership could not be verified for keyword IDs: [...]`.
- Post-write keyword readback enforces the same complete, exactly-once ownership
  check even without `LimitedBy`. A failed or unverifiable keyword readback, or
  a truncated broad (`campaign`/`ad_group`) readback, returns `applied=false`,
  `partial_failure=true`, and `readback_failed=true` with a concrete sanitized
  verification error instead of pretending success.
- Provider errors and warnings are sanitized; do not expect raw Yandex envelopes
  or token-bearing diagnostics in responses.

### Корректировки ставок по возрасту/демографии

- `POST /yandex/campaigns/{campaign_id}/bid-modifiers` — безопасный dry-run/apply wrapper над Direct v5 `bidmodifiers.set` для изменения существующих корректировок.
- `POST /yandex/campaigns/{campaign_id}/bid-modifiers/create` — wrapper над `bidmodifiers.add` для создания новых корректировок.

Для сценария «возраст 0–17 = -100%» сначала делайте `dry_run=true`:

```json
{
  "approved": true,
  "idempotency_key": "bidmod-demo-001",
  "dry_run": true,
  "adjustments": [{"age_range": "AGE_0_17", "adjustment_percent": -100}]
}
```

Пакет для реального update формируется после `GET /yandex/campaigns/{campaign_id}/bid-modifiers`, где берется `modifier_id` существующего сегмента:

```json
{
  "approved": true,
  "idempotency_key": "bidmod-apply-001",
  "dry_run": false,
  "adjustments": [{"modifier_id": 987654, "age_range": "AGE_0_17", "adjustment_percent": -100}]
}
```

Для создания нового корректировочного элемента используйте `POST /.../bid-modifiers/create` (`campaign_id` для campaign-level families, `ad_group_id` для `AD_GROUP_ADJUSTMENT`):

```json
{
  "approved": true,
  "idempotency_key": "bidmod-create-001",
  "dry_run": true,
  "items": [{"type": "DEMOGRAPHICS_ADJUSTMENT", "age": "AGE_0_17", "campaign_id": 1234567, "adjustment_percent": -100}]
}
```

Важно:

- `bidmodifiers.set` меняет только **существующую** корректировку по `Id` + `BidModifier`; создание нового модификатора через `.../bid-modifiers` не поддерживается.
- `adjustment_percent=-100` конвертируется в Direct `BidModifier=0`;
- `bidmodifiers.add` create использует documented v5 shapes: plural arrays для `DemographicsAdjustments`, `RetargetingAdjustments`, `RegionalAdjustments`, `SerpLayoutAdjustments`, `IncomeGradeAdjustments`; official enum values: `GENDER_MALE/GENDER_FEMALE`, `AGE_*`, `IOS/ANDROID`, `ALONE/SUGGEST`, `VERY_HIGH/HIGH/ABOVE_AVERAGE`;
- `WEATHER_ADJUSTMENT` через create не поддерживается: live Direct вернул `error_code=8000` unknown parameter `WeatherAdjustment`; public `bidmodifiers.add` не содержит weather block. Не искать guessed shape в live; update existing weather по-прежнему делается только через `modifier_id` + `BidModifier`, если row прочитана через `bidmodifiers.get`; `humidity`/`wind` и «Уровень трат в категории» не реализованы;
- `live_apply` доступен только после явного согласования с пользователем: `DIRECTPILOT_MODE=live_write`, `approved=true`, `idempotency_key`, `dry_run=false`;
- после apply endpoint делает readback через `bidmodifiers.get`.

Маркетолог подготавливает dry-run/readback + diff и показывает пользователю; apply делает оператор/оркестратор только после подтверждения.

Если provider/непредвиденная ошибка случается на этом endpoint: возвращается `HTTP 502` (редуцированные diagnostics, без токенов/секретов), в аудит пишется `yandex_bid_modifiers_failed`.

## Scope rule: конкретная кампания vs весь аккаунт

Перед любым выводом сначала зафиксируйте scope запроса пользователя.

- Если пользователь спрашивает про **конкретную кампанию**, выводы должны опираться на campaign-scoped endpoints (`/yandex/campaigns/{campaign_id}/...`) или на account-wide данные, явно отфильтрованные через `CampaignId`/`AdGroupId`/`AdId`/связанный id из campaign readback.
- Если пользователь спрашивает про **весь аккаунт**, account-wide endpoints (`/yandex/campaigns`, `/yandex/sitelinks`, `/yandex/businesses`, `/yandex/vcards`, finance/account reports) подходят для account-level выводов.
- Account-wide endpoint сам по себе **не доказывает**, что найденная сущность привязана к выбранной кампании. Его можно использовать только как справочник после того, как campaign-scoped readback дал конкретный id связи.

Пример для быстрых ссылок:

- для конкретной кампании используйте `GET /yandex/campaigns/{campaign_id}/ad-assets` и смотрите `SitelinkSetId` у объявления + `sitelinks_sets` в ответе;
- `GET /yandex/sitelinks` показывает все наборы аккаунта и не является доказательством привязки к этой кампании.

То же правило применяется ко всему: объявлениям, ключам, минус-словам, бюджетам, визиткам, организациям, отчетам, быстрым ссылкам и другим ассетам. Если вопрос про кампанию — проверяйте именно кампанию; если вопрос общий — можно анализировать весь аккаунт.

## Что маркетолог может делать сам

**Важно:** `/demo/*` и любые mock/sandbox-only маршруты не используются в рабочем маркетинг-процессе.

- Формировать выводы: что не так с семантикой, объявлениями, источниками, бюджетом, целями, спросом.
- Давать гипотезы: новые группы, ключи, минус-слова, тексты, офферы, посадочные страницы.
- Готовить безопасный план изменений для пользователя/оркестратора.

## Что маркетолог не должен делать сам

- Не ходить напрямую в `api.direct.yandex.com`, `api-metrika.yandex.net`, AI Studio/Search API, если та же задача покрыта DirectPilot.
- Не печатать токены, `.env`, OAuth headers, cookies.
- Не выполнять live-write без явного запроса и подтверждения пользователя.
- Не менять код DirectPilot — это задача `coder`.
- Не ревьюить код DirectPilot — это задача `reviewer`.
- Не запускать полноценную техническую проверку сборки/тестов — это задача `verifier`.
- Не считать, что публикация в live-create автоматически запускает кампанию. Для новой `DRAFT`-кампании следующий шаг — `ads.moderate` по созданным `ad_ids`, не `campaigns.resume`. `resume` нужен только для already-created кампаний, которые были остановлены/приостановлены.
- Не применять `POST /yandex/campaigns/{campaign_id}/time-targeting` с `dry_run=false` без явного `approved=true` + `idempotency_key` и подтверждения пользователя. В `live_readonly` реальный apply заблокирован с HTTP 409 до любого сетевого вызова; реально применить можно только в `live_write`. Используйте `dry_run=true` для проверки payload preview перед apply.

  Для smart-кампаний `TEXT_CAMPAIGN` эндпойнт дополнительно читает `TextCampaign.BiddingStrategy` и, если у кампании есть дневной бюджет, применяет `SpendMode→Mode` нормализацию: если `DailyBudget` есть, включается `DailyBudget` с `Mode`; если `DailyBudget: null`, включается сохранённая стратегия (`Search`/`Network`). Если прочитать обязательные поля не удалось (нет бюджета при `live`-режиме или отсутствует `BiddingStrategy` для smart-strategy), apply отклоняется с 502 (fail-closed). `BudgetType` из read-side **сохраняется** в write-side payload — удаление `BudgetType` вызывает error_code=8000.

- Для `POST /yandex/campaigns/{campaign_id}/strategy`: переход с multi-goal на одиночный `goal_id` обязан явно очистить старый набор целей через `TextCampaign.PriorityGoals=null`; не использовать `PriorityGoals.Items=[]` и не полагаться на omission. В non-empty `PriorityGoals.Items` каждый item должен иметь `Operation=SET`. `DailyBudget: null`/missing безопасен только если readback подтверждает `Search.WbMaximumConversionRate.BudgetType=WEEKLY_BUDGET`, иначе endpoint fail-closed до `campaigns.update`.

- Не включать все категории автотаргетинга по умолчанию для локальных сервисных поисковых кампаний. Default preset `exact_narrow` (Exact=YES, Narrow=YES, Alternative=NO, Accessory=NO, Broader=NO) — безопасный минимум. Broader включается только по явному запросу пользователя с осознанием trade-off по охвату. При создании/настройке кампании или группы явно спрашивайте пользователя о желаемых настройках автотаргетинга, не принимайте молча все категории. Brand-опции по умолчанию: WithoutBrands=YES, WithAdvertiserBrand=YES, WithCompetitorsBrand=NO.

## Правила для добавления объявлений в существующую кампанию/группу

При подготовке новых объявлений для конкретной кампании/группы через `POST /yandex/ad-groups/{ad_group_id}/ads`, маркетолог должен явно решить (или спросить пользователя):

### 1. BusinessId / организация

- **По умолчанию:** переиспользовать тот же `business_id`, который уже используется в существующих объявлениях кампании/группы (проверить через `GET /yandex/campaigns/{campaign_id}/ads` или `ad-assets`).
- Если пользователь явно хочет без организации — передать без `business_id`.
- Endpoint возвращает `warning` с кодом `inherit_business_id`, если `business_id` не указан.

### 2. SitelinkSetId / быстрые ссылки

- **По умолчанию:** переиспользовать тот же `sitelink_set_id`, если существующий набор релевантен (проверить через campaign-specific readback).
- Если набор нерелевантен новым объявлениям — опустить или указать другой.

### 3. Ключевые слова и минус-слова

- **Ключи и минуса управляются на уровне кампании/группы, НЕ на уровне объявления.**
- Добавление новых объявлений НЕ меняет ключевые слова и минус-слова.
- Если новый угол объявления требует дополнительных ключей или минус-слов — создайте отдельную задачу через `POST /campaigns/{campaign_id}/semantic-changes`, не смешивайте с созданием объявлений.
- Endpoint возвращает `warning` с кодом `keywords_not_per_ad`.

### 4. Слова-ловушки в текстах объявлений

- Использование технических/профессиональных терминов в тексте/заголовке объявления — это креативный выбор, он не заменяет управляемые списки ключевых слов.
- **Не путайте:** текст объявления и ключевая фраза — разные уровни. Ключи управляются отдельно от объявления.
- При использовании нишево-специфичных терминов отмечайте потенциальный DIY-трафик и закрывайте его отдельными negative/ключевыми правками, если это требуется по фактическому данным.

### 5. Отправка на модерацию

- После `live-create` или `POST /yandex/ad-groups/{ad_group_id}/ads` с `dry_run=false` НЕ используйте `campaigns.resume` для новых DRAFT-кампаний.
- Используйте `POST /yandex/ads/moderate` с `ad_ids` созданных объявлений.

## Стандартный workflow анализа рекламы

1. `GET /health` и `GET /integrations/yandex/direct/status`.
2. `GET /yandex/campaigns` — выбрать кампанию.
3. Для кампании: ad-groups, ads, keywords, **ad-assets (аудит внешнего вида)**.
4. Отчеты: summary + search-queries.
5. Финансы: account balance + campaigns finance.
6. Метрика: counters → goals → summary → traffic-sources.
7. Wordstat: top/dynamics/regions для проверки спроса и расширения семантики.
8. Для любых задач по ключам, минус-словам или поисковым запросам запускать semantic workflow:
   - классифицировать интент: keep / add as key / minus / decision needed;
   - использовать Wordstat (`/wordstat/top`, `/wordstat/dynamics`, `/wordstat/regions`) для расширения и проверки спроса;
   - проверять смежные ложные смыслы сферы (например смежные сервисы/использования, которые часто перетягивают нецелевой трафик).
   - группировать минуса по причинам: DIY, работа/обучение, покупка/запчасти, другая техника/услуга, конкуренты/бренды, география;
   - конкурентов/бренды (`айсберг`, `iceberg` и т.п.) добавлять точечно или после подтверждения поисковыми запросами; не минусовать широкие коммерческие слова вроде `компания`/`сервис`, если они могут быть полезным интентом;
   - перед live-write читать фактический список через `GET /yandex/campaigns/{campaign_id}/ad-groups/negative-keywords`; при работе со shared-контрольной моделью дополнительно использовать `GET /yandex/campaigns/{campaign_id}/negative-keywords`.
9. Сформировать вывод:
   - что работает;
   - где потери;
   - какие ключи/минус-слова/объявления/офферы проверить;
   - какие данные нужны дополнительно;
   - 3 варианта действий: Max Performance / Cost-Efficient / Ultra-Fast-Low-Cost.

## Контакты, телефон, график, организация

Для телефона/графика/визитки маркетолог не должен пытаться напрямую управлять Yandex API.

Через DirectPilot:

- `GET /yandex/vcards` — посмотреть визитки;
- `GET /yandex/businesses` — посмотреть организации/BusinessId;
- `GET /yandex/campaigns/{campaign_id}/ads` — проверить привязки `BusinessId`/`PreferVCardOverBusiness`/`VCardId`, если они есть в ответе;
- `GET /yandex/campaigns/{campaign_id}/ad-assets` — агрегированный аудит внешнего вида: все объявления, разрешённые быстрые ссылки, бизнесы/организации и визитки в одном ответе.
- Для сценария `error_code=3500` после `POST /yandex/vcards` используйте специальный runbook: `docs/YANDEX_BUSINESS_CONTACTS.md`.

Если DirectPilot показывает, что создание визиток не поддерживается для кампании, маршрут — через организацию/Яндекс Бизнес, а не повторные попытки `vCards.add`.

## Проверка качества вывода маркетолога

Перед ответом пользователь должен получить:

- конкретные endpoints DirectPilot, на которых основаны выводы;
- короткую интерпретацию без сырых API-дампов;
- список действий и ожидаемый эффект;
- явное разделение фактов из DirectPilot и гипотез;

## UTM workflow (Яндекс Директ)

### Для чего нужен UTM

UTM-метки — это параметры в URL, которые позволяют Яндекс Метрике и другим системам аналитики различать источники трафика. Без UTM вы не сможете точно определить, какие кампании/объявления/ключевые слова приносят заявки и продажи.

### Конвенция DirectPilot по умолчанию

| Параметр | Значение | Описание |
|---|---|---|
| `utm_source` | `yandex` | Источник — Яндекс Директ (фиксировано) |
| `utm_medium` | `cpc` | Тип трафика — оплата за клик (фиксировано) |
| `utm_campaign` | `<slug>` | Уникальный slug кампании (генерируется из названия + id или задаётся вручную) |
| `utm_content` | `{ad_id}` | ID объявления (автоподстановка из данных кампании) |
| `utm_term` | `{keyword}` / текущая ограничение | Планируется keyword-level mapping; до его реализации поле остаётся пустым |

### Пошаговый workflow

**Для существующих кампаний (existing campaigns):**

1. **Аудит:** `GET /yandex/campaigns/{campaign_id}/utm-audit`
   - Показывает текущий статус UTM по каждому объявлению и каждой быстрой ссылке.
   - Статусы: `complete` (все 5 параметров), `partial` (часть есть), `missing` (нет ни одного), `mismatch` (есть, но значения не совпадают с ожидаемыми).

2. **План/preview:** `POST /yandex/campaigns/{campaign_id}/utm-plan`
   - Всегда dry_run — **никогда не пишет** в Яндекс.
   - Показывает конкретный diff/impact: `old_url -> new_url` для каждого объявления (и `sitelink_items` для быстрых ссылок).
   - Если `campaign_slug` не передан — генерируется безопасно из названия кампании + id.
   - `overwrite=false` (по умолчанию) — сохраняет существующие UTM и query/fragment.
   - `overwrite=true` — перезаписывает все UTM-параметры.
   - `custom_params` — дополнительные параметры (напр. `utm_custom=extra`).
   - Быстрые ссылки — preview в `sitelink_items`; apply доступен через gated `utm-apply` при `include_sitelinks=true`.

3. **Подтверждение пользователя (HUMAN APPROVAL CONTRACT):**
   - Показать пользователю конкретный diff (old_url → new_url для каждого объявления).
   - Явно спросить про slug, overwrite, sitelinks.
   - **Не применять изменения без явного подтверждения пользователя.**
   - `approved=true` в запросе — это технический флаг, а не самоодобрение агента.

4. **Применение:** `POST /yandex/campaigns/{campaign_id}/utm-apply`
   - **Требования для реальной записи:**
     - `DIRECTPILOT_MODE=live_write`
     - `approved=true`
     - `idempotency_key` (>= 6 символов, уникальный ключ)
     - `dry_run=false`
   - `dry_run=true` — только preview, без записи.
   - После успешного apply возвращает `ad_ids`, `readback` (проверка новых URL объявлений) и `sitelink_readback` для быстрых ссылок.
   - Быстрые ссылки применяются через `sitelinks.update`; `Title`/`Description` и структура набора сохраняются.

5. **Readback:** проверить подтверждённые URL в `readback` и `sitelink_readback` ответа.

**Для новых кампаний (live-create):**

- Передайте `utm_config` в `POST /yandex/campaigns/live-create`, чтобы
  объявления родились сразу с UTM-разметкой.
- `LiveCreateCampaignRequest.utm_config` **переопределяет** `draft.utm_config`:
  если передан — используется он; если не передан — fallback на драфт.
- Это позволяет задать UTM при создании кампании, даже если драфт был создан
  без UTM, или переопределить slug на лету без изменения драфта.
- `enabled=false` в переопределении отключает UTM-разметку, даже если в драфте
  UTM был включён.

### Когда агент/маркетолог должен спросить пользователя

- **Campaign slug:** если неочевидно, какой slug использовать — спросить. По умолчанию генерируется из названия кампании + id.
  **Важно для кириллических названий:** если название кампании состоит только из
  кириллицы (например, «Турбины Ростов»), автоматический генератор slug
  выдаст ``campaign-{id}``, так как транслитерация отбрасывает не-ASCII символы.
  Чтобы в отчётах Метрики было читаемое название (а не ``campaign-12345``),
  **попросите пользователя явно указать ``campaign_slug``** — латинскую
  транслитерацию или смысловой slug (например, ``turbiny-rostov``).
- **Overwrite:** если часть UTM уже стоит — спросить, перезаписывать ли (overwrite=true) или сохранить существующие (overwrite=false).
- **Sitelinks:** если нужны UTM на быстрых ссылках — предупредить, что apply для sitelinks пока не реализован.

### Безопасность

- Все write-операции используют `ads.update` (REPLACE-shaped) — **все обязательные поля** (Title, Text, Href) переотправляются.
- `BusinessId`, `SitelinkSetId`, `VCardId`, `Title2` сохраняются при обновлении.
- Токены и секреты **никогда** не появляются в ответах, логах и preview.
- `live_readonly` блокирует реальные записи **до** любого сетевого вызова (HTTP 409).

## Миграция URL существующих объявлений

Для переезда посадочной страницы используйте специальный preview/apply workflow, а не ручной raw `ads.update`:

- `POST /yandex/campaigns/{campaign_id}/ads/landing-urls` — URL основных `TEXT_AD`;
- `POST /yandex/campaigns/{campaign_id}/sitelinks/migrate-urls` — clone-and-reattach быстрых ссылок;
- `POST /yandex/campaigns/{campaign_id}/landing-url-migrations` — единый non-transactional workflow.

Порядок работы маркетолога:
1. Получить текущие объявления и набор быстрых ссылок; подготовить точные `expected_href`/`expected_items` и целевые URL.
2. Проверить, что целевые HTTPS-host входят в `DIRECTPILOT_URL_MIGRATION_ALLOWED_HOSTS` и URL реально отдают 2xx; fragment должен существовать на странице.
3. Выполнить `dry_run=true` (default) и показать пользователю `changes`, `payload_preview`, `reference_scan` и риски.
4. Получить явное подтверждение именно показанного diff. Маркетолог сам live apply не выполняет.
5. Оператор применяет с `DIRECTPILOT_MODE=live_write`, `approved=true`, `dry_run=false`, новым `idempotency_key`.
6. Проверить `provider_results`, `readback`, `completed` и `partial_failure`.

`expected_href` и `expected_items` защищают от устаревшего preview: несовпадение текущих данных блокирует write. Для быстрых ссылок DirectPilot создаёт clone через `sitelinks.add`, проверяет его и привязывает через `ads.update`; исходный набор не изменяется и не удаляется, `sitelinks.update` в этом workflow не используется. Перед привязкой выполняется fail-closed account-wide reference scan: `campaigns.get` формирует inventory, `ads.get` постранично читает объявления по допустимому `SelectionCriteria.CampaignIds`, а нужный `TextAd.SitelinkSetId` фильтруется локально. Фильтр `SelectionCriteria.SitelinkSetIds` не используется; объявления без быстрых ссылок и с другим набором игнорируются, ссылки из других кампаний не перепривязываются. Некорректный или ограниченный inventory, provider error и повторяющиеся ID блокируют операцию.

Timeout/transport error при проверке целевого URL возвращается как безопасный HTTP 409. В provider-диагностике доступны только разрешённые поля (`provider`, `service`, `method`, `operation`, `http_status`, `error_code`, `error_string`, `error_detail`, если они есть); OAuth, токены, request body и `units` не показываются. Сначала всегда выполняйте `dry_run=true`; live apply остаётся отдельным операторским действием после явного подтверждения пользователя.

Операции не транзакционные. При `partial_failure=true` или несовпадении readback запрещён слепой повтор/rollback: сначала новый read-only preflight. Idempotency URL-миграций хранится process-local и после рестарта не гарантирует дедупликацию.
