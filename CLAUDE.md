# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это

Доменомер (<https://domenomer.ru>; с 16.08.2026 канон на бренд-домене, www / старый поддомен domenomer.delosvod.ru / доменомер.рф — 301 на него, все имена в одном `deploy/domenomer.caddy`) — публичный бесплатный инструмент экосистемы Делосвод: массовая проверка Ahrefs Domain Rating по списку доменов. Интерфейс на русском. Зависимостей нет: Python 3 (stdlib) + ванильные HTML/CSS/JS, без сборки. Ближайший «брат» по устройству и деплою — репозиторий `domain-checker` (Свободомен) того же автора; конвенции сервера описаны в `docs/deploy.md` репозитория `slovostat`.

Иконки сайта `static/favicon.svg`, `favicon.ico`, `apple-touch-icon.png` генерирует `brand/build.py` репозитория `delosvod` (знак семьи Делосвода: три бара по возрастанию, акцент `#FF8D00`) — руками не править; тот же знак инлайном в `<h1>` (`.header-icon`).

## Команды

```bash
cp .env.example .env                       # вписать AHREFS_API_KEY (ключ Ahrefs APIv3)
./start.sh                                 # = python3 server.py, http://localhost:3000
python3 -m unittest discover -s tests -v   # тесты, офлайн, ~3 с
python3 -m unittest tests.test_server.HttpTest.test_dr_ok_and_cache_hit -v   # один тест
docker compose up -d --build               # контейнер на 127.0.0.1:8003 (.env читает compose)
```

Открывать `static/index.html` с диска нельзя: фронтенд ходит по относительным `/api/…`. Для ручной проверки без ключа Ahrefs апстрим подменяется переменной `AHREFS_API_URL` (в тестах `fetch_upstream` подменён `mock.patch`).

## Архитектура

**`server.py`** — один файл, `ThreadingHTTPServer`, три роли:

1. **Прокси `/api/dr?target=`** — самое важное. Ключ Ahrefs один на всех посетителей, а лимит Ahrefs — 60 запросов/мин на ключ, поэтому лимит соблюдается **здесь, глобально**, а не в браузере:
   - `normalize_target` приводит ввод к ключу кэша и параметру для Ahrefs (схема/путь/`www.` срезаются, IDN → punycode); мусор → 400;
   - `TTLCache` (LRU + TTL, по умолчанию сутки) — HIT отдаётся сразу, заголовок `X-Cache`; кэшируются только успешные ответы;
   - `ConcurrencyGuard` — не больше `PER_IP_CONCURRENCY` одновременных запросов с IP (`TRUST_PROXY=1` → IP из `X-Forwarded-For`), иначе 429 + `Retry-After: 2`;
   - `UpstreamLimiter` — скользящее окно `UPSTREAM_RATE_PER_MIN` за 60 с с FIFO-очередью; `acquire(QUEUE_MAX_WAIT)` либо даёт слот, либо сразу (если ETA заведомо больше) / по таймауту возвращает 429 с оценкой `Retry-After`; при 429 от самого Ahrefs `pause()` останавливает выдачу слотов всем на `Retry-After` (или 5 с);
   - `fetch_upstream` изолирован — тесты подменяют именно его; ответ 200 проверяется на форму `domain_rating.domain_rating` до кэширования; ошибки Ahrefs (`["Error","Unauthorized"]`) разворачиваются `_ahrefs_error_text`, 5xx апстрима → 502.
2. **Статика** из `static/` с whitelist расширений `MIME` и проверкой, что путь не вышел за `static/` (`normpath` + prefix). HTML — `Cache-Control: no-cache`, ассеты — 5 мин.
3. **Служебное**: `/api/limits` (фронтенд берёт отсюда `max_domains`), `/healthz` (503 без ключа — так Docker-healthcheck и `update.sh` падают заметно), `/robots.txt`.

Конфиг — только переменные окружения (таблица в README), локально подгружаются из `.env` функцией `load_env_file`; в Docker их передаёт compose. Все настройки — константы в начале `server.py`.

**`static/app.js`** — IIFE без фреймворков. Поток: `parseDomains` → `uniqueDomains` → обрезка до `maxDomains` (с `/api/limits`) → пул из `MAX_CONCURRENCY` воркеров без собственного пейсинга (лимит держит сервер). При 429 `pauseAll` ставит общую паузу по `Retry-After` и повторяет домен до `MAX_RETRIES`; «Стоп»/Esc — `AbortController`, сигнал уходит и в `fetch`, и в `sleep`, недоделанные строки получают статус `skipped`. Статусы строки: `pending | ok | error | skipped`. `renderTable` и `exportCsv` идут через один и тот же `getFilteredResults → getSortedResults` (CSV = что видно). Пороги бейджей DR — `getDrClass` (≥51 / ≥21). `id` элементов в `static/index.html` — контракт с `app.js`.

**Метрика** — блок между `<!-- metrika:start -->…<!-- metrika:end -->` в конце `static/index.html` с плейсхолдером `__METRIKA_ID__`; `server.py` (`render_index`) подставляет `METRIKA_ID` из окружения или вырезает блок целиком, если переменная пуста. Единственный внешний запрос фронтенда, и только на проде.

**`static/privacy.html`** — политика конфиденциальности, `/privacy` (маппинг в `_static`); стили `.prose*` в `style.css`. Cookie-уведомление (`#cookie-notice`, cookie `nc_accepted` на 30 дней, как в «Крошке моей») — внутри блока Метрики в `index.html`: без счётчика сервис cookie не ставит.

**`static/style.css`** — тёмная тема, всё в CSS-переменных `:root`; шрифты системные (Google Fonts убраны намеренно — без внешних запросов).

## Деплой (VPS `lulu`, пользователь `deploy`)

Как у соседей: `/opt/domenomer` (git clone), `docker compose up -d --build`, порт `127.0.0.1:8003` (8000 slovostat, 8001 itogoskaz, 8002 domain-checker), Caddy на хосте — `deploy/domenomer.caddy` → `/etc/caddy/conf.d/`, валидировать **от пользователя caddy** (`sudo -u caddy caddy validate --config /etc/caddy/Caddyfile`), затем `systemctl reload caddy`. Обновление — `deploy/update.sh` (ff-only pull, rebuild, ждёт `/healthz`); его же дёргает GitHub Actions `deploy.yml` через SSH-ключ с forced command после зелёных `tests.yml`. Бренд-домены `domenomer.ru` / `доменомер.рф` (`xn--d1aca0abfedu.xn--p1ai`) — `deploy/domenomer-redirects.caddy`, ставить только когда их DNS указывает на сервер (иначе Caddy упрётся в лимиты Let's Encrypt).

## Соглашения и ограничения

- Все пользовательские строки — на русском (включая тексты ошибок сервера); хелпер `pluralize(n, one, few, many)`.
- Горячие клавиши: Ctrl/Cmd+Enter — запуск, Esc — стоп.
- Блок «Другие инструменты» в футере `static/index.html` — ручной список публичных инструментов экосистемы (Свободомен, Словостат, Словоправ) + ссылка на хаб delosvod.ru; при появлении новых инструментов дополнять здесь.
- Лицензия Ahrefs на DR-данные (<https://ahrefs.com/legal/domain-rating-license>): атрибуция «Domain Rating by Ahrefs» со ссылкой в футере обязательна и не должна скрываться; запрещено обходить лимиты и собирать датасет — поэтому кэш только в памяти и с TTL, никакой персистентной истории DR.
- Ключ Ahrefs — только в `.env`/окружении сервера, в ответы и фронтенд не попадает; `.env` в `.gitignore` и `.dockerignore`.
