ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN python -m venv /opt/venv

COPY pyproject.toml /tmp/pyproject.toml
RUN python -c '\
try:\
    import tomllib\
except ImportError:\
    import pip._vendor.tomli as tomllib\
import subprocess, sys\
with open("/tmp/pyproject.toml", "rb") as f:\
    data = tomllib.load(f)\
deps = data.get("project", {}).get("dependencies", [])\
if deps:\
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-cache-dir", *deps])\
'

WORKDIR /workspace
