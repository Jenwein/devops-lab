#!/bin/sh
set -eu

url="${1:-}"
canonical_host="${2:-}"
name="${3:-service URL}"

if [ -z "${url}" ] || [ -z "${canonical_host}" ]; then
  echo "refusing non-local, malformed, or missing ${name}" >&2
  exit 1
fi

case "${url}" in
  "https://${canonical_host}")
    exit 0
    ;;
  "https://${canonical_host}:"*)
    port="${url#"https://${canonical_host}:"}"
    case "${port}" in
      ''|*[!0-9]*) ;;
      *)
        if [ "${port}" -ge 1 ] 2>/dev/null && [ "${port}" -le 65535 ] 2>/dev/null; then
          exit 0
        fi
        ;;
    esac
    ;;
esac

echo "refusing non-local, malformed, or missing ${name}" >&2
exit 1
