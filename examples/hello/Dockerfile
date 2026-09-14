ARG DSH_BASE_IMAGE=dsh-sandbox:local
FROM ${DSH_BASE_IMAGE}
USER root
RUN printf 'Generic derived image\n' > /opt/example-image.txt
USER 10001:10001
