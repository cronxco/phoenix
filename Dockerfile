FROM python:3.12-slim

# Install Tailscale + openssh-client
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    openssh-client \
    iproute2 \
    && curl -fsSL https://tailscale.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

RUN chmod +x scripts/entrypoint.sh

# Secrets mounted at runtime — not baked into image
VOLUME ["/secrets", "/var/lib/tailscale"]

EXPOSE 8000

ENTRYPOINT ["scripts/entrypoint.sh"]
