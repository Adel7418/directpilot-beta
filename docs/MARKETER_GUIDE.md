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
| Посмотреть группы кампании | `GET /yandex/campaigns/{campaign_id}/ad-groups` | структура групп |
| Посмотреть объявления | `GET /yandex/campaigns/{campaign_id}/ads` | тексты, ссылки, статусы, business/vcard fields если есть |
| Посмотреть ключи | `GET /yandex/campaigns/{campaign_id}/keywords` | семантика, минус-гипотезы, дубли |
| Сводка по рекламе | `GET /yandex/reports/summary` | показы, клики, расходы, CTR/CPC; **live** в `sandbox`/`live_readonly`/`live_write` (источник `CAMPAIGN_PERFORMANCE_REPORT`); mock — только при `DIRECTPILOT_MODE=mock` |
| Поисковые запросы | `GET /yandex/reports/search-queries` | реальные запросы, минус-слова, новые ключи; **live** в `sandbox`/`live_readonly`/`live_write` (источник `SEARCH_QUERY_PERFORMANCE_REPORT`); mock — только при `DIRECTPILOT_MODE=mock`; пустой live-отчёт = `items=[]` с `source="yandex"` (не mock-fallback) |
| Опубликовать черновик в live-direct | `POST /yandex/campaigns/live-create` | Используйте `approved=true`, `idempotency_key`, `dry_run`; проверяйте `stages_executed`, `not_implemented`, `ad_group_ids`/`ad_ids`/`keyword_ids` |
| Проверить аудит после публикации | `GET /audit-log` | `live_create_campaign_*`, `live_create_campaign_failed`; для DRAFT-запуска ожидайте отдельный факт отправки ads на модерацию |
| Отправить DRAFT в модерацию | Direct `ads.moderate` по `ad_ids` | для новой кампании после live-create: **не** `campaigns.resume`; ожидаемый readback: campaign `Status=MODERATION`, ads `Status=MODERATION` |
| Возобновить остановленную кампанию | `POST /yandex/campaigns/{campaign_id}/resume` | только для уже созданных stopped/suspended кампаний; не для DRAFT-to-moderation |
| Обновить почасовое расписание показов | `POST /yandex/campaigns/{campaign_id}/time-targeting` | v5 `campaigns.update TimeTargeting`; перед apply читается текущий `DailyBudget` кампании (нормализация `SpendMode`→`Mode`); `DailyBudget` включается в update-payload — иначе Direct error 8000; `dry_run=true` доступен в любом режиме; `dry_run=false` + apply только в `live_write`; `readback` после apply — v5 `campaigns.get TimeTargeting`; payload preview в ответе |
| Баланс общего счета | `GET /yandex/account/balance` | безопасная финансовая сводка |
| Счетчики Метрики | `GET /metrika/counters` | доступные сайты/счетчики |
| Цели Метрики | `GET /metrika/counters/{counter_id}/goals` | список целей |
| Сводка Метрики | `GET /metrika/counters/{counter_id}/summary` | visits/users/pageviews/goals |
| Источники трафика | `GET /metrika/counters/{counter_id}/traffic-sources` | source mix |
| Расширить семантику | `GET /wordstat/top?phrase=...&regions=...&limit=...` | похожие запросы и спрос |
| Динамика спроса | `GET /wordstat/dynamics?phrase=...&regions=...&date_from=...&date_to=...&period=...` | сезонность/тренд |
| Региональный спрос | `GET /wordstat/regions?phrase=...` | где спрос выше |
| Найти id региона | `GET /wordstat/regions-tree` | region id/name |
| Быстрые ссылки/визитки/креативы/организации | `GET /yandex/sitelinks`, `/yandex/vcards`, `/yandex/ad-images`, `/yandex/creatives`, `/yandex/businesses` | аудит ассетов и контактной привязки |
| Финансы кампаний | `GET /yandex/campaigns/finance` | бюджет, расход/остатки, дневной бюджет |

Важно по `GET /yandex/reports/summary`:

- В `sandbox`/`live_readonly`/`live_write` endpoint возвращает `source="yandex"` при рабочей интеграции.
- `source="mock"` ожидается только при `DIRECTPILOT_MODE=mock`.
- В live-режимах HTTP **409** означает, что Yandex Direct client/токен недоступен; это не скрытая подмена mock-данными.

Важно по `GET /yandex/reports/search-queries`:

- В `sandbox`/`live_readonly`/`live_write` с настроенной интеграцией endpoint
  возвращает `source="yandex"` и реальный результат
  `SEARCH_QUERY_PERFORMANCE_REPORT` (`Query / CampaignId / AdGroupId /
  Impressions / Clicks / Ctr / Cost`). Источник: `SEARCH_QUERY_PERFORMANCE_REPORT`.
