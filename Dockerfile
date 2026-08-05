# Slim Python runtime. The Planner core is stdlib-only; only the FastAPI service layer needs
# packages (requirements.txt). Local model (Gemma via agent_server) — nothing bundled here.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY src/ ./src/
ENV PYTHONPATH=/app/src PLANNER_DATA_DIR=/app/data
# Run as a non-root user whose UID matches the host owner of data/ and the project repos, so
# generated plans on the bind mounts are not root-owned. Override at build time:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g "$APP_GID" app 2>/dev/null || true \
 && useradd -u "$APP_UID" -g "$APP_GID" -m -s /usr/sbin/nologin app 2>/dev/null || true \
 && mkdir -p /app/data && chown -R "$APP_UID:$APP_GID" /app
USER $APP_UID:$APP_GID
VOLUME ["/app/data"]
# HTTP service (mirrors the Architect): FACTORY triggers `planner:run` and polls `/jobs/{id}`.
# The batch CLI still works: docker compose run --rm planner-agent \
#   python3 scripts/produce_plan.py <PID>
CMD ["python3", "-m", "uvicorn", "planner.api:api", "--host", "0.0.0.0", "--port", "7805"]
