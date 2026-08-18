FROM ubuntu:24.04

# Base system packages (common to all languages)
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    patch \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Add deadsnakes PPA for Python 3.10 (not available in Ubuntu 24.04 by default)
RUN apt-get update && apt-get install -y software-properties-common && \
    add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && apt-get install -y \
    python3.10 \
    python3.10-dev \
    python3.10-venv \
    python3.10-distutils \
    python3-opencv \
    tesseract-ocr \
    tesseract-ocr-jpn \
    tesseract-ocr-eng \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

# Install pip for Python 3.10
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.10

# Install Poetry 2.*
RUN python3.10 -m pip install "poetry==2.*"

WORKDIR /app

# Clone repo at HEAD commit (with fix applied)
RUN git clone https://github.com/electricitymaps/electricitymaps-contrib.git src && \
    cd src && \
    (git fetch --depth 1 origin d085f8121004c91d8f5ab19df0ad9752d5a205fe || git fetch --depth 1 origin "+refs/pull/8113/head:refs/remotes/origin/pr/8113") && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive

WORKDIR /app/src

# Install dependencies using Poetry with a virtual environment
ENV POETRY_NO_INTERACTION=1 \
    PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
    POETRY_VIRTUALENVS_IN_PROJECT=true
RUN python3.10 -m poetry install -E parsers
ENV PATH="/app/src/.venv/bin:${PATH}"

# If install/build steps touched tracked files, reset them so bug.patch applies cleanly
RUN git reset --hard

# Apply bug.patch to revert to buggy state (BASE)
COPY bug.patch /tmp/bug.patch
RUN patch -p1 < /tmp/bug.patch && rm /tmp/bug.patch

RUN rm -rf /app/src/.git

WORKDIR /app/src
