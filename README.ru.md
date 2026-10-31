# Asterisk-Panel

[English version](README.md)

Веб-панель звонков Asterisk по образцу [awg-panel](https://github.com/BorisGB2001/awg-panel): все звонки в PostgreSQL, отчёты на экране, путь каждого звонка по IVR.

| | |
|---|---|
| Адрес | `https://<ip-сервера>:6443` (порт — `PANEL_PORT` в `.env`) |
| Каталог | `/opt/asterisk-panel` на сервере Asterisk |
| Сервисы | `asterisk-panel` (Python, FastAPI, 127.0.0.1:8080), `caddy` (HTTPS), `postgresql` |
| Данные | БД `asterisk` в PostgreSQL; `/opt/asterisk-panel/data/geo` — базы DB-IP |
| Вход | пароль + код из аутентификатора (TOTP) |

## Что показывает

- **Обзор** — звонки, отвеченные, пропущенные в очереди, ушедшие из IVR, ожидание оператора и SLA, время разговоров, телефоны в сети, активные каналы прямо сейчас, график по часам/дням, тепловая карта по дням недели и часам, группы, страны, последние звонки. Периоды: сегодня, вчера, 7/30 дней, месяц, прошлый месяц, произвольный.
- **Звонки** — поиск по номеру, имени и ID; фильтры по итогу, направлению, группе, стране, языку IVR, оператору, пункту меню; выгрузка CSV.
- **Карточка звонка** — кто звонил (номер, страна, регион, оператор связи), откуда пришёл (транк или телефон и IP, с которого он подключён), путь по IVR с названиями пунктов, нажатые кнопки, группы и переходы между ними, кто ответил, ожидание и разговор, плеер записи, **хронология по шагам** и **лог звонка** — события Asterisk как есть (CEL, queue_log, CDR).
- **Страны** — откуда звонят (по номеру, libphonenumber), регионы и операторы связи внутри страны, откуда подключены телефоны.
- **Группы** — поступило, отвечено, SLA, передано в следующую группу, брошено, неотвеченные вызовы, ожидание, разговоры; по каждой группе — операторы.
- **Операторы** — сколько раз звонило, ответил/не ответил, доля ответов, разговоры, внутренние звонки, в сети ли телефон.
- **IVR** — дерево меню с заходами, где кладут трубку, таймауты, сколько раз нажата каждая кнопка; выбор языка.
- **Телефоны** — регистрации SIP: IP, страна/город, провайдер, программа, RTT; история адресов, журнал регистраций, неудачные входы (подбор пароля) с IP и страной.
- **Журнал** — входы в панель, изменения учётных записей, прослушивание записей, выгрузки.
- **Администраторы** — суперадминистраторы и «только просмотр», временный пароль и TOTP при первом входе.

Светлая/тёмная тема, русский и английский интерфейс.

## Как собираются данные

Asterisk пишет в PostgreSQL сам — данные не теряются, даже если панель остановлена:

| Таблица | Модуль | Что |
|---|---|---|
| `cdr` | `cdr_pgsql` | записи CDR + колонка `recordingfile` (файл записи) |
| `cel` | `cel_pgsql` | все события каналов, `DTMF`, и события диалплана `CELGenUserEvent`: `LINE`, `RECORDING`, `IVR_NODE`, `IVR_KEY` (`узел:кнопка`, `узел:timeout`, `узел:invalid`), `IVR_EXIT`, `QUEUE_ENTER`, `QUEUE_GIVEUP` |
| `queue_log` | `res_config_pgsql` (realtime) | события очередей: вход, звонок оператору, ответ, неответ, брошен, переход в следующую группу |

Панель (роль `panel`, схема `panel`):

- **агрегатор** — каждые 5 с собирает завершённые звонки (`LINKEDID_END`) в таблицу `panel.calls`: путь по IVR, язык, группы, кто ответил, длительности, страна/регион номера, IP телефона;
- **коллектор AMI** (127.0.0.1:5038, пользователь `panel`) — регистрации телефонов (`ContactStatus`) и неудачные входы (security events) → `panel.devices`, `panel.device_log`, IP → страна по DB-IP Lite;
- **карта IVR** `/etc/asterisk/panel/ivr_map.json` — названия пунктов меню, кнопки, группы и сотрудники; формат — [ivr_map.example.json](deploy/asterisk-examples/ivr_map.example.json).

После изменения логики сборки звонков пересчитать все звонки:

```bash
ssh root@<сервер> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && PANEL_COLLECTORS=0 runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.calls rebuild'
```

## Установка

Требования: Debian 12/13 с Asterisk 20+ (собран с `cdr_pgsql`, `cel_pgsql`, `res_config_pgsql`, т. е. с `libpq-dev`).

1. Пароли БД и AMI — файл `secrets.env` на рабочей машине (в git не класть):
   ```
   DB_ASTERISK_PASSWORD=…
   DB_PANEL_PASSWORD=…
   AMI_SECRET=…
   ```
2. Панель — с рабочей машины (ставит PostgreSQL, Caddy, venv, создаёт БД, роли, таблицы, `.env`, сервис):
   ```bash
   ./deploy.sh root@<сервер> secrets.env
   ```
3. Настройте Asterisk с теми же паролями: запись в PostgreSQL, CEL и пользователь AMI — [pgsql-and-ami.conf](deploy/asterisk-examples/pgsql-and-ami.conf), события IVR в диалплане — [extensions.example.conf](deploy/asterisk-examples/extensions.example.conf). Карту IVR положите в `/etc/asterisk/panel/ivr_map.json` (формат — [ivr_map.example.json](deploy/asterisk-examples/ivr_map.example.json)), затем `core reload` и `logger reload`.
4. Первый администратор (временный пароль печатается один раз):
   ```bash
   ssh root@<сервер> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.admins create admin super'
   ```
   Вход: пользователь, временный пароль, **код пустой** → панель покажет QR для аутентификатора → затем попросит задать свой пароль.
5. Сертификат: по умолчанию `PANEL_TLS=internal` — Caddy выпускает сертификат своим CA (адрес частный, Let's Encrypt недоступен). Чтобы браузер не предупреждал, импортируйте корневой сертификат `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` в доверенные на рабочих машинах.

Чтобы не указывать сервер каждый раз: `echo root@<сервер> > .deploy-host` (файл в .gitignore).

Порт и адрес меняются в `/opt/asterisk-panel/.env` (`PANEL_HOST`, `PANEL_PORT`, `PANEL_TLS`), затем `/opt/asterisk-panel/deploy/install.sh`.

## Обновление

```bash
./deploy.sh root@<сервер>
```

Код копируется в `/opt/asterisk-panel`, обновляются зависимости, схема БД применяется заново (идемпотентно), сервис перезапускается. `.env`, `secrets.env` и данные сохраняются. Звонки в Asterisk не прерываются.

## Сброс доступа

```bash
# новый временный пароль и повторная настройка аутентификатора
ssh root@<сервер> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.admins reset admin'
```

## Разработка

```bash
cd panel && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest -q tests
.venv/bin/python -m app.i18n      # новые фразы → locales/en.json, недостающие переводы в ru.json
```
