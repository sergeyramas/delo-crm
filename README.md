# CRM Делопроизводства ATI

Мини-CRM пакетов документов: входящее → карточка со стадиями → список, воронка, карточка, лента. Это не лиды AlpStream и не UTM.

Хост: Ядро `77.91.113.64`, домен `crm.77-91-113-64.sslip.io`, приложение только на `127.0.0.1`. Наружу смотрит nginx. Почту клиентам и перевозчикам CRM не отправляет. Telegram-бот на этот хост не переносится.

## Порт для Вишну

**8091** на **127.0.0.1**. Снаружи только nginx, URL `https://crm.77-91-113-64.sslip.io`. Если порт занят, поменяйте `PORT` в env и `server 127.0.0.1:8091` в nginx вместе. В firewall порт не открывать.

## Память

На Ядре около **1.8G** на всю машину. CRM — один процесс Python, без воркеров, без фоновой выгрузки сообщений и без своего кэша в памяти.

| | Значение |
|---|---|
| Процессы | `workers=1` в `bin/serve`. Второго воркера нет и переменной, которая его включит, тоже нет |
| Запросы сразу | `LIMIT_CONCURRENCY=20` (потолок в коде 40) |
| Очередь сокета | `BACKLOG=16` (потолок 64) |
| Keep-alive | 5 секунд |
| nginx → приложение | `keepalive 4` |
| Память юнита | `MemoryHigh=384M`, `MemoryMax=512M` |
| Список | 50 сделок на страницу, API не больше 100 за запрос |
| Доска | последние 80 обновлённых сделок |
| Лента сделки | последние 200 событий |

SQLite одна, соединение одно. Redis, Celery и пул процессов не ставим.

Токен MAX и HTTPS-выгрузка сообщений в этот PR не входят. Кнопка MAX только сохраняет черновик в CRM.

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

Поле `number` (`Д-2026-0001`) — для импорта и воркера. Пакета нет: карточка создаётся с этим номером, `year_counters` этого года поднимается до seq, если он больше (назад не откатывается). Пакет есть: дописка по номеру, как по `thread_id`, новый номер не выдаётся. Без этого поля следующий пакет снова берёт `last_seq + 1`. `idempotent: true` не пишет повтор того же текста и не кладёт второй файл с тем же именем. Контракт воркера: `docs/WORKER-CRM-SYNC.md`.

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
| POST | `/s/inbound/doc` | новый пакет `polucheno`, дописка по `thread_id` или по `number` |
| GET | `/s/packages?status=` | список, фильтр по статусу пакета |
| GET | `/s/packages/{id}` | карточка и события; `{id}` — число или номер `Д-2026-0001` |
| PATCH | `/s/packages/{id}` | поля и статус, без почты |
| POST | `/s/packages/{id}/events` | заметка или файл (`type`: `mail_in`, `mail_out`, `tg`, `status`, `note`, `file`) |

Отказ (`otkaz`) без комментария — `422`. Вложение `content_base64` пишется на диск (до 8 МБ). Ссылку `url` CRM сохраняет и не скачивает.

Экраны ходят в базу сами, секрет в браузер не попадает.

## Импорт реестра

Уже лежащие пакеты заливаются CSV (разделитель `;`) в живую базу. Колонки воркера: `skvoznoj_n`, `data`, `klient`, `marshrut`, `gruz`, `stavka`, `status`, `email_klient`, `email_perevozchik`, `komu`, `kommentarij`. Маршрут вида `Москва → Казань` режется на откуда/куда. Статус реестра `otpravleno` становится стадией `docs_sent` (пара `STAGE_STATUS` / `STATUS_STAGE`). Пустое `komu` и текст с Камалюковым или Кртиным пишутся как перевозчик «Перевозчик 21», иначе берётся ИП из `komu`. Водитель и госномер, если нет своих колонок, ищутся в комментарии.

