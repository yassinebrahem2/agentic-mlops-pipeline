ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN python -m venv /opt/venv && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/profile.d/00-venv.sh && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/bash.bashrc

COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import subprocess, sys; toml = __import__('tomllib' if sys.version_info >= (3, 11) else 'pip._vendor.tomli', fromlist=['load']); data = toml.load(open('/tmp/pyproject.toml', 'rb')); deps = data.get('project', {}).get('dependencies', []); subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *deps]) if deps else None"

# @duckdb

WORKDIR /workspace
