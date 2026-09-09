FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Runtime dependencies only — pytest and other dev deps are deliberately
# excluded from the image (see requirements.txt for the dev set).
RUN pip install --no-cache-dir daftlistings==2.0.5 PyYAML==6.0.2

COPY pyproject.toml .
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

USER app
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python -c "import sys,time,os; p='/data/heartbeat'; \
  sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 3600 else 1)"

ENTRYPOINT ["python", "-m", "daftwatch", "loop", \
  "--config", "/data/config.yaml", "--db", "/data/daft.db", \
  "--heartbeat", "/data/heartbeat"]