Ставка `30000 без НДС` берёт число `30000`, пометку «без НДС» или «с НДС» кладёт в `flags` и в строку списка. Несколько адресов в `email_klient` через запятую или `;`: первый — `client_email`, остальные — в `client_note`.

Водитель, ТС, ИНН и прочие поля, которых нет в CSV, берутся из JSON той же заявки: `--payload-json payload-Д-2026-0001.json` или файл `payload-*.json` рядом с CSV. JSON ложится поверх строки с тем же `number`. Пустое поле JSON уже заполненное из CSV не затирает. Из воздуха импорт поля не выдумывает.

Вложения: `Д-2026-0001-*.pdf` и файлы `01-…`, `02-…`, `03-…`, `04-…` без переименования. Префикс `NN-` клеится к единственному номеру в CSV. Если номеров несколько, имя файла должно быть в `attachments` этого JSON. Отдельный файл: `--attach Д-2026-0001=/ПУТЬ/файл.pdf` или `--attach /ПУТЬ/файл.pdf`, когда в CSV один номер. Повтор не прикладывает тот же файл второй раз.

На Ядре nginx пускает `/s/` только с `127.0.0.1`. Команда бьёт в живое имя `https://crm.77-91-113-64.sslip.io`, а TCP открывает на loopback (`--connect`), иначе nginx ответит 403. Секрет читается из файла, в команду его не подставлять. Сначала сухой прогон:

```sh
cd /srv/delo-crm
.venv/bin/python bin/import_reestr /ПУТЬ/reestr.csv \
  --base-url https://crm.77-91-113-64.sslip.io \
  --connect 127.0.0.1 \
  --docs-dir /ПУТЬ/files \
  --payload-json /ПУТЬ/payload-Д-2026-0001.json \
  --dry-run
```

Каталог `--docs-dir` — тот, где лежат `01-zayavka-….pdf`, `02-….docx`, `03-….pdf`, `04-….pdf`. Ожидание сухого прогона одной строки: `created=1` … `attached=4` `errors=0`.

`--dry-run` только печатает `created/updated/attached/errors` и в CRM не ходит. База при этом не открывается, поэтому `updated` в сухом прогоне равен 0, а каждая разобранная строка считается в `created`. Боевой прогон — та же команда без `--dry-run` и с секретом:

```sh
cd /srv/delo-crm
.venv/bin/python bin/import_reestr /ПУТЬ/reestr.csv \
  --base-url https://crm.77-91-113-64.sslip.io \
  --connect 127.0.0.1 \
  --secret-file /root/ati/secrets/delo-crm.env \
  --docs-dir /ПУТЬ/files \
  --payload-json /ПУТЬ/payload-Д-2026-0001.json
```

`--secret-file` понимает строку секрета или env-файл, где есть `INBOUND_SECRET`. Тот же ключ можно положить в окружение процесса (`INBOUND_SECRET` или `DELO_CRM_INBOUND_SECRET`), не в argv. Прямой обход nginx, та же база: `--base-url http://127.0.0.1:8091` без `--connect`.

В конце одна строка `created=… updated=… attached=… errors=…`. Ненулевые ошибки — код выхода 1. `python -m app.seed` на эту базу не запускать.

Воркер после импорта шлёт новые заявки сам, см. `docs/WORKER-CRM-SYNC.md`. На Hermes копируется `app/sync_client.py` или `bin/crm_post_inbound`.

## Вход на экраны

Один из двух способов, не оба сразу, чтобы не было двойного пароля.

1. **nginx basic** (так в примере конфига). `AUTH_MODE=proxy`, `UI_GATE_SECRET` пустой. Приложение слушает только loopback, пароль спрашивает nginx (`htpasswd`). С машины на `127.0.0.1:8091` UI открыт тем, у кого есть shell.
2. **Пароль приложения.** `AUTH_MODE=basic`. Nginx без `auth_basic`. Заголовок `WWW-Authenticate`. `/health` и `/s/*` пароль экрана не спрашивают: шлюз закрыт своим секретом.

