FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full runtime needed by the production entrypoint.
COPY . ./

ENV PORT=8000
EXPOSE 8000

# compat_server wraps the unchanged reliable runtime at the HTTP boundary so
# stale cached clients can reach native reliability tools through collaborate
# without mutating the legacy router or tool registry.
CMD ["python", "compat_server.py"]
