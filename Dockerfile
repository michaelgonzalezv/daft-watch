FROM python:3.12-slim

RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Runtime dependencies only — pytest and other dev deps are deliberately
# excluded from the image (see requirements.txt for the dev set).
RUN pip install --no-cache-dir playwright==1.62.0 PyYAML==6.0.2

# Install Chromium + its OS libraries. `playwright install` here downloads the
# browser into root's cache; the `cp` below hands it to the non-root `app` user
# (chowning /root's cache would not survive the USER switch cleanly).
RUN playwright install --with-deps chromium

COPY pyproject.toml .
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Make the Chromium build reachable by the unprivileged `app` user.
# world-readable so it works even when docker-compose overrides `user:` to an
# arbitrary host uid/gid.
RUN mkdir -p /home/app/.cache \
    && cp -r /root/.cache/ms-playwright /home/app/.cache/ \
    && chown -R app /home/app/.cache \
    && chmod -R a+rX /home/app/.cache

USER app
ENV PLAYWRIGHT_BROWSERS_PATH=/home/app/.cache/ms-playwright
VOLUME ["/data"]

HEALTHCHECK --interval=5m --timeout=10s --start-period=3m CMD python -c "import sys,time,os; p='/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 3600 else 1)"

ENTRYPOINT ["python", "-m", "daftwatch", "loop", "--config", "/data/config.yaml", "--db", "/data/daft.db", "--heartbeat", "/data/heartbeat"]
