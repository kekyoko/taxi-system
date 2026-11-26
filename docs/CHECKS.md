# Налаштування зовнішньої перевірки (`checks.yaml`)

Засіб викладача звертається до запущеного стенда як зовнішній клієнт. Він читає
`openapi.yaml` (маршрути, `x-course`, іменовані приклади `normal`, `scarce`, `missing`,
`forbidden`) і `checks.yaml` (те, чого контракт не каже).

* `items` — виконані пункти поза ядром; ядро перевіряється завжди.
* `instances`, `gateways` — діагностичні порти з `docker-compose.yml`. Клієнтські запити
  йдуть тільки через nginx `http://localhost:8080`; прямі адреси потрібні, щоб порівняти
  два екземпляри й перевірити canary.
* `services.broker: redis` — Redis Streams виступає брокером (псевдонім `broker` у Compose).
* `log_fields` — names полів JSON-рядку в `app/main.py` (`time`, `instance`, `route`,
  `status`, `duration_ms`), X-Request-ID повертається в заголовку кожної відповіді.
* `heartbeat_s: 20` — інтервал heartbeat менший за `proxy_read_timeout 60s` у nginx.
* `simulator_down` — маршрут `POST /sim/admin/unavailable?seconds=N` робить симулятор
  тимчасово недоступним (503 не закріплюється за ключем).

Збережені запити клієнта попередньої версії лежать у [client-v1.json](client-v1.json);
їх надсилає перевірка сумісності О6.2. Таблиця поєднань версій — у [compat-table.md](compat-table.md).

Локальна команда перевірки (після `docker compose up -d --build`):

```bash
python -m pip install -r requirements.txt
python scripts/check.py          # 33 перевірки через nginx
python -m pytest tests -q        # автоматичні тести сумісності
python scripts/replay.py         # збережені запити клієнта v1
```
