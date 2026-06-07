# DirectPilot Beta — простая документация API

DirectPilot Beta — отдельное API-first приложение для безопасной подготовки и проверки рекламных кампаний Яндекс Директ.

## Текущий режим

Сейчас приложение работает с реальными production-данными Директа в режиме:

```text
DIRECTPILOT_MODE=live_readonly
```

- реальные кампании, группы, объявления и ключевые фразы читаются через API Директа;
- реальные настройки в Директе не меняются в `live_readonly`;
- токены и секреты не выводятся в ответах;
- write-like действия требуют `approved=true`, `idempotency_key` и отдельный режим `live_write`.

OpenAPI:

```text
http://127.0.0.1:8000/openapi.json
```

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

Это mock-генератор структуры. Он помогает быстро получить рабочий каркас кампании, но перед live-запуском структуру нужно проверить человеком/агентом.

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

Показывает JSON, который в будущем мог бы быть отправлен в Yandex Direct.

Важно: сейчас это только preview, `dry_run=true`, `requires_approval=true`. Live create/update apply не реализован.

---

## 9. Бюджет и ставки

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

---

## 10. Yandex Direct read-only facade

В режиме `live_readonly` эти методы читают реальные production-данные Яндекс Директа и возвращают `source="yandex"`, `read_only=true`. В режиме `mock` они возвращают локальные демонстрационные данные.

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

## 11. Pause / resume

Это ограниченный live-control блок. В текущем `live_readonly` режиме реальные write-вызовы заблокированы; `dry_run=true` доступен для проверки сценария без изменения Директа.

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

- без `approved=true` вернётся ошибка;
- `idempotency_key` обязателен;
- при `dry_run=true` реальное состояние не меняется;
- audit log фиксирует запрос;
- реальные внешние write calls возможны только в отдельном режиме `live_write` при `dry_run=false`.

---

## 12. Audit log

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

## 13. Рекомендации и apply flow

```http
GET  /recommendations
POST /recommendations/{recommendation_id}/approve
POST /recommendations/{recommendation_id}/reject
POST /actions/{action_id}/apply
```

`apply` требует approve и idempotency key. Для внешних изменений в Директе используется отдельный live-control слой `/yandex/.../pause|resume`; общий recommendations/apply flow остаётся внутренним сценарием приложения.

---

## 14. Техническое примечание про DELETE

Некоторые DELETE endpoints в MVP принимают JSON body, например:

```http
DELETE /campaign-drafts/{draft_id}/keywords
```

Это допустимо для локального FastAPI/TestClient и удобно для agents, но при подключении внешнего API gateway/proxy это нужно пересмотреть: часть HTTP-клиентов и прокси может отбрасывать тело DELETE-запроса. Если появится такой шлюз, безопасная альтернатива — `POST .../remove`.

---

## 15. Утилиты

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

Пока нет live endpoints для:

- создания кампании в Яндекс Директ;
- полного обновления реальной кампании в Яндекс Директ;
- отправки preview payload в Direct;
- автоматического расходования бюджета.

Это сделано намеренно: текущий production-режим — real-data read-only. Live writes — отдельный управляемый этап через `live_write` после проверки лимитов и approval-политики.


### Расширенный read-only/API-first слой Яндекс Директа

DirectPilot Beta теперь содержит обёртки для полного практического read-only и аналитического покрытия Direct API v5. Все методы работают через реальный OAuth-токен из переменной окружения, но значение токена нигде не сохраняется и не выводится.

#### Аналитика reports

```text
GET /yandex/reports/live/CAMPAIGN_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/ADGROUP_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/AD_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/live/CRITERIA_PERFORMANCE_REPORT?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
GET /yandex/reports/search-queries-live?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
```

Назначение: статистика кампаний, групп, объявлений, условий показа и поисковых запросов. Ответ возвращается как read-only provider payload (`source=yandex`, `read_only=true`).

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

#### Аудит таргетингов и семантики

```text
GET /yandex/retargeting-lists                         -> retargetinglists.get
GET /yandex/campaigns/{campaign_id}/audience-targets  -> audiencetargets.get
GET /yandex/keywords-research/has-search-volume?keywords=...
GET /yandex/keywords-research/deduplicate?keywords=...
GET /yandex/keywords-research/wordstat/create?phrases=...&geo_ids=213
GET /yandex/keywords-research/wordstat/{report_id}
DELETE /yandex/keywords-research/wordstat/{report_id}
```

Назначение: аудит аудиторий, проверка спроса, дедупликация фраз и работа с Wordstat-отчётами.

#### Расширения объявлений и ассеты

```text
GET /yandex/sitelinks      -> sitelinks.get
GET /yandex/vcards         -> vcards.get
GET /yandex/ad-images      -> adimages.get
GET /yandex/creatives      -> creatives.get
GET /yandex/feeds          -> feeds.get
GET /yandex/businesses     -> businesses.get
GET /yandex/agency-clients -> agencyclients.get
```

Назначение: быстрые ссылки, визитки, изображения, креативы, фиды, организации и агентские клиенты. Эти endpoints нужны, чтобы программа могла строить полную карту аккаунта Директа, а не только кампании/ключи.
