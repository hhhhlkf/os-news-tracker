# The OpenHands agent image is deliberately separate from crawler-runtime.
# BASE_IMAGE must be a reviewed Python 3.12 image pinned by sha256 digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG RUNTIME_VERSION=openhands-agent-runtime:1.43.1-r2
LABEL org.opencontainers.image.title="os-news OpenHands discovery agent runtime" \
      org.opencontainers.image.version="${RUNTIME_VERSION}" \
      org.opencontainers.image.openhands-sdk="1.43.1" \
      org.opencontainers.image.python-lock="/runtime/requirements.lock" \
      org.opencontainers.image.python-sbom="/runtime/discovery-agent-installed.txt"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /runtime
COPY discovery-agent-runtime/requirements.txt /runtime/requirements.txt
COPY discovery-agent-runtime/requirements.lock /runtime/requirements.lock
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates tmux \
    && rm -rf /var/lib/apt/lists/*
RUN python -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" \
    && python -m pip install --require-hashes -r /runtime/requirements.lock \
    && python -m pip check \
    && python -m pip freeze --all | grep -Ev '^(pip|setuptools|wheel)==' | LC_ALL=C sort > /runtime/discovery-agent-installed.txt \
    && sha256sum /runtime/requirements.lock > /runtime/requirements.lock.sha256 \
    && python -m pip uninstall --yes pip setuptools wheel \
    && find /usr/local/bin /usr/bin -maxdepth 1 -type f \( -name 'pip' -o -name 'pip[0-9]*' \) -delete \
    && find /usr/local/lib -type d \( -name 'pip' -o -name 'pip-*' -o -name 'setuptools' -o -name 'setuptools-*' -o -name 'wheel' -o -name 'wheel-*' -o -name 'ensurepip' \) -prune -exec sh -c 'find "$1" -depth -delete' _ {} \; \
    && rm -rf /root/.cache /tmp/* /var/lib/apt/lists/*

# The trusted egress proxy runs in a separate ordinary infrastructure
# container.  The same immutable image provides its reviewed policy module,
# while the untrusted OpenHands process always runs with --runtime=runsc.
COPY app/__init__.py /runtime/app/__init__.py
COPY app/discovery/sandbox/egress_proxy.py /runtime/app/discovery/sandbox/egress_proxy.py
COPY app/discovery/sandbox/network_policy.py /runtime/app/discovery/sandbox/network_policy.py
COPY discovery-agent-runtime/agent_runtime.py /runtime/agent_runtime.py
COPY discovery-agent-runtime/trusted_proxy.py /runtime/trusted_proxy.py

RUN python -m py_compile /runtime/agent_runtime.py /runtime/trusted_proxy.py \
    && python -c "import openhands.sdk, openhands.tools, openhands.agent_server; import app.discovery.sandbox.egress_proxy"
RUN command -v dmesg >/dev/null
RUN mkdir -p /workspace /home/openhands \
    && chmod 0770 /workspace \
    && chown 65532:65532 /home/openhands \
    && chmod 0700 /home/openhands

ENV PYTHONPATH=/runtime \
    HOME=/home/openhands \
    XDG_CACHE_HOME=/home/openhands/.cache \
    XDG_CONFIG_HOME=/home/openhands/.config \
    XDG_DATA_HOME=/home/openhands/.local/share \
    XDG_RUNTIME_DIR=/home/openhands/.runtime
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-c", "raise SystemExit('host protocol program is required')"]
