ARG PYTHON_VERSION=3.12
FROM mambaorg/micromamba:latest as micromamba
FROM python:${PYTHON_VERSION}-slim

COPY --from=micromamba /bin/micromamba /bin/micromamba

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/workspace/src:/workspace"

COPY environment.y* /tmp/
RUN micromamba create -y -p /opt/venv -f /tmp/environment.y* && micromamba clean --all --yes

WORKDIR /workspace
