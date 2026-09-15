FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full runtime needed by the production entrypoint.
# (Previously only server.py + PROJECT_CONTEXT.md were copied, which would break
# unified orchestration and idempotency modules after merge.)
COPY . ./

ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
