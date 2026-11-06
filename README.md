# CRM Делопроизводства ATI

Мини-CRM пакетов документов: входящее → карточка со стадиями → список, воронка, карточка, лента. Это не лиды AlpStream и не UTM.

Хост: Ядро `77.91.113.64`, домен `crm.77-91-113-64.sslip.io`, приложение только на `127.0.0.1`. Наружу смотрит nginx. Почту клиентам и перевозчикам CRM не отправляет. Telegram-бот на этот хост не переносится.

## Порт для Вишну

**8091** на **127.0.0.1**. В юните и в nginx стоит один и тот же порт. Если порт занят, поменяйте `PORT` и `proxy_pass` вместе и не публикуйте его в firewall.

## Две оси карточки

Макеты показывают воронку сделки из девяти стадий. ТЗ требует статус пакета документов. В базе живут оба поля.

| Стадия сделки (экран) | Статус пакета (API) |
|---|---|
| Находка, На одобрении | `polucheno` |
| Подтверждён, Заявка сформирована | `podgotovleno` |
| Документы отправлены | `otpravleno` |
| Подписаны, Водитель в пути, Доставлен | `podpisano` |
| Оригиналы/оплата | `arhiv` |
| Отказ | `otkaz` |

`POST /s/inbound/doc` создаёт пакет в статусе `polucheno` и ставит стадию «Находка». Номер `Д-2026-0001`: год и счётчик, счётчик на год атомарный, с 0001, номера не переиспользуются. Повтор с тем же `thread_id` дописывает событие в ту же карточку.

Смена стадии на экране пересчитывает статус. `PATCH` со статусом не откатывает более мелкую стадию, если она уже внутри этого статуса: «На одобрении» остаётся при `polucheno`.

Кнопка на карточке двигает стадию внутри CRM. Письмо из неё не уходит. Запись «исходящее» — это факт, который человек внёс руками.

## Локально

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# в .env задать INBOUND_SECRET (длинная случайная строка) — иначе /s/* отвечает 503
.venv/bin/python bin/serve
```

Открыть http://127.0.0.1:8091/ . `bin/serve` откажется слушать не loopback.

Проверки:

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
sh bin/smoke   # берёт .venv/bin/python, если он есть
```

`bin/smoke` поднимает пустую базу на свободном порту, шлёт inbound со свежим секретом и проверяет номер `Д-<текущий год>-0001` и статус `polucheno`. На дате 2026-11-06 это `Д-2026-0001`. Секрет smoke в файлы не пишет.

Макет девяти сделок (как на экранах 06.11, данные вымышленные):

```sh
.venv/bin/python -m app.seed
```

Seed занимает номера `Д-2026-0001` … `Д-2026-0009`. На боевую базу его не ставить: скрипт не пишет в путь `/srv`. Следующий живой пакет того же года получит `0010`.

## API

