#!/bin/sh
# Inbound WebSocket agent, running as the deploying user. The controller connection
# trusts only the platform CA; build steps get the system roots plus the platform CA.
set -eu
: "${JENKINS_URL:?JENKINS_URL is required}"
: "${AGENT_NAME:?AGENT_NAME is required}"

work="${HOME}/work"
mkdir -p "${work}"
# SonarScanner 7 builds its own SSL context and ignores javax.net.ssl.*; it reads this
# keystore, under SONAR_USER_HOME, with the JDK default password.
export SONAR_USER_HOME="${HOME}/.sonar"
mkdir -p "${SONAR_USER_HOME}/ssl"
rm -f "${SONAR_USER_HOME}/ssl/truststore.p12"
# keytool reports failures on stdout, so nothing here is redirected away.
keytool -importcert -noprompt -alias devops-lab-ca -file /run/tls/ca.crt \
  -keystore "${SONAR_USER_HOME}/ssl/truststore.p12" -storepass changeit
# git and curl read the system bundle, which a non-root container user cannot extend
# in place; build a private copy with the platform CA appended so build steps can
# clone over HTTPS without losing the public roots.
bundle="${HOME}/ca-bundle.crt"
cat /etc/ssl/certs/ca-certificates.crt /run/tls/ca.crt > "${bundle}"
export GIT_SSL_CAINFO="${bundle}"
export CURL_CA_BUNDLE="${bundle}"

curl --fail --silent --show-error --noproxy '*' --cacert /run/tls/ca.crt \
  --output "${HOME}/agent.jar" "${JENKINS_URL}/jnlpJars/agent.jar"

# args4j expands a leading @ into one argument per file line, so the PEM is passed
# inline; only the single-line secret file survives that expansion intact.
exec java -jar "${HOME}/agent.jar" \
  -url "${JENKINS_URL}/" -name "${AGENT_NAME}" -secret @/run/agent/secret \
  -webSocket -workDir "${work}" -cert "$(cat /run/tls/ca.crt)"
