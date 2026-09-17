FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full runtime needed by the production entrypoint.
COPY . ./

ENV PORT=8000
EXPOSE 8000

# reliable_server preserves the legacy MCP surface while adding durable,
# pollable specialist execution, Guardian Lite background health checks, and
# bounded compatibility timeouts.
CMD ["python", "reliable_server.py"]
