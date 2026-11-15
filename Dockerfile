FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Build public admin assets into the immutable image, not the read-only runtime filesystem.
# Build-time placeholder settings are ephemeral and MUST NOT use real production secrets.
RUN SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(64))')" \
    MEDIA45_HOST=localhost \
    MEDIA45_ORIGIN=https://localhost \
    python manage.py collectstatic --noinput
RUN mkdir -p /app/coverdata && chown app:app /app/coverdata
USER app
EXPOSE 8000
