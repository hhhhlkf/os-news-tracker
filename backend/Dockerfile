FROM python:3.11-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN python -m playwright install --with-deps chromium
COPY . .
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
