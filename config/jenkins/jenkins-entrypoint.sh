#!/bin/sh
set -eu

secrets_dir="${JENKINS_SECRETS_DIR:-/run/jenkins-secrets}"
overlay_dir="${CASC_OVERLAY_DIR:-/run/casc.d}"
core_file="${CASC_CORE_FILE:-/usr/share/jenkins/ref/casc.yaml}"
realm_file="${CASC_LOCAL_REALM_FILE:-/usr/share/jenkins/ref/casc-local-realm.yaml}"
launcher="${JENKINS_LAUNCHER:-/usr/local/bin/jenkins.sh}"
validator="$(dirname "$0")/validate-origin.sh"
[ -x /usr/local/bin/devops-lab-validate-origin ] && validator=/usr/local/bin/devops-lab-validate-origin

sh "${validator}" "${JENKINS_URL:-}" "${JENKINS_HOST:-}" JENKINS_URL

for file in "${secrets_dir}"/*; do
  [ -f "${file}" ] || continue
  name="$(basename "${file}" | tr 'a-z-' 'A-Z_')"
  value="$(cat "${file}")"
  export "${name}=${value}"
done

CASC_JENKINS_CONFIG="${core_file}"
# Configuration as Code merges every source into one document, so two files that each
# name a security realm produce a realm with two entries and startup fails. The default
# local realm therefore only joins the sources when no overlay declares one.
overlay_realm=no
for candidate in "${overlay_dir}"/*.yaml "${overlay_dir}"/*.yml; do
  if [ -f "${candidate}" ] && grep -q '^[[:space:]]*securityRealm:' "${candidate}"; then
    overlay_realm=yes
    break
  fi
done
if [ "${overlay_realm}" = no ]; then
  CASC_JENKINS_CONFIG="${CASC_JENKINS_CONFIG},${realm_file}"
fi
for candidate in "${overlay_dir}"/*.yaml "${overlay_dir}"/*.yml; do
  if [ -f "${candidate}" ]; then
    CASC_JENKINS_CONFIG="${CASC_JENKINS_CONFIG},${overlay_dir}"
    break
  fi
done
export CASC_JENKINS_CONFIG
export CASC_MERGE_STRATEGY=override

exec "${launcher}" "$@"
