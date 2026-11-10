# Корпоративный proxy и дополнительный CA

Эта инструкция относится к локальной сборке из исходников. Серверная установка обычно получает готовый образ из GHCR; её исходящий сетевой доступ и доверенный CA также настраивает администратор организации.

Обычный Windows/Docker Desktop использует стандартные корневые сертификаты. При корпоративном TLS proxy нужен утверждённый организацией CA. Dockerfile поддерживает необязательный BuildKit secret `proxy_ca` только на шаге установки Python-зависимостей. Не встраивайте секреты или session-specific CA в слои образа. Пример отдельного локального override-файла:

```yaml
services:
  web:
    build:
      secrets:
        - proxy_ca
    environment:
      HTTPS_PROXY: ${HTTPS_PROXY}
      HTTP_PROXY: ${HTTP_PROXY}
      NO_PROXY: localhost,127.0.0.1,db,web
      SSL_CERT_FILE: /run/proxy-ca.pem
      REQUESTS_CA_BUNDLE: /run/proxy-ca.pem
    volumes:
      - ${PRODUCT_CA_BUNDLE}:/run/proxy-ca.pem:ro
  worker:
    build:
      secrets:
        - proxy_ca
    environment:
      HTTPS_PROXY: ${HTTPS_PROXY}
      HTTP_PROXY: ${HTTP_PROXY}
      NO_PROXY: localhost,127.0.0.1,db,web
      SSL_CERT_FILE: /run/proxy-ca.pem
      REQUESTS_CA_BUNDLE: /run/proxy-ca.pem
    volumes:
      - ${PRODUCT_CA_BUNDLE}:/run/proxy-ca.pem:ro
secrets:
  proxy_ca:
    file: ${PRODUCT_CA_BUNDLE}
```

`PRODUCT_CA_BUNDLE` указывает на полный доверенный CA bundle с корневыми сертификатами и корпоративным CA. Запуск: `docker compose -f compose.yaml -f compose.proxy.yaml up -d --build`. Не сохраняйте адреса с паролями/ключами в отслеживаемых файлах. Proxy-настройка сетевого доступа не гарантирует доступ к сайтам Банка России, Росстата или ФНС: результат проверяется отдельным реальным запросом.
