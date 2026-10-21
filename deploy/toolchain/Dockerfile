FROM public.ecr.aws/docker/library/python@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ARG PIP_INDEX_URL=https://mirrors.huaweicloud.com/repository/pypi/simple
COPY requirements.txt /tmp/yipilot-requirements.txt
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.huaweicloud.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/yipilot-requirements.txt \
    && python -m pip check \
    && rm /tmp/yipilot-requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MYPY_CACHE_DIR=/tmp/yipilot-mypy \
    RUFF_CACHE_DIR=/tmp/yipilot-ruff
LABEL org.yipilot.toolchain="python-tools-v1"
WORKDIR /workspace
