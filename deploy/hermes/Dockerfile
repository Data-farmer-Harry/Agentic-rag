ARG HERMES_BASE_IMAGE=nikolaik/python-nodejs:python3.13-nodejs20-slim@sha256:ef37897d6366a5e510782b6f708106c5e4e447975036d898295542647c155d6b
FROM ${HERMES_BASE_IMAGE}

ARG HERMES_AGENT_VERSION=0.19.0
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV HERMES_HOME=/data/hermes \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --system hermes \
    && useradd --system --gid hermes --home-dir /data/hermes hermes \
    && mkdir -p /data/hermes /opt/hermesgraph/plugin \
    && chown -R hermes:hermes /data/hermes /opt/hermesgraph

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" \
    "hermes-agent==${HERMES_AGENT_VERSION}" \
    "aiohttp==3.14.1"

COPY deploy/hermes/patch_error_classifier.py /opt/hermesgraph/patch_error_classifier.py
RUN python /opt/hermesgraph/patch_error_classifier.py

COPY --chown=hermes:hermes deploy/hermes/bootstrap.py /opt/hermesgraph/bootstrap.py
COPY --chown=hermes:hermes deploy/hermes/config.yaml /opt/hermesgraph/config.yaml
COPY --chown=hermes:hermes deploy/hermes/plugin/ /opt/hermesgraph/plugin/
COPY --chown=hermes:hermes deploy/hermes/entrypoint.sh /usr/local/bin/hermesgraph-entrypoint
RUN chmod 0555 /usr/local/bin/hermesgraph-entrypoint

USER hermes
WORKDIR /data/hermes

EXPOSE 8642 8643

ENTRYPOINT ["hermesgraph-entrypoint"]
CMD ["hermes", "gateway", "run", "--no-supervise"]
