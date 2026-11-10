# Установка в Windows

Нужны Windows 10/11 с WSL 2, [Docker Desktop](https://www.docker.com/products/docker-desktop/) в режиме **Linux containers** и PowerShell. Git нужен только для варианта `git clone`; проект можно скачать ZIP-архивом без установки Git. Рекомендуются не менее 4 ГБ оперативной памяти и 10 ГБ свободного места.

## 1. Установите и запустите Docker Desktop

Дождитесь готовности Docker Desktop, затем откройте новое окно PowerShell и выполните:

```powershell
$ErrorActionPreference = 'Stop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Установите Docker Desktop, запустите его и заново откройте PowerShell.'
}
docker compose version
if ($LASTEXITCODE -ne 0) { throw 'Docker Compose недоступен. Проверьте установку Docker Desktop.' }
docker info | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop ещё не запущен или его движок не готов.' }
```

Не продолжайте после красной ошибки: сначала устраните её причину. Для обычного скачивания и запуска PowerShell от администратора не требуется.

## 2. Скачайте проект без Git

Можно [скачать ZIP в браузере](https://github.com/kd170703/product/archive/refs/heads/main.zip) и распаковать его. Следующие команды сами скачивают архив и выбирают точную папку, поэтому не зависят от имени вашей папки «Загрузки». Повторный запуск использует уже распакованный проект и сохраняет существующий `.env`.

```powershell
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$productRoot = Join-Path $env:USERPROFILE 'product-service'
$productProject = Join-Path $productRoot 'product-main'

if (-not (Test-Path -LiteralPath (Join-Path $productProject 'compose.yaml') -PathType Leaf)) {
    if (Test-Path -LiteralPath $productRoot) {
        throw 'Папка product-service уже существует, но проект в ней не найден. Укажите другую новую папку в productRoot; существующие файлы не перезаписываются.'
    }
    $productArchive = Join-Path $env:TEMP ('product-' + [guid]::NewGuid().ToString('N') + '.zip')
    Invoke-WebRequest -Uri 'https://github.com/kd170703/product/archive/refs/heads/main.zip' -UseBasicParsing -OutFile $productArchive
    Expand-Archive -LiteralPath $productArchive -DestinationPath $productRoot
}
if (-not (Test-Path -LiteralPath (Join-Path $productProject '.env.example') -PathType Leaf)) {
    throw 'Не найден .env.example. Проверьте, что распакован полный проект.'
}
Set-Location -LiteralPath $productProject
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
New-Item -ItemType Directory -Force incoming, backups | Out-Null
notepad.exe (Join-Path $productProject '.env')
```

После успешного выполнения приглашение PowerShell должно указывать на `product-service\product-main`. Файл настроек находится именно в этой папке проекта.

<details>
<summary>Альтернатива: скачать через Git</summary>

Установите [Git for Windows](https://git-scm.com/download/win), полностью закройте PowerShell и откройте новое окно. Проверьте `git --version`; сообщение «git не распознано» означает, что команда ещё недоступна. Тогда используйте ZIP-вариант выше или проверьте установку Git и PATH.

```powershell
$ErrorActionPreference = 'Stop'
git --version
if ($LASTEXITCODE -ne 0) { throw 'Git недоступен. Переоткройте PowerShell после установки Git.' }
git clone https://github.com/kd170703/product.git
if ($LASTEXITCODE -ne 0) { throw 'Проект не скачан; не выполняйте следующие команды.' }
Set-Location -LiteralPath product
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
New-Item -ItemType Directory -Force incoming, backups | Out-Null
notepad.exe (Join-Path (Get-Location).Path '.env')
```

</details>

## 3. Настройте и запустите приложение

В `.env` замените `DJANGO_SECRET_KEY` длинной случайной строкой и `POSTGRES_PASSWORD` отдельным надёжным паролем. Сохраните изменения и закройте Блокнот. Для локального запуска оставьте `POSTGRES_DB=product`, `POSTGRES_USER=product`, `POSTGRES_HOST=db`, `POSTGRES_PORT=5432`, `DJANGO_HTTPS=0`. Не добавляйте `.env` в Git. Пример настройки не содержит рабочих секретов.

Случайный ключ можно получить в PowerShell и скопировать в `.env`:

```powershell
$randomBytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($randomBytes)
[Convert]::ToBase64String($randomBytes)
```

Запуск:

```powershell
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Запуск не завершён. Посмотрите ошибку выше.' }
docker compose ps
docker compose exec web python manage.py createsuperuser
if ($LASTEXITCODE -ne 0) { throw 'Учётная запись не создана. Проверьте готовность контейнера web.' }
```

Последняя команда интерактивно создаёт вашу учётную запись; готового пароля администратора в проекте нет. При первом запуске web применяет миграции, создаёт справочники и группы прав и собирает статические файлы. Worker запускается после готовности web.

Откройте **http://localhost:8000/** и войдите созданной учётной записью. Проверка состояния: **http://localhost:8000/health/**. Если контейнер ещё не готов, посмотрите журнал:

```powershell
docker compose logs --tail 100 web worker
```

Порт доступен только на этом компьютере (`127.0.0.1`). Для доступа коллег из сети нужен сервер с HTTPS; настройки приведены в [руководстве развёртывания](deployment.md).

Подробности обновления и резервных копий: [эксплуатация](operations.md).
