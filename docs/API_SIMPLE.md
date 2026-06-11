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
  "reason": "Подготовка новой кампании после ревью"
}
```

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
  возвращает `error_code=8000` / `Отсутствует обязательный параметр Mode`.

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

Для живой кампании Direct v5 используйте `KeywordBids.set`.

Правильная минимальная форма для известных `KeywordId`:

```json
{
  "method": "set",
  "params": {
    "KeywordBids": [
      {
        "KeywordId": 57440007797,
        "SearchBid": 250000000
      }
    ]
  }
}
```

Где `SearchBid` указывается в микроденежных единицах Direct: `250000000` = `250 ₽`.

**Pitfall:** не смешивайте `CampaignId` + `AdGroupId` + `KeywordId` в одном item при массовом обновлении, если обновляете конкретные ключи. На live-проверке Direct вернул `error_code=9300` / `Превышено ограничение на количество объектов в одном запросе` для batch формы с `CampaignId`, `AdGroupId`, `KeywordId`, `SearchBid` на 30 items. Корректный retry по `KeywordId + SearchBid` применился ко всем 30 ключам без per-item ошибок.

Для автотаргетинга добавляйте явный флаг, если задаёте ручную поисковую ставку:

```json
{
  "KeywordId": 205759917809,
  "SearchBid": 250000000,
  "AutotargetingSearchBidIsAuto": "NO"
}
```

Не меняйте `NetworkBid`, если РСЯ должна оставаться выключенной (`Network.BiddingStrategyType=SERVING_OFF`).

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

### Поисковые запросы

```http
GET /yandex/reports/search-queries
```

Ответы содержат признаки:

```json
{
  "source": "yandex",
  "read_only": true
}
```

Последняя проверка реального аккаунта:

```text
GET /yandex/campaigns -> count=1
GET /yandex/campaigns/[REDACTED_CAMPAIGN_ID]/ad-groups -> count=1
GET /yandex/campaigns/[REDACTED_CAMPAIGN_ID]/ads -> count=1
GET /yandex/campaigns/[REDACTED_CAMPAIGN_ID]/keywords -> count=32
```

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

## 13. Audit log

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
GET /yandex/campaigns/{campaign_id}/bids              -> bids.get
GET /yandex/campaigns/{campaign_id}/bid-modifiers     -> bidmodifiers.get
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
GET /yandex/sitelinks      -> sitelinks.get
GET  /yandex/vcards        -> vcards.get
POST /yandex/vcards        -> vcards.add (dry-run по умолчанию; live-write только через approved + idempotency_key + dry_run=false; для реальной записи Direct требует campaign_id)
POST /yandex/ads/business  -> ads.update для привязки опубликованной организации BusinessId к TextAd (dry-run по умолчанию; live-write только через approved + idempotency_key + dry_run=false)
GET  /yandex/ad-images     -> adimages.get
GET /yandex/creatives      -> creatives.get
GET /yandex/feeds          -> feeds.get
GET /yandex/businesses     -> businesses.get
GET /yandex/agency-clients -> agencyclients.get
```

Назначение: быстрые ссылки, визитки, BusinessId-привязка, изображения, креативы, фиды, организации и агентские клиенты. Эти endpoints нужны, чтобы программа могла строить полную карту аккаунта Директа, а не только кампании/ключи.

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
- пример подтверждённого live-кейса: `business_id=11588384335`, `campaign_id=710691939`, ads `17747346245..17747346249` → `applied=True`, `BusinessId=11588384335`, `PreferVCardOverBusiness="NO"`, `VCardId` пустой/`null`.
