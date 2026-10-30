#!/bin/sh
# Keeps the internet-facing mailhub-mcp container away from everything local:
# the host itself (services on the host, e.g. SSH or a home automation server),
# the LAN and the tailnet. Only outbound internet (Gmail API, Google OAuth)
# stays open. Idempotent.
#
# SUBNETS (space-separated) must list the subnet of every instance's network
# (SUBNET in each instance's .env, default 172.31.0.0/24 in
# ../compose.override.yml). With more than one instance, set it in the systemd
# unit, e.g. Environment="SUBNETS=172.31.0.0/24 172.31.1.0/24". The private
# ranges below include those subnets, so instances cannot reach each other.
set -eu

SUBNETS=${SUBNETS:-172.31.0.0/24}

add() {
    chain=$1; shift
    iptables -C "$chain" "$@" 2>/dev/null || iptables -I "$chain" "$@"
}

for subnet in $SUBNETS; do
    # Container -> host. Replies on connections the host opened (Funnel ->
    # docker-proxy -> container) must still pass, so allow ESTABLISHED first.
    # -I inserts at the top, so add the DROP before the ACCEPT.
    add INPUT -s "$subnet" -j DROP
    add INPUT -s "$subnet" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Container -> LAN, other private ranges (other instances included) and the
    # tailnet (100.64.0.0/10).
    for dst in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16; do
        add DOCKER-USER -s "$subnet" -d "$dst" -m conntrack --ctstate NEW -j DROP
    done
done
