# Render + residential proxy setup

Polymarket's CLOB geoblocks the `POST /order` endpoint from US and EU datacenter IPs. To keep running on Render, we route **only** the CLOB API traffic through a residential proxy — Gamma scans stay direct for speed.

Total extra cost: ~$7–15/mo for the proxy. Total time: ~10 min.

---

## 1. Pick a proxy provider

Any HTTP proxy with an exit node in a Polymarket-allowed country works. Cheapest reliable options:

| Provider | Plan | Cost | Notes |
|---|---|---|---|
| **[IPRoyal Royal Residential](https://iproyal.com/residential-proxies/)** | Pay-as-you-go | $7 min, $1.75/GB | Best for our use — bot uses maybe 50 MB/mo |
| **[Webshare](https://www.webshare.io/)** | Rotating residential | $6/mo (1 GB) | Good UI, cheap entry |
| **[SmartProxy](https://smartproxy.com/)** | Residential | $8.50/GB | Reliable but priciest |

**For this bot** you want:
- **Protocol**: HTTP
- **Type**: Rotating OR sticky residential (either works — sticky is nicer for auth caching)
- **Country**: **Finland, India, Brazil, Japan, or Mexico** (definitely not blocked). Avoid US/UK/FR/BE/SG/AU/CA-Ontario/TW/TH.
- **Whitelist**: either IP-auth (whitelist Render's worker egress IP) or user:pass (simpler)

Grab the URL in this format:
```
http://USER:PASS@proxy.provider.com:PORT
```

---

## 2. Set the proxy in Render

Render dashboard → **polymarket-bot** → **Environment** tab → add:

| Key | Value |
|---|---|
| `CLOB_PROXY_URL` | `http://USER:PASS@proxy.provider.com:PORT` |

Save. Render auto-redeploys.

That's it on the Render side. The bot code already reads this env var and, when set, transparently routes every CLOB API call (including `POST /order`) through the proxy.

---

## 3. Verify it worked

After redeploy, the startup log should show:

```
Polymarket near-certainty bot — LIVE TRADING
CLOB client initialized (signature_type=0)
CLOB requests now routed via proxy proxy.provider.com:PORT
```

On the next scan, when an order fires you should see:
```
POST https://clob.polymarket.com/order "HTTP/2 200 OK"
```

…and a Telegram ping with the order ID.

If you still see `403 geoblock`, the proxy's exit node is in a blocked country. Swap the proxy config for a Finnish or Indian exit.

---

## 4. Troubleshooting

### `Failed to install CLOB proxy: ...`
Proxy URL malformed. Must be `http://user:pass@host:port` (not `https://` for the proxy protocol itself, even though the target is HTTPS — that's normal for HTTP CONNECT tunneling).

### `httpx.ProxyError: 407 Proxy Authentication Required`
Wrong username/password, or the provider requires IP whitelisting instead of user:pass. Add Render's worker outbound IP to the whitelist (Render dashboard shows this under **Settings → Outbound IP Addresses**).

### Orders still return 403 geoblock
The proxy provider routed you through a blocked country. Most providers let you target a specific country — explicitly pick **Finland** (`country-FI`) or **India** (`country-IN`). IPRoyal syntax: `proxy.iproyal.com:12321:USER:PASS_country-fi`.

### Bot runs slow now
Only CLOB calls are proxied; scans (Gamma) go direct, so scan speed is unchanged. Order placement adds ~200–500ms per order via proxy — inconsequential for a near-certainty strategy.

---

## 5. Data usage estimate

- Scan: 0 proxy traffic (Gamma API is direct)
- Order book fetches: 0 proxy traffic (CLOB /book — also direct via `requests` library, not `httpx`)
- Order submission: ~5 KB per order
- API auth ping: ~10 KB once per process start

At ~20 orders/day that's **<5 MB/month**, well under any proxy plan's minimum. Your actual bill will be the plan minimum (e.g. IPRoyal $7).
