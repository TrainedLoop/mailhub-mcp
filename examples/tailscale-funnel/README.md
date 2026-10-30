# Example: Tailscale Funnel on a home server

One way to publish mailhub-mcp: a Linux host running Docker, with [Tailscale](https://tailscale.com) on the host and Funnel providing public HTTPS. This is an example setup, not a requirement; see [docs/tutorial.md](../../docs/tutorial.md) for the generic requirements.

The steps set up one instance, for one owner and their mailboxes. [More than one person](#more-than-one-person) at the end adds a second instance for someone else on the same host.

```
claude.ai --HTTPS--> Tailscale Funnel (:443) --> 127.0.0.1:8742 --> container --> Gmail API
```

Paths below assume the project lives in `/opt/mailhub-mcp` on the host. Change them if you install elsewhere (including the path in `firewall/mailhub-mcp-firewall.service`).

## 1. Enable HTTPS and Funnel

In the Tailscale admin console, enable HTTPS certificates for your tailnet and allow Funnel for the host in the tailnet policy (the `funnel` node attribute). Your public URL will be `https://<host>.<tailnet>.ts.net`.

Use it as `PUBLIC_BASE_URL` in `.env` and register `https://<host>.<tailnet>.ts.net/auth/callback` as the redirect URI of the Google Web client.

## 2. Why the port is loopback-only

The root `docker-compose.yml` publishes `127.0.0.1:8742:8742`. On hosts whose firewall INPUT policy accepts everything, a plain `8742:8742` would expose the unencrypted server to the whole LAN. Funnel runs on the host and forwards to loopback.

## 3. Start it with the fixed subnet

The firewall targets a known subnet, so use the override that creates the named network `mailhub_mcp` (172.31.0.0/24):

```bash
docker compose -f docker-compose.yml -f examples/tailscale-funnel/compose.override.yml up -d --build
```

If you change the subnet (`SUBNET` in `.env`), list it in `SUBNETS` for the firewall (step 5) as well.

## 4. Publish with Funnel

```bash
sudo tailscale funnel --bg 8742
tailscale funnel status
```

Funnel still serves publicly on port 443; 8742 is only the local target port. It serves the root, which the OAuth `/.well-known/*` endpoints need. `--bg` persists across reboots.

## 5. Isolate the container

The container is reachable from the internet, so it must not reach the host (SSH, a home automation server, other services), the LAN or the tailnet. The firewall script drops container traffic to the host and to private and tailnet ranges, and leaves outbound internet open (Gmail API, Google OAuth). It is idempotent.

```bash
sudo cp /opt/mailhub-mcp/examples/tailscale-funnel/firewall/mailhub-mcp-firewall.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailhub-mcp-firewall.service
```

Verify with three connections that must time out, then one that must succeed. Replace the placeholders: the host's tailnet IP, your LAN router's address, and the host-side address of the container network (`172.31.0.1`).

```bash
docker compose exec mailhub-mcp python -c "import socket; socket.create_connection(('172.31.0.1', 22), 3)"
docker compose exec mailhub-mcp python -c "import socket; socket.create_connection(('<host-tailnet-ip>', 22), 3)"
docker compose exec mailhub-mcp python -c "import socket; socket.create_connection(('<lan-router-ip>', 80), 3)"
docker compose exec mailhub-mcp python -c "import socket; socket.create_connection(('gmail.googleapis.com', 443), 3); print('ok')"
```

(Use any port that is open on the host for the first check; 22 assumes SSH is running.) The last command should print `ok`.

## 6. Deploying updates

From your workstation, this is safe to repeat: it never overwrites `secrets/`, `data/` or `.env` on the host.

```bash
ssh <host> 'sudo mkdir -p /opt/mailhub-mcp'
rsync -av --rsync-path='sudo rsync' --exclude .venv --exclude .git \
  --exclude client_secret_desktop.json \
  --exclude /secrets/ --exclude /data/ --exclude /.env \
  ./ <host>:/opt/mailhub-mcp/
```

`--rsync-path='sudo rsync'` needs passwordless sudo for rsync on the host. On first install, copy the account credentials separately (see the tutorial's "Add an account") and create `.env` there. Then run the compose command from step 3 on the host.

## More than one person

Every login in `ALLOWED_EMAILS` sees every linked mailbox, so a second person gets a second instance, not a second address in the first one's `.env`. On the same host:

1. Install a second copy, for example in `/opt/mailhub-mcp-2`, with its own `.env`, `secrets/accounts/` (their mailboxes) and `data/` (tutorial steps 3 to 6). Never share `secrets/` or `data/` between instances.
2. In its `.env`, besides their own `ALLOWED_EMAILS` and `JWT_SIGNING_KEY`, set a distinct local port, container, Compose project and network:

   ```
   HOST_PORT=8743
   CONTAINER_NAME=mailhub-mcp-2
   COMPOSE_PROJECT_NAME=mailhub-mcp-2
   NETWORK_NAME=mailhub_mcp_2
   SUBNET=172.31.1.0/24
   ```

   Each instance gets its own Docker network: never put two instances on one network. The firewall drops traffic from either subnet to all private ranges, which include the other instance's subnet, so the two containers cannot reach each other.
3. Funnel serves HTTPS only on ports 443, 8443 and 10000. The first instance uses 443, so serve the second on Funnel's port 8443, forwarding to its local port:

   ```bash
   sudo tailscale funnel --bg --https=8443 8743
   tailscale funnel status
   ```

   Its public URL is `https://<host>.<tailnet>.ts.net:8443`. Set `PUBLIC_BASE_URL=https://<host>.<tailnet>.ts.net:8443` in the second `.env`, and add `https://<host>.<tailnet>.ts.net:8443/auth/callback` as an authorized redirect URI of the Google Web client (one Web client can list both instances' redirect URIs).
4. Start it from its own directory with the same compose command as step 3.
5. List both subnets for the firewall: edit the installed unit with `sudo systemctl edit --full mailhub-mcp-firewall.service`, add `Environment="SUBNETS=172.31.0.0/24 172.31.1.0/24"` under `[Service]`, then `sudo systemctl restart mailhub-mcp-firewall.service`. Repeat the checks of step 5 from the second directory, using `172.31.1.1` as the host-side address, and check that the first container cannot reach the second (`docker compose exec mailhub-mcp python -c "import socket; socket.create_connection(('172.31.1.2', 8742), 3)"` from the first directory must time out; check the second container's address with `docker inspect mailhub-mcp-2`).
6. The second person adds their own custom connector in claude.ai with `https://<host>.<tailnet>.ts.net:8443/mcp` and logs in with their own Google account.

When deploying updates (step 6), rsync to each instance's directory; `.env`, `secrets/` and `data/` are excluded, so each keeps its own settings.

## Notes

- If the host reboots on a schedule, the container comes back by itself (`unless-stopped`), and the OAuth state in `./data` keeps the connector connected. Avoid scheduling routines at that time.
- To shut everything down: `docker compose down` in each instance's directory, `sudo tailscale funnel reset`, and revoke the app in your Google accounts.
