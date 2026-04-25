FROM node:20-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        docker.io \
        git \
        jq \
        python3 \
        python3-pip \
        python3-venv \
        util-linux \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

ENV PATH="/opt/venv/bin:${PATH}"

RUN npm install -g @openai/codex@latest

COPY app.py /app/app.py
COPY entrypoint.sh /app/entrypoint.sh
COPY host-bash.sh /app/host-bash.sh
RUN chmod +x /app/entrypoint.sh /app/host-bash.sh \
    && mv /bin/bash /bin/bash.container \
    && cp /app/host-bash.sh /bin/bash \
    && chmod 755 /bin/bash

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/app/entrypoint.sh"]
