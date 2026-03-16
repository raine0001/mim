FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    pydantic \
    pydantic-settings \
    python-dotenv \
    sqlalchemy \
    asyncpg \
    SpeechRecognition

EXPOSE 8000

CMD ["uvicorn", "core.app:app", "--host", "0.0.0.0", "--port", "8000"]
