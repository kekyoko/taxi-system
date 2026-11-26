# Середовище запуску

## Основний варіант (Docker Compose)

Вимоги: Docker 24+, Docker Compose v2.24+, близько 4 ГБ RAM. Вільні порти:

| Порт | Сервіс | Призначення |
|---|---|---|
| 8080 | nginx | єдина адреса клієнта (`http://localhost:8080`, `/web/`) |
| 5432 | postgres | база (діагностика) |
| 6379 | redis / `broker` | брокер Streams із AOF (діагностика) |
| 8001, 8002 | app-a, app-b | окремі екземпляри (діагностика, canary) |
| 8085 | external-sim | симулятор (діагностика) |
| 8086, 8087 | gateway-a, gateway-b | WebSocket-шлюзи (діагностика) |

Команда з README: `docker compose up -d --build`. Сервіси `migrate` і `seed` виконуються
автоматично перед стартом app. pgdata зберігається в іменованому томі `pgdata`. Redis
запускається з `--appendonly yes --appendfsync everysec`: події Streams переживають
рестарт брокера, що потрібно для відновлення після керованих відмов (О4.4).

## Локальний стенд без Docker

На робочій машині Docker був відсутній, тому для перевірки використано портативні
PostgreSQL 16.16-1, Redis 7.4.8 (msys2) і nginx 1.27.5 з конфігурацією, еквівалентною
`nginx/nginx.conf`, а також supervisor `start.py`/`stop.py` у тимчасовій теці. Адреси
сервісів ті самі; клієнт і перевірки звертаються лише до nginx `:8080`.
Це середовище є лише засобом демонстрації — офіційний запуск лишається Compose.

## Перевірка готовності

```bash
curl -i http://localhost:8080/health
curl -i http://localhost:8080/ready
python scripts/check.py
python scripts/replay.py
python -m pytest tests -q
```

Усі ці команди працюють через nginx, крім прямих адрес у `checks.yaml`, які потрібні
лише для порівняння двох екземплярів.
