# Деплой на Google Cloud (бесплатно)

Бот работает на бесплатной виртуальной машине Google Cloud (Always Free) и стоит $0 в месяц.

## Где работает

| Параметр | Значение |
|---|---|
| Проект | `rat-bot-513414` |
| Машина | `rat-bot`, зона `us-central1-c` |
| Тип | `e2-micro` (2 shared vCPU, 1 ГБ RAM) + 2 ГБ swap |
| ОС | Debian 13 (trixie) |
| Диск | 30 ГБ Standard persistent disk |
| Сеть | **только внешний IPv6**, внешнего IPv4 нет |
| Папка бота | `~/rat-bot` (пользователь `parmezzzan`) |

### Почему бесплатно и почему без IPv4

Always Free покрывает одну `e2-micro` в `us-central1`/`us-west1`/`us-east1`, до 30 ГБ Standard-диска и 1 ГБ исходящего трафика в месяц.

Внешний IPv4 в бесплатный тариф **не входит**: $0.005/час, это примерно $3.65 в месяц. Бесплатно только 1 час в месяц на аккаунт. Внешний IPv6 бесплатный, поэтому IPv4 у машины удалён.

Боту нужны только исходящие соединения, и все нужные сервисы доступны по IPv6:
- `api.telegram.org`,
- `generativelanguage.googleapis.com` (Gemini),
- `pypi.org`, `files.pythonhosted.org`,
- `deb.debian.org`.

**GitHub по IPv6 недоступен**, поэтому для `git pull` IPv4 включается временно (см. «Обновление бота»).

Цены: https://cloud.google.com/free/docs/free-cloud-features и https://cloud.google.com/vpc/network-pricing.

## Особенности сервера

### `docker-compose.override.yml`

На сервере лежит файл `docker-compose.override.yml`. В git его нет, он создаётся вручную:

```yaml
services:
  bot:
    network_mode: host
    build:
      network: host
```

Он нужен, потому что стандартная bridge-сеть Docker даёт контейнеру только IPv4, а у машины внешнего IPv4 нет. С `network_mode: host` контейнер использует сеть машины и ходит наружу по IPv6. Docker Compose подхватывает override-файл автоматически.

### Как зайти на машину

Консоль Google Cloud → Compute Engine → VM instances → кнопка **SSH** у `rat-bot`. Без внешнего IPv4 подключение идёт через туннель Google (IAP), это бесплатно.

Команды `gcloud`, которые меняют настройки машины, выполняются в **Cloud Shell**: иконка `>_` вверху консоли.

## Обновление бота

1. **Cloud Shell:** временно включить IPv4.
   ```bash
   gcloud compute instances add-access-config rat-bot --zone=us-central1-c
   ```
2. **SSH машины:** подтянуть код и пересобрать.
   ```bash
   cd ~/rat-bot && git pull && docker compose up -d --build
   ```
3. **Cloud Shell:** выключить IPv4.
   ```bash
   gcloud compute instances delete-access-config rat-bot --zone=us-central1-c --access-config-name="external-nat"
   ```
4. **SSH машины:** перезапустить бота.
   ```bash
   cd ~/rat-bot && docker compose restart bot
   ```

Шаг 4 обязателен. После удаления IPv4 бот остаётся с повисшими соединениями, открытыми по IPv4. Он продолжает получать команды, но не может отвечать: в логах `httpx.ConnectTimeout` / `telegram.error.TimedOut`. Перезапуск открывает новые соединения по IPv6.

Чтобы обновление было бесплатным, держите IPv4 включённым меньше часа в месяц. Обычно хватает 5–10 минут.

## Полезные команды (SSH машины)

```bash
cd ~/rat-bot
docker compose ps                     # статус
docker compose logs --tail 50 bot     # последние логи
docker compose logs -f bot            # логи в реальном времени
docker compose restart bot            # перезапуск
free -h                               # память и swap
```

После перезагрузки машины бот поднимается сам (`restart: always`).

## Бэкапы

Бот каждый день в `BACKUP_TIME` (04:00 МСК) отправляет файл базы в чат `BACKUP_CHAT_ID`. Вручную бэкап делается командой `/backup`. Снапшоты дисков в Google Cloud не настроены, потому что они платные.

Восстановление из бэкапа:
1. Скачать `.sqlite3` из Telegram.
2. Загрузить его в Cloud Storage: консоль → Cloud Storage → Buckets → создать bucket в `us-central1`, класс Standard → Upload.
3. На машине (Cloud Storage доступен по IPv6):
   ```bash
   cd ~/rat-bot && docker compose stop bot
   gcloud storage cp gs://BUCKET/ФАЙЛ.sqlite3 data/messages.sqlite3
   docker compose start bot
   ```
4. Удалить bucket.

## Контроль расходов

В Billing → Budgets & alerts настроен бюджет $1 с письмами на 50/90/100%. Расходы по позициям смотрят в Billing → Reports → Group by: SKU. Строки `External IP Charge` там быть не должно, кроме нескольких минут обновлений.

## Установка с нуля

1. **Машина.** Compute Engine → Create instance:
   - `e2-micro`, регион `us-central1`, модель Standard (не Spot);
   - Debian, **Standard persistent disk** 30 ГБ;
   - Data protection: **No backups**;
   - Observability: снять **Install Ops Agent**.
2. **Swap, Docker, git** (SSH машины). После команд переподключиться по SSH, чтобы применилась группа `docker`.
   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   curl -fsSL https://get.docker.com | sudo sh && sudo apt-get install -y git && sudo usermod -aG docker $USER
   ```
3. **Бот.**
   ```bash
   git clone https://github.com/konstantin-fomin/rat-bot ~/rat-bot
   ```
   Положить `.env` в `~/rat-bot/`, базу в `~/rat-bot/data/messages.sqlite3`, создать `docker-compose.override.yml` (см. выше), затем:
   ```bash
   cd ~/rat-bot && mkdir -p data logs && docker compose up -d --build
   ```
4. **Переход на IPv6** (Cloud Shell):
   ```bash
   gcloud compute networks update default --switch-to-custom-subnet-mode --quiet
   gcloud compute networks subnets update default --region=us-central1 --stack-type=IPV4_IPV6 --ipv6-access-type=EXTERNAL
   gcloud compute instances network-interfaces update rat-bot --zone=us-central1-c --network-interface=nic0 --stack-type=IPV4_IPV6 --ipv6-network-tier=PREMIUM
   ```
   Проверить IPv6 на машине (должны прийти HTTP-коды, а не ошибки):
   ```bash
   for u in https://api.telegram.org https://generativelanguage.googleapis.com https://pypi.org/simple/pip/; do curl -6 -sS -m 10 -o /dev/null -w "%{http_code}  $u\n" $u; done
   ```
   Затем удалить IPv4 (Cloud Shell):
   ```bash
   gcloud compute instances delete-access-config rat-bot --zone=us-central1-c --access-config-name="$(gcloud compute instances describe rat-bot --zone=us-central1-c --format='value(networkInterfaces[0].accessConfigs[0].name)')"
   ```
   И перезапустить бота (SSH машины):
   ```bash
   cd ~/rat-bot && docker compose restart bot
   ```
