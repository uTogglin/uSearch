# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Combined builder + dist image for deploying uSearch (a SearXNG fork) to
# Fly.io with a single `fly deploy`. It merges container/builder.dockerfile
# and container/dist.dockerfile into one multi-stage build so there is no
# dependency on the locally-built `localhost/...:builder` image that the
# repo's `manage` script produces.
#
# Served by Granian (WSGI) on port 8080.

# ---------------------------------------------------------------------------
# Stage 1 — builder: create the venv and byte-compile the app
# ---------------------------------------------------------------------------
FROM ghcr.io/searxng/base:searxng-builder AS builder

COPY ./requirements.txt ./requirements-server.txt ./

ENV UV_NO_MANAGED_PYTHON="true"
ENV UV_NATIVE_TLS="true"

RUN --mount=type=cache,id=uv,target=/root/.cache/uv set -eux -o pipefail; \
    uv venv; \
    uv pip install --requirements ./requirements.txt --requirements ./requirements-server.txt; \
    uv cache prune --ci

# version_frozen.py is generated before the build (searx/version.py freeze) and
# travels in with the rest of the source.
COPY ./searx/ ./searx/

RUN set -eux -o pipefail; \
    python -m compileall -q -f -j 0 --invalidation-mode=unchecked-hash ./searx/

# ---------------------------------------------------------------------------
# Stage 2 — dist: minimal runtime image
# ---------------------------------------------------------------------------
FROM ghcr.io/searxng/base:searxng AS dist

COPY --chown=977:977 --from=builder /usr/local/searxng/.venv/ ./.venv/
COPY --chown=977:977 --from=builder /usr/local/searxng/searx/ ./searx/
COPY --chown=977:977 ./container/ ./

# Entry-point tweaks for a fast scale-to-zero cold start:
#  1. Normalise CRLF -> LF. The repo is often checked out on Windows where the
#     script picks up CRLF, making the kernel look for "/bin/sh\r" and the
#     container crash-loops with ENOENT.
#  2. Drop the per-boot `update-ca-certificates` run. The base image already
#     ships a valid CA bundle and we add no custom certs, so it reports
#     "0 added, 0 removed" while costing ~2s on every cold start. Replace the
#     call with a no-op (`true`) so the surrounding `if` stays valid sh.
RUN sed -i 's/\r$//' /usr/local/searxng/entrypoint.sh \
    && sed -i 's/update-ca-certificates/true/' /usr/local/searxng/entrypoint.sh \
    && chmod +x /usr/local/searxng/entrypoint.sh

ENV __SEARXNG_VERSION="fly" \
    __SEARXNG_SETTINGS_PATH="$__SEARXNG_CONFIG_PATH/settings.yml" \
    GRANIAN_PROCESS_NAME="searxng" \
    GRANIAN_INTERFACE="wsgi" \
    GRANIAN_HOST="::" \
    GRANIAN_PORT="8080" \
    GRANIAN_WEBSOCKETS="false" \
    GRANIAN_BLOCKING_THREADS="2" \
    GRANIAN_WORKERS_KILL_TIMEOUT="30s" \
    GRANIAN_BLOCKING_THREADS_IDLE_TIMEOUT="5m"

# "*_PATH" ENVs are defined in the base image
VOLUME $__SEARXNG_CONFIG_PATH
VOLUME $__SEARXNG_DATA_PATH

EXPOSE 8080

ENTRYPOINT ["/usr/local/searxng/entrypoint.sh"]
