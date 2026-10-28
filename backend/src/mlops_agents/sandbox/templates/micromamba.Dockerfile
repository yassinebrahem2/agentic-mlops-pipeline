ARG PYTHON_VERSION=3.12
FROM mambaorg/micromamba:1.5.10 as micromamba
FROM python:${PYTHON_VERSION}-slim

COPY --from=micromamba /bin/micromamba /bin/micromamba

ENV MAMBA_ROOT_PREFIX="/opt/conda" \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV="/opt/venv" \
    UV_PROJECT_ENVIRONMENT="/opt/venv" \
    PYTHONPATH="/workspace/src:/workspace"

RUN echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/profile.d/00-venv.sh && \
    echo 'export PATH="/opt/venv/bin:$PATH"' >> /etc/bash.bashrc

COPY environment.y* /tmp/
RUN micromamba create -y -p /opt/venv -f /tmp/environment.y* && micromamba clean --all --yes

# @duckdb

WORKDIR /workspace
