FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
# Secrets (.env*, *.key, *.pem) are excluded by .dockerignore and never baked into layers.
COPY . /app
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/artifacts/tts-cache /home/app/.cache/huggingface \
    && chown -R app /app /home/app/.cache
USER app
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
