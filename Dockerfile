FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full runtime needed by the production entrypoint.
COPY . ./

ENV PORT=8000
EXPOSE 8000

# compat_server preserves the existing six-tool MCP catalog while routing
# bounded reliability commands through collaborate to reliable_server's
# durable queue/Guardian runtime. Future refreshed clients still receive the
# native reliability tools from reliable_server itself.
CMD ["python", "compat_server.py"]
