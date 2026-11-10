#!/bin/sh
# Generate production configuration once. Never replace an existing secret.
set -eu

if [ "$#" -gt 1 ]; then
    echo "Использование: sh scripts/setup-server.sh [monitoring.organization.ru]" >&2
    exit 2
fi

command -v python3 >/dev/null 2>&1 || {
    echo "Нужен Python 3 для безопасного создания секретов." >&2
    exit 1
}

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

python3 - "$project_dir" "${1:-}" <<'PY'
import os
from pathlib import Path
import re
import secrets
import sys

project = Path(sys.argv[1])
domain = sys.argv[2].strip()
if domain and not re.fullmatch(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", domain
):
    raise SystemExit("Укажите доменное имя без https://, порта и пути.")

os.umask(0o077)
config = project / ".env.prod"
if config.is_symlink():
    raise SystemExit(".env.prod должен быть обычным файлом, а не символической ссылкой.")
secret_dir = project / ".secrets"
if secret_dir.is_symlink():
    raise SystemExit(".secrets должен быть обычным каталогом, а не символической ссылкой.")
if not config.exists():
    if not domain:
        raise SystemExit("Для первого запуска укажите домен: sh scripts/setup-server.sh monitoring.organization.ru")
    text = (project / ".env.prod.example").read_text(encoding="utf-8")
    if domain:
        text = text.replace("APP_DOMAIN=\n", f"APP_DOMAIN={domain}\n", 1)
    # Exclusive creation prevents simultaneous setup from overwriting a config.
    try:
        with config.open("x", encoding="utf-8") as stream:
            stream.write(text)
        print("Создан .env.prod; проверьте домен, образы и каталог резервных копий.")
    except FileExistsError:
        pass
else:
    print("Существующий .env.prod сохранён без изменений.")
config.chmod(0o600)

secret_dir.mkdir(mode=0o700, exist_ok=True)
secret_dir.chmod(0o700)
for name in ("django_secret_key", "db_password", "backup_password", "openai_api_key"):
    path = secret_dir / name
    if path.is_symlink():
        raise SystemExit(f"Секрет не должен быть символической ссылкой: {path}")
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write("" if name == "openai_api_key" else secrets.token_urlsafe(64) + "\n")
        print(f"Создан файл секрета: .secrets/{name}")
    except FileExistsError:
        if not path.is_file():
            raise SystemExit(f"Ожидался обычный файл: {path}")
    # Compose file secrets are bind mounts: the non-root app must be able to
    # read the file in its container. The host parent directory stays 0700.
    path.chmod(0o444)

incoming = project / "incoming"
if not incoming.exists():
    incoming.mkdir(mode=0o755)
    incoming.chmod(0o755)
if incoming.stat().st_mode & 0o005 != 0o005:
    print("Внимание: incoming должен быть доступен UID 10001 на чтение и проход.")
backup_dir = project / "backups-prod"
backup_dir.mkdir(mode=0o700, exist_ok=True)

print("Значения секретов не выводятся. Сохраните .secrets вне сервера в защищённом хранилище.")
print("Размещайте проект внутри приватного каталога сервера с правами 0700.")
print("После проверки .env.prod: sh scripts/server.sh pull; sh scripts/server.sh up -d")
PY
