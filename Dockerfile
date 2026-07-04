FROM python:3.10-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY configs ./configs
COPY scripts ./scripts
COPY data ./data

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["python", "-m", "src.main"]
