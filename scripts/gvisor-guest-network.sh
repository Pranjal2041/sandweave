#!/bin/bash
set -euo pipefail
test ! -e /dev/kvm
ip -br addr
ip route
getent ahostsv4 example.com
curl --fail --max-time 30 -o /tmp/example.html https://example.com
grep -q 'Example Domain' /tmp/example.html
echo OUTBOUND_DNS_HTTPS_PASS
mkdir -p /tmp/network-test
printf 'GUEST_NETWORK_FORWARD_PASS\n' > /tmp/network-test/index.html
echo FORWARDED_HTTP_READY
exec python3 -m http.server --bind 0.0.0.0 --directory /tmp/network-test 8000
