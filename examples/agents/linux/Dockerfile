FROM eclipse-temurin:21-jre@sha256:49e21e16e3c86eb7816a44a67549910ed090fbeb40c29c525d58bf5e02e91b0f

ARG SCANNER_VERSION=7.2.0.5079
ARG SCANNER_SHA256=da9f4e64a3d555f08ce38b5469ebd91fe2b311af473f7001a5ee5c1fd58b004b

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends \
       ca-certificates curl git python3 unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl --fail --silent --show-error --location \
      --output /tmp/sonar-scanner.zip \
      "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SCANNER_VERSION}-linux-x64.zip" \
    && echo "${SCANNER_SHA256}  /tmp/sonar-scanner.zip" | sha256sum -c - \
    && unzip -q /tmp/sonar-scanner.zip -d /opt \
    && mv "/opt/sonar-scanner-${SCANNER_VERSION}-linux-x64" /opt/sonar-scanner \
    && ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner \
    && rm /tmp/sonar-scanner.zip

RUN mkdir -p /home/agent && chmod 0777 /home/agent
ENV HOME=/home/agent
COPY entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod 0755 /usr/local/bin/agent-entrypoint
ENTRYPOINT ["/usr/local/bin/agent-entrypoint"]
