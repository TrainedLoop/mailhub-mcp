# Example: Tailscale sidecar

Another way to publish mailhub-mcp with [Tailscale](https://tailscale.com) Funnel. It is an alternative to [../tailscale-funnel/](../tailscale-funnel/). Here a `tailscale/tailscale` container joins the tailnet as its own machine and proxies to mailhub-mcp over their Docker network. The two setups compare like this:

- **Hostname and port:** each instance gets its own hostname (`https://mailhub.<tailnet>.ts.net`) on port 443. Funnel only serves ports 443, 8443 and 10000 per machine, so with one Funnel on the host, every extra service has to take one of the remaining ports.
- **Host exposure:** no port is published on the host, not even on loopback.
- **Host requirements:** the host does not need Tailscale installed or Funnel enabled.

```
claude.ai --HTTPS--> Funnel on the sidecar (:443) --> mailhub-mcp:8742 on the named network --> mailhub-mcp
```

The container is still internet-facing. Keep the firewall unit from [../tailscale-funnel/firewall/](../tailscale-funnel/firewall/): the sidecar sits on the same named network with a fixed subnet, so the firewall keeps both containers away from the host, the LAN and the tailnet, and lets the sidecar reach mailhub-mcp's port (`APP_PORT`, default 8742) on that subnet. The two containers don't share a network namespace, so a restart of one doesn't cut the other off. The sidecar also runs Tailscale in userspace mode, so mailhub-mcp has no route into the tailnet.

Paths below assume the project lives in `/opt/mailhub-mcp`.

## 1. Tailnet policy and auth key

In the Tailscale admin console:

1. Enable HTTPS certificates for the tailnet.
2. In the tailnet policy:
   - define a tag for the sidecar, for example `"tagOwners": {"tag:mailhub": ["autogroup:admin"]}`;
   - allow Funnel for it with `{"target": ["tag:mailhub"], "attr": ["funnel"]}` under `nodeAttrs`;
   - do not grant `tag:mailhub` access to anything else.
3. Create an auth key with the tag `tag:mailhub`. It should be **not ephemeral**, so the node survives restarts. A one-off key is enough, because the node identity is kept in the state volume after the first login.
4. Save the key on the host and restrict it:

   ```bash
   sudo install -m 600 -o root -g root /dev/stdin /opt/mailhub-mcp/secrets/ts-authkey <<< 'tskey-auth-...'
   ```

## 2. URL, `.env` and Google redirect URIs

The public URL is `https://<TS_HOSTNAME>.<tailnet>.ts.net`, and `TS_HOSTNAME` defaults to `mailhub`.

1. Set `PUBLIC_BASE_URL` to it in `.env`.
2. Add these authorized redirect URIs to the Google Web client:
   - `https://mailhub.<tailnet>.ts.net/auth/callback`
   - `https://mailhub.<tailnet>.ts.net/admin/auth/callback`
   - `https://mailhub.<tailnet>.ts.net/admin/oauth/callback`

## 3. Start it

```bash
docker compose -f docker-compose.yml -f examples/tailscale-sidecar/compose.override.yml up -d
docker logs mailhub-mcp-tailscale 2>&1 | tail -20
curl -sf https://mailhub.<tailnet>.ts.net/health
```

The first start logs in with the key and requests the certificate. The first HTTPS request can take a few seconds while the certificate is issued.

## 4. Point the connector at the new URL

In claude.ai, remove the old custom connector and add one with `https://mailhub.<tailnet>.ts.net/mcp`, then log in again. Scheduled routines that use the connector pick up the new one once it has the same name. If they don't, edit them.

## Moving from ../tailscale-funnel/

1. Do steps 1 to 3 above. The old container is replaced by the same service name, and `data/` and `secrets/accounts/` are untouched, so linked mailboxes and the change journal stay.
2. Check the new URL (`/health`, then `/admin`), and switch the connector (step 4).
3. Turn off the host Funnel: `sudo tailscale funnel reset`.
4. Keep `mailhub-mcp-firewall.service` enabled. It protects the sidecar's subnet.
5. Remove the old redirect URIs from the Google Web client once nothing uses them.

To roll back before step 3, run the old compose command (`-f examples/tailscale-funnel/compose.override.yml`). The host Funnel still forwards to `127.0.0.1:8742`.

## Moving from the shared-namespace version

Earlier versions of this example ran mailhub-mcp inside the sidecar's network namespace. A sidecar restart then left mailhub-mcp behind and Funnel answered 502. To move:

1. `git pull` and rerun the firewall: `sudo systemctl restart mailhub-mcp-firewall`. The new rule lets the sidecar reach mailhub-mcp on the subnet.
2. Run the update commands below. Both containers are recreated.

## More than one person

A second instance gets its own sidecar and hostname instead of Funnel port 8443. In its `.env` set:

```
TS_HOSTNAME=mailhub-2
TS_CONTAINER_NAME=mailhub-mcp-2-tailscale
TS_STATE_VOLUME=mailhub-mcp-2-tailscale
CONTAINER_NAME=mailhub-mcp-2
COMPOSE_PROJECT_NAME=mailhub-mcp-2
NETWORK_NAME=mailhub_mcp_2
SUBNET=172.31.1.0/24
```

Then follow the firewall step for a second subnet in [../tailscale-funnel/README.md](../tailscale-funnel/README.md#more-than-one-person), with its own auth key in its own `secrets/ts-authkey`.

## Updates

```bash
docker compose -f docker-compose.yml -f examples/tailscale-sidecar/compose.override.yml pull
docker compose -f docker-compose.yml -f examples/tailscale-sidecar/compose.override.yml up -d
```
