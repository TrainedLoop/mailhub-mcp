# Example: Tailscale Funnel on a home server

One way to publish mailhub-mcp: a Linux host running Docker, with [Tailscale](https://tailscale.com) on the host and Funnel providing public HTTPS. This is an example setup, not a requirement; see [docs/tutorial.md](../../docs/tutorial.md) for the generic requirements.

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

If you change the subnet, change `SUBNET` in `firewall/mailhub-mcp-firewall.sh` to match.

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

## Notes

- If the host reboots on a schedule, the container comes back by itself (`unless-stopped`), and the OAuth state in `./data` keeps the connector connected. Avoid scheduling routines at that time.
- To shut everything down: `docker compose down`, `sudo tailscale funnel reset`, and revoke the app in your Google accounts.
