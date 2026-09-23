FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=8000
EXPOSE 8000

# Optional FS08 cutover diagnostic is read-only and defaults OFF.
# Migration remains separately identity-bound and defaults OFF.
CMD ["sh", "-c", "python five_seat_active_job_diagnostic.py && python five_seat_production_migrate.py && python jaytec_read_startup_selftest.py && exec python notion_courier_server.py"]
