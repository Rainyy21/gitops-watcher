FROM python:3.12-alpine

RUN apk add --no-cache git docker-cli docker-cli-compose && git config --global --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app/watcher.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/watcher.py"]
