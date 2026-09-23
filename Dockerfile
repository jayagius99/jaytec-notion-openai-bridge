FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

ENV PORT=8000
EXPOSE 8000

# Optional FS08 cutover diagnostics/reconciliation default OFF; image smoke covers adapter compatibility.
# Migration remains separately identity-bound and defaults OFF.
CMD ["sh", "-c", "python five_seat_active_job_diagnostic.py && python five_seat_scheduler_diagnostic.py && python five_seat_quarantine_evidence.py && python five_seat_v2_quarantine_evidence.py && python five_seat_v3_quarantine_evidence.py && python five_seat_v1_quarantine_reconcile.py && python five_seat_v2_quarantine_reconcile.py && python five_seat_v2_expired_reconcile.py && python five_seat_legacy_job_reconcile.py && python five_seat_production_migrate.py && python five_seat_authority_bootstrap.py && python jaytec_read_startup_selftest.py && exec python notion_courier_server.py"]
