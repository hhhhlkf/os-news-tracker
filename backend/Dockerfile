FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN python -m playwright install --with-deps chromium
COPY . .
CMD ["uvicorn", "app.entry:app", "--host", "0.0.0.0", "--port", "8000"]
