FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=8000
EXPOSE 8000

# FS08 cutover helpers are individually gated and default OFF.
# Order: read-only visibility -> exact orphan reconciliation -> migration.
CMD ["sh", "-c", "python five_seat_active_job_diagnostic.py && python five_seat_orphaned_job_reconcile.py && python five_seat_production_migrate.py && python jaytec_read_startup_selftest.py && exec python notion_courier_server.py"]
