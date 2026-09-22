ARG EDGE_IMAGE=nginx:1.30.5-alpine
FROM ${EDGE_IMAGE}

RUN rm /etc/nginx/conf.d/default.conf \
      /docker-entrypoint.d/10-listen-on-ipv6-by-default.sh
