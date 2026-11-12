# Синхронизация воркера RamOS с CRM

Воркер на Hermes после новой заявки и после смены статуса сам пишет в CRM. Разовая заливка уже лежащего реестра — `bin/import_reestr`, тем же шлюзом. Секреты в этот репозиторий не класть и в команду не подставлять.

Почту CRM по-прежнему не отправляет. SMTP, MAX и сканы СТС сюда не входят.

## Куда слать

Живой хост: `https://crm.77-91-113-64.sslip.io`. Приложение на Ядре слушает только `127.0.0.1:8091`.

Nginx в `location /s/` пускает пакеты **только с 127.0.0.1** и снимает basic auth. Секрет шлюза проверяет само приложение. Запрос на публичный адрес с другой машины (и даже curl с Ядра на свой внешний IP) получит 403 от nginx: источник не loopback.

Поэтому на Ядре:

| Как | Значение |
|---|---|
| Надёжно, мимо nginx | `DELO_CRM_URL=http://127.0.0.1:8091` |
| Именно живое имя (сертификат и SNI) | `DELO_CRM_URL=https://crm.77-91-113-64.sslip.io` и `DELO_CRM_CONNECT=127.0.0.1` |

`DELO_CRM_CONNECT` — куда открыть TCP. Имя в URL остаётся живым, nginx видит 127.0.0.1. Если переменная пустая, клиент идёт по DNS как обычно.

Секрет: `DELO_CRM_INBOUND_SECRET`. Это та же строка, что `INBOUND_SECRET` в `/root/ati/secrets/delo-crm.env` (mode 600). В git её нет. В argv её не передавать: она попадает в `ps` и в историю. Заголовок запроса — `X-Inbound-Secret`. Поле JSON `secret` тоже принимается и в базу не пишется.

Пока секрет короче 16 символов, шлюз отвечает 503.

## Новая заявка

`POST {DELO_CRM_URL}/s/inbound/doc`

```json
{
  "source": "manual",
  "number": "Д-2026-0001",
  "idempotent": true,
  "client_name": "ООО «Пример»",
  "client_email": "docs@example.ru",
  "route_from": "Москва",
  "route_to": "Казань",
  "cargo": "тент",
  "rate_rub": 68000,
  "load_date": "2026-11-05",
  "carrier_name": "Перевозчик 21",
  "carrier_email": "mail@example.ru",
  "driver_name": "Орлов К.",
  "plate": "А123ВС 777",
  "text": "комментарий заявки"
}
```

`source`: `mail`, `tg` или `manual`. `load_date` — `ГГГГ-ММ-ДД`. `rate_rub` — целое.

Ответ: `{"id","number","status","created"}`. Новый пакет начинается с `polucheno` / стадия «Находка».

### Номер

Без поля `number` CRM берёт следующий номер текущего года: `Д-2026-0001`, затем `0002`. Счётчик в `year_counters`, номера не переиспользуются.

С полем `number` (формат строго `Д-ГГГГ-NNNN`, как `Д-2026-0001`):

- пакета нет — карточка создаётся **с этим номером**. `last_seq` этого года поднимается до seq, если он больше. Назад счётчик не едет. Следующая заявка без номера получит `last_seq + 1`. Дырки ниже максимума сами не заполняются;
- пакет с этим номером уже есть — дописка, как у `thread_id`: пустые поля добираются, уже заполненные маршрут, груз и ставка не затираются. Новый номер не выдаётся. `created` будет `false`.

`idempotent: true` на повторе не пишет второе событие с тем же текстом и не кладёт файл с тем же именем ещё раз. Для почтового треда флаг не нужен: новый текст письма должен остаться в ленте. `thread_id` по-прежнему склеивает письма. Один `thread_id` на два разных номера — `409`.

Чтобы перезаписать уже заполненный маршрут, шлите его в `PATCH`, не рассчитывайте на повторный `POST`.

## Смена статуса

`PATCH {DELO_CRM_URL}/s/packages/Д-2026-0001`

