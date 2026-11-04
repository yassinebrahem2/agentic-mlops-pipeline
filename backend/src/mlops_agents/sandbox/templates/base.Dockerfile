ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN python -m venv /opt/venv && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/profile.d/00-venv.sh && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/bash.bashrc

# @duckdb

WORKDIR /workspace
