FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

WORKDIR /app

COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

# Copy the full runtime needed by the production entrypoint.
# (Previously only server.py + PROJECT_CONTEXT.md were copied, which would break
# unified orchestration and idempotency modules after merge.)
COPY . ./

ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
