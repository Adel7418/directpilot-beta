# Direct Campaign 710382063 Semantics Change Plan

Status: proposed, no live changes applied.
Campaign: `710382063` — dishwasher repair, Kazan.
Source: DirectPilot live-readonly diagnostics + Yandex Direct search query report for 2026-06-02..2026-06-08.

## User decisions

- Keep autotargeting enabled for traffic discovery.
- Monitor autotargeting search queries and filter junk regularly.
- Keep nearby-region queries such as `ремонт посудомоечных машин пестречинский район` at least for statistics unless the geography is explicitly excluded later.
- Keep commercial component/brand service queries such as `замена тэна посудомоечной машины Electrolux`, but run them with limited budget/bid and evaluate by actual leads.
- Exclude DIY/instructional intent, not the component term itself.

## Add negative phrases now

### DIY / instructional intent

- `как поменять тэн`
- `как заменить тэн`
- `как поменять насос`
- `как заменить насос`
- `как поменять помпу`
- `как заменить помпу`
- `как поменять разбрызгиватель`
- `как заменить разбрызгиватель`
- `сделать самому`
- `самому`
- `своими руками`
- `инструкция`
- `схема`
- `руководство`
- `видео`

### Education / non-service intent

- `курсы`
- `обучение`
- `тренинг`
- `научиться`

### Parts-only / purchase intent

- `купить`
- `продажа`
- `продают`
- `ремкомплект`
- `запчасти`
- `детали`

### Clearly unrelated service intent

- `плиточник`
- `духовок`
- `микроволновок`
- `установка фасада`

### Competitor / official-site intent to review before applying

Apply only if the user confirms these are not useful:

- `хотроинтер`
- `официальный сайт`
- `официальный сервис`

## Do not add as negatives by default

- `пестречинский район`
- `балтаси`
- `нижний услон`
- `зеленодольск`
- `тэн`
- `насос`
- `помпа`
- `плата`
- `bosch`
- `electrolux`
- `beko`
- `ariston`
- `gorenje`

## Key phrases to add / strengthen

Use exact or phrase-oriented matching where possible; do not rely only on broad matching.

- `ремонт посудомоечных машин казань`
- `ремонт посудомоечной машины казань`
- `ремонт посудомойки казань`
- `ремонт посудомойки на дому казань`
- `ремонт посудомоечных машин на дому казань`
- `мастер по ремонту посудомоечных машин казань`
- `вызов мастера по ремонту посудомоечной машины`
- `ремонт посудомоечной машины bosch казань`
- `ремонт посудомоечной машины electrolux казань`
- `ремонт посудомоечной машины beko казань`
- `замена тэна посудомоечной машины electrolux`
- `замена тэна посудомоечной машины bosch`
- `замена насоса посудомоечной машины bosch`
- `замена циркуляционного насоса посудомоечной машины`

## Autotargeting rule

- Keep enabled.
- Limit bid/budget if the platform allows separation by group/campaign.
- Review search query report every 2–3 days while traffic is being collected.
- Move good queries into manual keys.
- Add only obvious junk to negative phrases.

## Live-write approval needed

No live Direct changes were applied while creating this plan.

Before live write, confirm:

1. Apply the negative phrases above, excluding the “review before applying” competitor/official phrases unless confirmed.
2. Add/strengthen the listed key phrases.
3. Keep autotargeting enabled.
4. Do not exclude nearby geography yet.
