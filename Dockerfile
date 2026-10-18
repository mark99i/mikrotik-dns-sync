FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY main.py service.py models.py ros_api.py config.py helpers.py /app/

# Непривилегированный пользователь
RUN useradd --system --no-create-home --uid 1000 dnssync \
    && chown -R dnssync:dnssync /app
USER dnssync

# config.json5 и CA-сертификаты монтируются извне (см. docker-compose.example.yaml)
ENTRYPOINT ["python", "/app/main.py", "-c", "/app/config.json5"]
CMD ["--dry-run"]
