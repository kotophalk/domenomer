# ⚡ Доменомер

**<https://domenomer.delosvod.ru>** — бесплатная массовая проверка **Ahrefs Domain Rating (DR)** для списка доменов. Вставили список — получили таблицу с DR, отфильтровали, выгрузили в CSV. Бесплатный инструмент экосистемы [Делосвод](https://delosvod.ru/).

Работает через бесплатный эндпоинт Ahrefs `domain-rating-free` (0 API units), но ему нужен ключ APIv3 — он лежит на сервере и в браузер не попадает.

## Возможности

- Ввод списка доменов текстом или загрузкой TXT/CSV; URL и `www.` нормализуются, дубли убираются, кириллические домены (`.рф`) поддерживаются
- Таблица результатов с цветовыми бейджами DR (0–20 / 21–50 / 51+), сортировкой и фильтром по диапазону DR
- Экспорт в CSV ровно того, что видно в таблице (с UTF-8 BOM для Excel)
- Лимит Ahrefs (60 запросов/мин на ключ) соблюдается **на сервере, глобально**: общая FIFO-очередь со скользящим окном, кэш результатов на сутки — популярные домены отдаются мгновенно и не расходуют лимит
- Автоматический повтор при 429 (`Retry-After` или экспоненциальный backoff), кнопка «Стоп» (или Esc) с сохранением уже полученных результатов
- Ноль зависимостей: Python 3 (стандартная библиотека) и ванильные HTML/CSS/JS

## Запуск локально

```bash
git clone https://github.com/kotophalk/domenomer.git
cd domenomer
cp .env.example .env      # вписать AHREFS_API_KEY=...
./start.sh                # или: python3 server.py
```

Открыть <http://localhost:3000>. Ключ Ahrefs APIv3 создаётся в Ahrefs: **Account settings → API keys** ([подробнее](https://docs.ahrefs.com/docs/api/reference/api-keys-creation-and-management)); переменная окружения `AHREFS_API_KEY` имеет приоритет над `.env`.

Через Docker:

```bash
docker compose up -d --build   # порт 127.0.0.1:8003, .env читает compose
```

## API

`GET /api/dr?target=<домен>` — DR одного домена. Ответ Ahrefs пробрасывается как есть:

```json
{"domain_rating": {"domain_rating": 42.0, "license": "..."}}
```

Заголовок `X-Cache: HIT|MISS`. Ошибки — `{"error": "..."}`: `400` (некорректный домен), `429` (лимит на IP или очередь к Ahrefs заполнена; заголовок `Retry-After`), `502` (Ahrefs недоступен / неожиданный ответ), `503` (не задан ключ).

Служебные: `GET /api/limits` → `{"max_domains", "upstream_rate_per_min", "cache_ttl"}`; `GET /healthz` (503, если нет ключа).

## Настройки (переменные окружения)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AHREFS_API_KEY` | — | ключ Ahrefs APIv3, обязателен |
| `HOST`, `PORT` | `0.0.0.0`, `3000` | адрес и порт (в Docker — `8080`) |
| `UPSTREAM_RATE_PER_MIN` | `60` | лимит Ahrefs на ключ, держится глобально |
| `QUEUE_MAX_WAIT` | `30` | секунд ожидания слота в очереди, дальше 429 |
| `PER_IP_CONCURRENCY` | `6` | одновременных запросов к `/api/dr` с одного IP |
| `MAX_DOMAINS` | `200` | доменов за один прогон (сообщается фронтенду) |
| `CACHE_TTL`, `CACHE_MAX` | `86400`, `50000` | кэш успешных ответов, сек / записей (`0` — выключить) |
| `TRUST_PROXY` | `0` | `1` — брать IP клиента из `X-Forwarded-For` / `X-Real-IP` |
| `CORS_ALLOW_ORIGIN` | пусто | `*` или список origin через запятую — разрешить API с других сайтов |
| `AHREFS_API_URL` | боевой URL | подмена апстрима (для тестов) |
| `LOG_LEVEL` | `INFO` | уровень логирования |

## Деплой

Схема та же, что у соседних инструментов на том же VPS (подробно — в `docs/deploy.md` репозитория [slovostat](https://github.com/kotophalk/slovostat)): Caddy на хосте терминирует TLS, каждый инструмент — свой каталог в `/opt` со своим `docker-compose.yml` и портом на `127.0.0.1`.

```bash
sudo install -d -o deploy -g deploy /opt/domenomer
git clone https://github.com/kotophalk/domenomer.git /opt/domenomer
cd /opt/domenomer
cp .env.example .env          # вписать AHREFS_API_KEY; порт 8003
docker compose up -d --build
curl -s http://127.0.0.1:8003/healthz
```

Домен: [`deploy/domenomer.caddy`](deploy/domenomer.caddy) (`domenomer.delosvod.ru` → `127.0.0.1:8003`) кладётся в `/etc/caddy/conf.d/`, затем `sudo -u caddy caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`. Сертификат Caddy получит сам. Редиректы с бренд-доменов `domenomer.ru` / `доменомер.рф` — [`deploy/domenomer-redirects.caddy`](deploy/domenomer-redirects.caddy), ставится отдельно, когда их A-записи укажут на сервер.

Обновление: `/opt/domenomer/deploy/update.sh` (подтягивает `origin/main`, пересобирает, ждёт `/healthz`). Автодеплой: GitHub Actions после зелёных тестов на `main` запускает тот же скрипт по SSH-ключу с forced command; секреты `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_KNOWN_HOSTS` — как у slovostat.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

Офлайн: нормализация доменов, лимитер (окно, FIFO, пауза), кэш, лимит на IP, HTTP-контракт с подменённым апстримом, защита статики от обхода пути.

## Структура

* `server.py` — HTTP-сервер: статика, `/api/dr` (прокси к Ahrefs с очередью, кэшем и лимитами), `/api/limits`, `/healthz`.
* `static/` — фронтенд (ванильный JS/CSS, без сборки).
* `deploy/` — Caddy-блоки и `update.sh`.
* `tests/` — тесты.

## Лицензия данных

Domain Rating by [Ahrefs](https://ahrefs.com/). Использование DR-данных регулируется [Ahrefs Domain Rating License](https://ahrefs.com/legal/domain-rating-license): атрибуция обязательна, обход лимитов и сбор датасета запрещены — отсюда серверный лимит и короткий TTL кэша.