Секрет шлюза — заголовок `X-Inbound-Secret` или поле JSON `secret` (поле в базу не кладётся). В query секрет не принимается. Это тот же приём, что `inbox_site_secret` у [alpstream-crm](https://github.com/sergeyramas/alpstream-crm) на `POST /s/inbound/lead`: отдельный ключ только на `/s/*`, не пароль экрана. Там ключ проверяется HMAC (`X-Alp-Ts`, `X-Alp-Nonce`, `X-Alp-Sig`). Здесь сверка строки: почтовый воркер ещё не подключён, вход — ручной POST.

Пока `INBOUND_SECRET` короче 16 символов, шлюз отвечает `503`.

```sh
curl -sS -H "Content-Type: application/json" \
  -H "X-Inbound-Secret: $INBOUND_SECRET" \
  -X POST http://127.0.0.1:8091/s/inbound/doc \
  -d '{"source":"mail","from":"Иванова <docs@example.ru>","subject":"Заявка","text":"Текст","client_hint":"ООО «Пример»"}'
```

Ответ: `{"id","number","status","created"}`. `source`: `mail`, `tg` или `manual`.

| Метод | Путь | Что делает |
|---|---|---|
| POST | `/s/inbound/doc` | новый пакет `polucheno` или дописка по `thread_id` |
| GET | `/s/packages?status=` | список, фильтр по статусу пакета |
| GET | `/s/packages/{id}` | карточка и события; `{id}` — число или номер `Д-2026-0001` |
| PATCH | `/s/packages/{id}` | поля и статус, без почты |
| POST | `/s/packages/{id}/events` | заметка или файл (`type`: `mail_in`, `mail_out`, `tg`, `status`, `note`, `file`) |

Отказ (`otkaz`) без комментария — `422`. Вложение `content_base64` пишется на диск (до 8 МБ). Ссылку `url` CRM сохраняет и не скачивает.

Экраны ходят в базу сами, секрет в браузер не попадает.

## Вход на экраны

Один из двух способов, не оба сразу, чтобы не было двойного пароля.

1. **nginx basic** (так в примере конфига). `AUTH_MODE=proxy`, `UI_GATE_SECRET` пустой. Приложение слушает только loopback, пароль спрашивает nginx (`htpasswd`). С машины на `127.0.0.1:8091` UI открыт тем, у кого есть shell.
2. **Пароль приложения.** `AUTH_MODE=basic`, `UI_USER` и `UI_PASSWORD` в env. Nginx без `auth_basic`. Заголовок `WWW-Authenticate`. `/health` и `/s/*` пароль экрана не спрашивают: шлюз закрыт своим секретом.

Локально `AUTH_MODE=off`.

Формы экрана сверяют cookie `delo_csrf`.

## Куда класть данные

Код: `/opt/delo-crm`.

| | Путь |
|---|---|
| База и файлы, основной | `/srv/delo-crm/` (`delo.db`, каталог `files/`) |
| То же, если удобнее дерево ati | `/srv/ati/delo-crm/` |
| Секреты | `/srv/ati/secrets/delo-crm.env` (mode `600`) и `delo-crm.htpasswd` |

В env боевого хоста:

```sh
HOST=127.0.0.1
PORT=8091
DATA_DIR=/srv/delo-crm
SQLITE_PATH=/srv/delo-crm/delo.db
INBOUND_SECRET=
AUTH_MODE=proxy
NOTIFY_TG=0
```

`DATA_DIR=/srv/ati/delo-crm` и `SQLITE_PATH=/srv/ati/delo-crm/delo.db` — если выбран второй каталог. Один путь, не оба сразу.

## Выкладка

```sh
sudo useradd --system --create-home --home-dir /var/lib/delo --shell /usr/sbin/nologin delo || true
sudo mkdir -p /opt/delo-crm /srv/delo-crm/files /srv/ati/secrets
sudo chown -R delo:delo /srv/delo-crm
# код в /opt/delo-crm, затем:
sudo python3 -m venv /opt/delo-crm/.venv
sudo /opt/delo-crm/.venv/bin/pip install -r /opt/delo-crm/requirements.txt
sudo chown -R delo:delo /opt/delo-crm
sudo install -m 600 /dev/null /srv/ati/secrets/delo-crm.env
# заполнить env на сервере, не из репозитория
sudo cp deploy/delo-crm.service /etc/systemd/system/delo-crm.service
sudo systemctl daemon-reload
sudo systemctl enable --now delo-crm
sudo cp deploy/nginx-crm.77-91-113-64.sslip.io.conf /etc/nginx/sites-available/delo-crm.conf
sudo ln -s /etc/nginx/sites-available/delo-crm.conf /etc/nginx/sites-enabled/delo-crm.conf
sudo htpasswd -c /srv/ati/secrets/delo-crm.htpasswd andrey
sudo nginx -t && sudo systemctl reload nginx
```

Проверка с Ядра:

```sh
curl -sS -H "Content-Type: application/json" \
  -H "X-Inbound-Secret: $INBOUND_SECRET" \
  -X POST http://127.0.0.1:8091/s/inbound/doc \
  -d '{"source":"manual","text":"проверка выкладки","client_hint":"Тест"}'
```

Ожидание: `Д-2026-0001` и `"status":"polucheno"`, если база пустая и год 2026. Экран: `https://crm.77-91-113-64.sslip.io/` после сертификата или `http://` до certbot.

`NOTIFY_TG` оставить `0`. Кода бота, webhook и long polling в репозитории нет. При `NOTIFY_TG=1` и заполненных `TG_BOT_TOKEN` и `TG_CHAT_ID` смена статуса пакета делает один `sendMessage`. Пока группа не согласована, флаг не включать.

## Переменные

| Имя | Смысл | В примере |
|---|---|---|
| `HOST` | адрес bind, только loopback | `127.0.0.1` |
| `PORT` | порт за nginx | `8091` |
| `DATA_DIR` | каталог файлов | пусто → `./data` |
| `SQLITE_PATH` | файл базы | пусто → `$DATA_DIR/delo.db` |
| `INBOUND_SECRET` | секрет `/s/*` | пусто |
| `AUTH_MODE` | `off`, `basic`, `proxy` | `off` |
| `UI_USER` | логин basic | пусто |
| `UI_PASSWORD` | пароль basic | пусто |
| `UI_GATE_SECRET` | если nginx шлёт `X-Delo-Gate` | пусто |
| `UI_DISPLAY_NAME` | подпись в шапке | `Андрей (логист)` |
| `NOTIFY_TG` | звать Telegram при смене статуса | `0` |
| `TG_BOT_TOKEN` | токен, только если флаг 1 | пусто |
| `TG_CHAT_ID` | чат, только если флаг 1 | пусто |

## Чего в этой версии нет

SMTP, автоподпись, разбор PDF в поля, оплата, новый VPS, перенос Telegram, IMAP. Входящее письмо позже может прислать воркер Вишну тем же `POST /s/inbound/doc`.
