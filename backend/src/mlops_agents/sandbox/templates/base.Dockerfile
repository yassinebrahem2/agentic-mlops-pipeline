ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN python -m venv /opt/venv

WORKDIR /workspace
