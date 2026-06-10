FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY . .
RUN python -m playwright install --with-deps chromium || true
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
