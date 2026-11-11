FROM python:3.12-alpine

RUN apk add --no-cache git docker-cli docker-cli-compose && git config --global --add safe.directory '*'

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# watcher.py + cli.py, and web/ for the gitops-web service (same image)
COPY app/ ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "/app/watcher.py"]
