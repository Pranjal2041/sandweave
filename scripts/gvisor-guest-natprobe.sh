#!/bin/bash
set -euo pipefail
mkdir -p /tmp/server-a /tmp/server-b
printf 'SERVICE_A\n' > /tmp/server-a/index.html
printf 'SERVICE_B\n' > /tmp/server-b/index.html
python3 -m http.server --bind 0.0.0.0 --directory /tmp/server-a 8001 >/tmp/server-a.log 2>&1 &
server_a=$!
python3 -m http.server --bind 0.0.0.0 --directory /tmp/server-b 8002 >/tmp/server-b.log 2>&1 &
server_b=$!
trap 'kill "$server_a" "$server_b" 2>/dev/null || true' EXIT
if [[ ${1:-nft} == iptables ]]; then
    iptables -t nat -N GVM
    iptables -t nat -A PREROUTING -m addrtype --dst-type LOCAL -j GVM
    iptables -t nat -A GVM ! -i docker0 -p tcp --dport 8000 -j DNAT --to-destination 10.0.2.15:8001
    iptables -t nat -A GVM ! -i docker0 -p tcp --dport 8080 -j DNAT --to-destination 10.0.2.15:8002
else
    nft add table ip probe
    nft 'add chain ip probe ingress { type nat hook prerouting priority -100; policy accept; }'
    nft 'add rule ip probe ingress tcp dport 8000 counter dnat to 10.0.2.15:8001'
    nft 'add rule ip probe ingress tcp dport 8080 counter dnat to 10.0.2.15:8002'
fi
echo NAT_PROBE_READY
wait