Учётки экрана — Сергей и Андрей, оба с ролью `admin` (в шапке «Админ»). Рядом в таблице `roles` остаются `dispatcher` («Диспетчер») и `operator` («Оператор»). Отдельного ACL по экранам нет: оба видят одни и те же сделки. Это задел, не матрица прав.

На Ядре в `/root/ati/secrets/delo-crm-auth.env` или в `/root/ati/secrets/delo-crm.env` выставить `DELO_USER_SERGEY_ROLE=admin` и `DELO_USER_ANDREY_ROLE=admin`. Пароли не менять.

Пароли только в `/root/ati/secrets/delo-crm-auth.env`. Пустое значение не стирает уже записанный хэш и не пускает в новую базу. В репозитории паролей нет, процесс их не печатает.

Локально `AUTH_MODE=off` (шапка «Андрей (логист)» без входа) или `basic` с паролями в своём `.env`, который не коммитится.

Старый одиночный вход `UI_USER` / `UI_PASSWORD` ещё работает, если у этой учётки нет хэша из `DELO_USER_*_PASSWORD`.

## Карточки контрагентов

Из сделки открываются три карточки: заказчик, перевозчик, водитель. На карточке — название или ФИО, контакт, компания, телефон, почта, ИНН, для перевозчика договор, для водителя авто, госномер, водительское удостоверение и паспорт (если поля пришли). Ниже список сделок этого контрагента.

Заявка (`POST /s/inbound/doc` и `PATCH`) сразу создаёт или обновляет карточку. Пустое поле заявки уже заполненное не затирает. Форма сделки пишет то, что в ней введено.

Ключ внутри одного типа (заказчик отдельно от водителя), сверху вниз:

| Что пришло | С какой карточкой склеиваем |
|---|---|
| ИНН, 10 или 12 цифр | с тем же ИНН |
| иначе телефон | с тем же телефоном |
| иначе почта | с той же почтой |
| иначе только имя | с тем же именем |

Телефон: только цифры, `8XXXXXXXXXX` приводится к `7XXXXXXXXXX`, короче 10 цифр ключом не считается. Один номер у заказчика и у водителя — две карточки. Если номер уже записан на другую карточку того же типа, он туда не переносится (ИНН важнее).

Поля заявки: `client_name`, `client_contact`, `client_phone`, `client_email`, `client_inn`, `client_company`, `carrier_name`, `carrier_phone`, `carrier_email`, `carrier_inn`, `carrier_contract`, `driver_name`, `driver_phone`, `driver_email`, `vehicle`, `plate`, `license_no` (или `driver_license`), `passport_no` (или `driver_passport`). Плюс прежние `client_hint` и `from`.

## Мессенджеры

По телефону карточка ставит отметку Telegram, WhatsApp, MAX: «есть», «нет» или «неизвестно». Кнопка «Написать» сама ничего не отправляет.

- Telegram: экран черновика и галочка подтверждения. Подпись первого исходящего в этом диалоге — «Привет, это ИИ-ассистент Сергея.», дальше — «ИИ-ассистент Сергея на связи.». От имени «Ассистент Андрея». Кто нажал, пишется отдельно.
- WhatsApp: ссылка `wa.me`. Сообщение набирает человек у себя.
- MAX: черновик остаётся в CRM. Боевой отправки нет.

`MESSENGER_CHECK=off` (так и должно стоять на Ядре) — бейджи «неизвестно», сеть не вызывается. `mock` — учебная заглушка из последней цифры телефона, тоже без сети. `live` не ходит в сеть и ничего не выгружает: боевой адаптер, токен MAX и приём сообщений по HTTPS в этот выпуск не входят. Токены, если когда-нибудь появятся, только в env на сервере (`MESSENGER_TG_TOKEN`, `MESSENGER_WA_TOKEN`, `MESSENGER_MAX_TOKEN`), в репозиторий их не класть.

Формы экрана сверяют cookie `delo_csrf`.

## Куда класть данные

Код и окружение: `/srv/delo-crm` (приложение и `.venv`). `/opt` не используется.

