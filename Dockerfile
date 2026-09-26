# Server-side conversions server. Provide config and secrets at runtime:
#   docker run -p 8080:8080 -v $PWD/tracking.yaml:/app/tracking.yaml \
#     -v capi-data:/data -e SHOPIFY_WEBHOOK_SECRET=... -e META_CAPI_ACCESS_TOKEN=... tracking-agent
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tracking_agent ./tracking_agent
RUN pip install --no-cache-dir . gunicorn
ENV TRACKING_CONFIG=/app/tracking.yaml
VOLUME /data
EXPOSE 8080
# One process: the send queue runs as a thread inside it. Point
# capi.database at /data/capi.sqlite3 so the queue survives restarts.
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8080", "tracking_agent.capi.server:create_app()"]
