ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN python -m venv /opt/venv && \
    pip install --no-cache-dir uv && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/profile.d/00-venv.sh && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/bash.bashrc

WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

WORKDIR /workspace
