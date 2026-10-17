FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app
COPY gateway ./gateway
COPY web ./web
USER app
# Loopback only. A future reverse proxy must share the network namespace or
# an explicitly reviewed bind change must accompany the deployment configuration.
CMD ["python", "-m", "gateway.server"]