Номер в пути можно не кодировать вручную, клиент сам кодирует `Д`. Тело:

```json
{"status": "otpravleno", "actor": "worker"}
```

Стадия считается по паре `STATUS_STAGE` (обратная сторона `STAGE_STATUS` в `app/domain.py`):

| `status` | стадия |
|---|---|
| `polucheno` | `nahodka` |
| `podgotovleno` | `zayavka` |
| `otpravleno` | `docs_sent` |
| `podpisano` | `podpisany` |
| `arhiv` | `originaly` |
| `otkaz` | `otkaz` |

Если воркер знает стадию точнее (`v_puti`, `dostavlen`, `docs_sent`), пришлите `stage`. Тогда статус берётся из `STAGE_STATUS`, и при обоих полях стадия главнее. Отказ без `comment` — `422`. Повторный `PATCH` с тем же статусом и стадией новое событие не пишет.

## Файл

`POST {DELO_CRM_URL}/s/packages/Д-2026-0001/events`

```json
{
  "type": "file",
  "actor": "worker",
  "text": "заявка",
  "attachments": [{"name": "Д-2026-0001-zayavka.pdf", "content_base64": "<байты>"}]
}
```

Потолок 8 МБ. Поле `url` CRM сохраняет и не скачивает.

## Тексты настроек

`GET {DELO_CRM_URL}/s/settings/snapshot`

Тот же секрет и тот же способ, что у `/s/inbound/doc`: заголовок `X-Inbound-Secret`
(поле `secret` в JSON для GET не используется). Без секрета — `401`, пока секрет
короче 16 символов — `503`.

```json
{
  "version": 7,
  "generated_at": "2026-11-12T10:15:00+04:00",
  "values": {
    "mail.from_name": "ИП Кртин А.С. («Перевозчик 21»)"
  }
}
```

В `values` только опубликованные значения. Ключа, который ни разу не публиковали
(или сбросили к умолчанию), в ответе нет: Дело само берёт своё умолчание. `version` —
общий счётчик публикаций, растёт на каждой публикации, откате и сбросе. Значения типа
`map` приходят строкой JSON. Воркеру достаточно сравнить `version` с прошлым снимком,
чтобы понять, что тексты менялись.

## Как вызвать с Hermes

Скопировать на Hermes `app/sync_client.py` (только стандартная библиотека) или пару `app/sync_client.py` + `bin/crm_post_inbound`. Рядом с CRM импорт такой: `from app.sync_client import post_inbound, patch_package`. Если файл лежит один, каталог должен быть в `PYTHONPATH`, импорт `from sync_client import post_inbound, patch_package`.

Процесс Hermes должен получить `DELO_CRM_URL` и `DELO_CRM_INBOUND_SECRET` из своего окружения (файл mode 600 на сервере, не из git). В коде секрета нет.

```python
from sync_client import patch_package, post_inbound

created = post_inbound({
    "source": "manual",
    "number": "Д-2026-0042",
    "idempotent": True,
    "client_name": "ООО «Пример»",
    "route_from": "Москва",
    "route_to": "Казань",
    "text": "новая заявка",
    "carrier_name": "Перевозчик 21",
})
patch_package(created["number"], {"status": "otpravleno", "actor": "worker"})
```

Тот же вызов без Python, секрет уже в окружении процесса, не в команде:

```sh
bin/crm_post_inbound inbound <<'JSON'
{"source":"manual","idempotent":true,"text":"новая заявка","client_name":"ООО «Пример»","carrier_name":"Перевозчик 21"}
JSON
bin/crm_post_inbound patch Д-2026-0001 <<'JSON'
{"status":"otpravleno","actor":"worker"}
JSON
```

`curl` с Ядра на loopback, секрет из уже открытого окружения, не литералом:

```sh
curl -sS -H "Content-Type: application/json" \
  -H "X-Inbound-Secret: ${DELO_CRM_INBOUND_SECRET}" \
  -X POST "${DELO_CRM_URL}/s/inbound/doc" \
  --data-binary @payload.json
```
