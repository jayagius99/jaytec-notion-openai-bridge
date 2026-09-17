FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full runtime needed by the production entrypoint.
COPY . ./

ENV PORT=8000
EXPOSE 8000

# notion_compat_server preserves the original six-tool Notion MCP surface,
# adds backward-compatible durable reliability commands through collaborate,
# and still exposes the native reliable_server tools for refreshed clients.
CMD ["python", "notion_compat_server.py"]
