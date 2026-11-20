FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY data ./data
COPY ui ./ui
COPY eval ./eval

RUN pip install --upgrade pip && pip install -e . \
    && python -m data.generate --out data/payments.duckdb --seed 42 \
    && useradd --create-home appuser && chown -R appuser /app

USER appuser

EXPOSE 8000 8501
CMD ["uvicorn", "analyst.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
