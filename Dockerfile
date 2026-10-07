FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
COPY conftest.py ./conftest.py

COPY docker/gateway-entrypoint.sh /usr/local/bin/gateway-entrypoint
COPY docker/controller-entrypoint.sh /usr/local/bin/controller-entrypoint
RUN chmod +x /usr/local/bin/gateway-entrypoint /usr/local/bin/controller-entrypoint

EXPOSE 8000

CMD ["gateway-entrypoint"]