- `source="mock"` ожидается только при `DIRECTPILOT_MODE=mock`; если в
  live-режиме вы видите старые mock-фразы (`сантехник на дом казань`,
  `вызов электрика недорого`, `ремонт квартир под ключ`) — это баг
  конфигурации, а не ожидаемое поведение.
- Пустой live-отчёт (нет строк за период) — **валидный** ответ:
  `items=[]`, `source="yandex"`, `read_only=true`. Не 502, не mock-fallback.
- В live-режимах HTTP **409** означает, что Yandex Direct client/токен
  недоступен.
- Фильтр `campaign_id` уходит в Reports API как
  `SelectionCriteria.Filter = [{Field: "CampaignId", Operator: "IN",
  Values: ["..."]}]`, **не** `SelectionCriteria.CampaignIds` (эта форма
  возвращает HTTP 400 на reports endpoint).


## Что использовать дополнительно (advanced)

Для задач с углубленной диагностикой используй только DirectPilot endpoints ниже:

- `/yandex/campaigns/{campaign_id}/bids`
- `/yandex/campaigns/{campaign_id}/bid-modifiers`
- `/yandex/campaigns/{campaign_id}/negative-keywords`
- `/yandex/changes`, `/yandex/changes/check`
- `/yandex/dictionaries`
- `/yandex/retargeting-lists`, `/yandex/campaigns/{campaign_id}/audience-targets`
- `/yandex/keywords-research/has-search-volume`, `/yandex/keywords-research/deduplicate`
- `/yandex/reports/live/{report_type}`, `/yandex/reports/search-queries-live`
- `/yandex/account/balance`, `/yandex/campaigns/finance`
- `/metrika/counters`, `/metrika/counters/{counter_id}/goals`, `/metrika/counters/{counter_id}/summary`, `/metrika/counters/{counter_id}/traffic-sources`
- `/wordstat/top`, `/wordstat/dynamics`, `/wordstat/regions`, `/wordstat/regions-tree`

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
- Не применять `POST /yandex/campaigns/{campaign_id}/time-targeting` с `dry_run=false` без явного `approved=true` + `idempotency_key` и подтверждения пользователя. В `live_readonly` реальный apply заблокирован с HTTP 409 до любого сетевого вызова; реально применить можно только в `live_write`. Используйте `dry_run=true` для проверки payload preview перед apply. Эндпойнт автоматически читает текущий `DailyBudget` кампании и включает его с нормализованным `Mode` в payload update — без этого Direct возвращает error 8000. Если `DailyBudget` не удалось прочитать, apply отклоняется с 502 (fail closed).

## Стандартный workflow анализа рекламы

1. `GET /health` и `GET /integrations/yandex/direct/status`.
2. `GET /yandex/campaigns` — выбрать кампанию.
3. Для кампании: ad-groups, ads, keywords.
4. Отчеты: summary + search-queries.
5. Финансы: account balance + campaigns finance.
6. Метрика: counters → goals → summary → traffic-sources.
7. Wordstat: top/dynamics/regions для проверки спроса и расширения семантики.
8. Для любых задач по ключам, минус-словам или поисковым запросам запускать semantic workflow:
   - классифицировать интент: keep / add as key / minus / decision needed;
   - использовать Wordstat (`/wordstat/top`, `/wordstat/dynamics`, `/wordstat/regions`) для расширения и проверки спроса;
   - проверять соседние ложные смыслы сферы: например, для бытовых кондиционеров — авто-кондиционеры (`авто`, `автомобиль`, `автокондиционер`, `кондиционер в машине`);
   - группировать минуса по причинам: DIY, работа/обучение, покупка/запчасти, другая техника/услуга, конкуренты/бренды, география;
   - конкурентов/бренды (`айсберг`, `iceberg` и т.п.) добавлять точечно или после подтверждения поисковыми запросами; не минусовать широкие коммерческие слова вроде `компания`/`сервис`, если они могут быть полезным интентом;
   - перед live-write читать фактический список через `/yandex/campaigns/{campaign_id}/negative-keywords` или read-only `adgroups.get`.
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
- `GET /yandex/campaigns/{campaign_id}/ads` — проверить привязки `BusinessId`/`PreferVCardOverBusiness`/`VCardId`, если они есть в ответе.
- Для сценария `error_code=3500` после `POST /yandex/vcards` используйте специальный runbook: `docs/YANDEX_BUSINESS_CONTACTS.md`.

Если DirectPilot показывает, что создание визиток не поддерживается для кампании, маршрут — через организацию/Яндекс Бизнес, а не повторные попытки `vCards.add`.

## Проверка качества вывода маркетолога

Перед ответом пользователь должен получить:

- конкретные endpoints DirectPilot, на которых основаны выводы;
- короткую интерпретацию без сырых API-дампов;
- список действий и ожидаемый эффект;
- явное разделение фактов из DirectPilot и гипотез;
- отсутствие секретов и raw-токенов.
