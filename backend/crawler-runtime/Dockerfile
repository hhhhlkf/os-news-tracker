# BASE_IMAGE must be a reviewed Python image pinned by sha256 digest.
# Example: python:3.12-slim-bookworm@sha256:<digest>
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG RUNTIME_VERSION
LABEL org.opencontainers.image.title="os-news crawler runtime" \
      org.opencontainers.image.version="${RUNTIME_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /runtime
COPY crawler-runtime/requirements.lock /runtime/requirements.lock
# Dependency and browser downloads happen only while building this reviewed
# image. Discovery uses headless Chromium only; Firefox, WebKit and full
# Chromium are deliberately not installed. A formal connector container never
# invokes pip or contacts a package/browser repository.
RUN python -m pip install --require-hashes --no-deps -r /runtime/requirements.lock \
    && python -m playwright install --with-deps --only-shell chromium \
    && python -m pip check \
    && python -m pip uninstall --yes pip setuptools wheel \
    && find /usr/local/bin -maxdepth 1 -type f \( -name 'pip' -o -name 'pip[0-9]*' \) -delete \
    && find /usr/bin -maxdepth 1 -type f \( -name 'pip' -o -name 'pip[0-9]*' \) -delete \
    && find /usr/local/lib -type d \( -name 'pip' -o -name 'pip-*' -o -name 'setuptools' -o -name 'setuptools-*' -o -name 'wheel' -o -name 'wheel-*' -o -name 'ensurepip' \) -prune -exec sh -c 'find "$1" -depth -delete' _ {} \; \
    && python -c "import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ('pip','setuptools','wheel','ensurepip'))" \
    && rm -rf /root/.cache /tmp/* /var/lib/apt/lists/*

COPY app/__init__.py /runtime/app/__init__.py
COPY app/discovery/plugin /runtime/app/discovery/plugin
COPY app/discovery/sandbox/egress_proxy.py /runtime/app/discovery/sandbox/egress_proxy.py
COPY app/discovery/sandbox/network_policy.py /runtime/app/discovery/sandbox/network_policy.py

RUN python -c "import app.discovery.plugin.runner, app.discovery.sandbox.egress_proxy"
RUN command -v dmesg >/dev/null
RUN test -x /ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell \
    && test -x /ms-playwright/ffmpeg-*/ffmpeg-linux \
    && test -z "$(find /ms-playwright -mindepth 1 -maxdepth 1 -type d \
        \( -name 'chromium-[0-9]*' -o -name 'firefox-*' -o -name 'webkit-*' \) \
        -print -quit)"
RUN mkdir -p /connectors && chmod 0555 /connectors

RUN find /runtime -type d -exec chmod 0555 {} + \
    && find /runtime -type f -exec chmod 0444 {} +

ENV PYTHONPATH=/runtime
USER 65532:65532
ENTRYPOINT []
CMD ["python", "-m", "app.discovery.plugin.runner"]
