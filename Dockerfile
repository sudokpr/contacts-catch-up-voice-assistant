FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ ./app/
COPY scripts/ ./scripts/

RUN mkdir -p /data

ENV PORT=8000
ENV DATABASE_URL=/data/contacts.db

EXPOSE 8000

CMD uv run uvicorn app.main:app --host 0.0.0.0 --port $PORT