| | Путь |
|---|---|
| База и файлы, основной | `/srv/delo-crm/` (`delo.db`, каталог `files/`) |
| То же, если удобнее дерево ati | `/srv/ati/delo-crm/` |
| Секреты приложения | `/root/ati/secrets/delo-crm.env` (mode `600`) |
| Пароли Сергея и Андрея | `/root/ati/secrets/delo-crm-auth.env` (mode `600`) |
| Старый файл, если уже создан | `/srv/ati/secrets/delo-crm.env` — юнит его тоже читает |
| htpasswd nginx | `/srv/ati/secrets/delo-crm.htpasswd` (не в `/root`: www-data туда не зайдёт) |

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
sudo mkdir -p /srv/delo-crm/files /root/ati/secrets /srv/ati/secrets
sudo chown -R delo:delo /srv/delo-crm
# код в /srv/delo-crm, затем:
sudo python3 -m venv /srv/delo-crm/.venv
sudo /srv/delo-crm/.venv/bin/pip install -r /srv/delo-crm/requirements.txt
sudo chown -R delo:delo /srv/delo-crm
sudo install -m 600 /dev/null /root/ati/secrets/delo-crm.env
sudo install -m 600 /dev/null /root/ati/secrets/delo-crm-auth.env
# заполнить оба файла на сервере, не из репозитория
# delo-crm.env — PORT, DATA_DIR, INBOUND_SECRET, AUTH_MODE, NOTIFY_TG, MESSENGER_*
# delo-crm-auth.env — пароли не менять; роли: DELO_USER_SERGEY_ROLE=admin, DELO_USER_ANDREY_ROLE=admin
# те же две строки роли можно положить в delo-crm.env, если auth-файла с ролями нет
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
| `LIMIT_CONCURRENCY` | запросов сразу в одном процессе | `20` |
| `BACKLOG` | очередь сокета | `16` |
| `DATA_DIR` | каталог файлов | пусто → `./data` |
| `SQLITE_PATH` | файл базы | пусто → `$DATA_DIR/delo.db` |
| `INBOUND_SECRET` | секрет `/s/*` | пусто |
| `AUTH_MODE` | `off`, `basic`, `proxy` | `off` |
| `UI_USER` | логин basic | пусто |
| `UI_PASSWORD` | пароль basic | пусто |
| `UI_GATE_SECRET` | если nginx шлёт `X-Delo-Gate` | пусто |
| `UI_DISPLAY_NAME` | подпись в шапке без входа | `Андрей (логист)` |
| `NOTIFY_TG` | звать Telegram при смене статуса | `0` |
| `TG_BOT_TOKEN` | токен статуса, только если флаг 1 | пусто |
| `TG_CHAT_ID` | чат статуса, только если флаг 1 | пусто |
| `MESSENGER_CHECK` | `off`, `mock`, `live` | `off` |
| `MESSENGER_TG_TOKEN` | токен проверки/отправки Telegram, адаптер не подключён | пусто |
| `MESSENGER_WA_TOKEN` | токен WhatsApp, адаптер не подключён | пусто |
| `MESSENGER_MAX_TOKEN` | токен MAX, адаптер не подключён | пусто |
| `DELO_USER_SERGEY_PASSWORD` | пароль Сергея | пусто |
| `DELO_USER_SERGEY_ROLE` | `admin`, `dispatcher` или `operator` | `admin` |
| `DELO_USER_ANDREY_PASSWORD` | пароль Андрея | пусто |
| `DELO_USER_ANDREY_ROLE` | `admin`, `dispatcher` или `operator` | `admin` |

## Чего в этой версии нет

SMTP, автоподпись, разбор PDF в поля, оплата, новый VPS, перенос Telegram, IMAP, боевые токены MAX и WhatsApp. Входящее письмо позже может прислать воркер Вишну тем же `POST /s/inbound/doc`. Проверка мессенджеров и отправка в Telegram с карточки — заглушка, см. выше.
