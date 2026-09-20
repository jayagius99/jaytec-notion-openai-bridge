FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=8000
EXPOSE 8000

# Production Notion-facing endpoint is a courier only. All direct native tool
# calls and free-form collaboration are rejected at the HTTP boundary; only
# exact JAYTEC-authored status/task-packet pass-through commands survive.
# This Dockerfile change intentionally pins CI to the complete courier stress-test head.
CMD ["sh", "-c", "python jaytec_read_startup_selftest.py && exec python notion_courier_server.py"]
