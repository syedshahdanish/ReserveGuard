FROM python:3.11.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    PORTFOLIO_DEMO=1 \
    DATABASE_PATH=/tmp/reserveguard.sqlite

WORKDIR /app

RUN pip install --no-cache-dir --disable-pip-version-check tzdata==2025.2

COPY service.py ./

RUN useradd --uid 10001 --create-home reserveguard

USER 10001:10001

EXPOSE 8080

CMD ["python", "service.py"]