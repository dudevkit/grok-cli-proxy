FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY static/ static/
COPY config.example.json .

RUN mkdir -p data

ENV PYTHONUNBUFFERED=1

EXPOSE 8787

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--log-level", "warning"]
