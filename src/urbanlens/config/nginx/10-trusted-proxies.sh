#!/bin/sh
# Writes the set_real_ip_from list the vhosts include. Runs from the image's
# /docker-entrypoint.d, before nginx starts. UL_TRUSTED_PROXY_CIDRS is a space-
# or comma-separated list of addresses or CIDRs; unset or blank keeps Docker's
# default network pools.
set -eu

DEFAULT_TRUSTED_PROXY_CIDRS="172.16.0.0/12 192.168.0.0/16"
OUT=/etc/nginx/conf.d/trusted_proxies.inc

cidrs=$(printf '%s' "${UL_TRUSTED_PROXY_CIDRS:-}" | tr ',' ' ')
[ -n "$(printf '%s' "$cidrs" | tr -d ' \t\n')" ] || cidrs=$DEFAULT_TRUSTED_PROXY_CIDRS

: > "$OUT"
for cidr in $cidrs; do
    # The value is written into nginx config, so anything but an address or CIDR is refused.
    case "$cidr" in
        *[!0-9a-fA-F:./]*|"")
            echo "10-trusted-proxies: refusing UL_TRUSTED_PROXY_CIDRS entry '$cidr'" >&2
            exit 1
            ;;
    esac
    echo "set_real_ip_from $cidr;" >> "$OUT"
done
echo "10-trusted-proxies: trusting X-Forwarded-For from $cidrs"
