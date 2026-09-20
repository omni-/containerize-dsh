# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS node
FROM ubuntu:24.04
COPY --from=node /usr/local/ /usr/local/
ARG DSH_VERSION=0.1.5-rc.1
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl wget git python3 ripgrep socat libstdc++6 libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g --omit=dev --no-audit --no-fund \
       --allow-scripts=@deepseek-ai/dsh-subprocess-local,koffi,node-pty,@google/genai,protobufjs \
       "@deepseek-ai/dsh@${DSH_VERSION}" \
    && dsh --version
COPY scripts/patch-dsh.py /opt/dsh/patch-dsh.py
RUN python3 /opt/dsh/patch-dsh.py
RUN useradd --create-home --uid 10001 --shell /bin/bash agent \
    && mkdir -p /workspace /opt/dsh \
    && chown agent:agent /workspace
COPY scripts/start-dsh.sh /opt/dsh/start-dsh.sh
COPY scripts/git_transfer.py scripts/workspace.py /opt/dsh/
RUN chmod 0555 /opt/dsh/start-dsh.sh
ENV HOME=/home/agent DSH_HOME=/home/agent/.dsh DSH_PERMISSION_MODE=danger-full-access
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["/opt/dsh/start-dsh.sh"]
