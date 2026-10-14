# Troubleshooting

Debug from the outside in. Each step rules out one layer, so stop at the first one that fails.

```
internet ─► edge NAT ─► LoadBalancer IP ─► stunnerd ─► JVB pod
   (1)          (1)            (1)            (2,3)       (4,5)
```

Symptom that brought most people here: **the Jitsi page loads and you join the room, but there is no audio/video, and the call drops after a few seconds.** Signaling (web, ingress, Prosody, jicofo) works; ICE between the browser and JVB fails.

---

## 1. Does UDP reach STUNner?

From a machine outside your network:

```bash
python3 debug/stun-ping.py <PUBLIC_IP>
```

- `OK` → NAT, firewall and LoadBalancer pass UDP. Go to step 2.
- `TIMEOUT` → check:
  - the edge NAT forwards **UDP** 3478 (not only TCP) to `<LB_INTERNAL_IP>`;
  - `kubectl -n stunner get svc` shows `EXTERNAL-IP=<LB_INTERNAL_IP>`;
  - Cilium announces that IP (L2 announcements or BGP);
  - your ISP does not filter the port.

Repeat the test against `<LB_INTERNAL_IP>` from inside your LAN. Works inside but not outside → the problem is the edge NAT/firewall.

## 2. Does TURN authentication work?

```bash
./debug/gen-turn-credentials.sh '<TURN_SHARED_SECRET>' <PUBLIC_IP>
```

Open the [Trickle ICE page](https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/) **from mobile data**, add `turn:<PUBLIC_IP>:3478?transport=udp` with the printed username/password, click *Gather candidates*.

- A `relay` candidate appears → STUNner is reachable and the secret is right. Go to step 3.
- `401` / authentication errors → `sharedSecret` in the GatewayConfig differs from what you used, or the clocks are badly out of sync.

Note: the `srflx` candidate may show an internal cluster IP. That is Cilium SNAT and is harmless for the TURN path.

## 3. Does the browser get and use TURN from Jitsi?

Join a room and, **within the first seconds** (the session is torn down when ICE fails), run in the browser console:

```js
APP.conference._room.jvbJingleSession?.peerconnection?.peerconnection?.getConfiguration()?.iceServers
```

Or open `chrome://webrtc-internals` before joining. It keeps the data after the call drops.

- `iceServers` contains `turn:<PUBLIC_IP>:3478` with a username and credential → Prosody is fine. Go to step 4.
- Empty or missing TURN → Prosody is not advertising it:

  ```bash
  kubectl -n jitsi exec <release>-jitsi-meet-prosody-0 -- env | grep -E 'TURN|STUN'
  ```

  Use `TURN_HOST`, `TURN_PORT`, `TURN_TRANSPORT=udp`, `TURN_CREDENTIALS`. Names like `TURN_CREDENTIALS_SECRET` are ignored, and `TURNS_*` means TURN over **TLS**.

- TURN is listed but never used → `config.useTurnUdp = true;` is missing. Jitsi drops TURN/UDP for the JVB connection by default. Check:

  ```bash
  kubectl -n jitsi exec deploy/<release>-jitsi-meet-web -- grep -r useTurnUdp /config/
  ```

  Hard-refresh the browser afterwards: `config.js` is cached.

Note: Prosody is a **StatefulSet** in this chart, so `kubectl exec deploy/...` does not find it. Use the pod name (`...-prosody-0`) or `sts/...`.

## 4. Does STUNner allow the relay to JVB?

Watch the stunnerd log while someone joins a room:

```bash
kubectl -n stunner logs -f deploy/stunner-gateway
```

### `permission denied ... to peer <ip>: no route to endpoint`

STUNner does not consider `<ip>` a backend of any route on the listener.

1. Is `<ip>` the JVB pod IP?

   ```bash
   kubectl -n jitsi get pod -l app.kubernetes.io/component=jvb -o wide
   ```

   If not, JVB advertises something else. Remove `JVB_ADVERTISE_IPS`, `useNodeIP`, `hostNetwork` and `hostPort`.

2. Is the route healthy? Both `Accepted` and `ResolvedRefs` must be `True`:

   ```bash
   kubectl -n stunner get udproute <route> -o yaml | sed -n '/status:/,$p'
   ```

   `ResolvedRefs=False` with `RefNotPermitted` → the ReferenceGrant is missing or in the wrong namespace.

3. Does the operator know the pod IP?

   ```bash
   kubectl -n stunner-system port-forward svc/stunner-config-discovery 13478:13478 &
   sleep 2
   curl -s localhost:13478/api/v1/configs | python3 -m json.tool | grep -A6 '"endpoints"'
   ```

4. Does **stunnerd** know it? Compare with its last applied config:

   ```bash
   kubectl -n stunner logs deploy/stunner-gateway | grep "New configuration" | tail -1
   ```

   If the operator lists the current JVB pod IP but stunnerd's last config shows an **old** one, stunnerd lost its CDS watch and is running a stale config. See [Stale stunnerd config](#stale-stunnerd-config).

### Relay allowed, still no media

Packets leave STUNner but JVB does not answer. Check network policies (UDP 10000 from the `stunner` namespace into the JVB pod) and the JVB log:

```bash
hubble observe --to-ip <JVB_POD_IP> --port 10000 -f
kubectl -n jitsi logs deploy/<release>-jitsi-meet-jvb-0 | grep -iE "ice|failed|selected"
```

`debug/udp-echo.yaml` tests the whole TURN path independently of Jitsi.

## 5. Stale stunnerd config

**Symptom:** stunnerd logs `closing connection to server ...:13478` and never logs `connection successfully opened` again. From then on it ignores every change, for example a new JVB pod IP after a redeploy.

**Immediate fix:**

```bash
kubectl -n stunner rollout restart deploy/stunner-gateway
kubectl -n stunner logs -f deploy/stunner-gateway | grep -E "cds|New configuration"
```

**Root cause we found: the operator kept restarting.**

```bash
kubectl -n stunner-system get pods                      # look at RESTARTS
kubectl -n stunner-system logs deploy/stunner-gateway-operator-controller-manager --previous --tail=40
```

```
failed to renew lease stunner-system/...: context deadline exceeded
ERROR setup problem running manager {"error": "leader election lost"}
```

The operator could not renew its leader-election lease because the kube-apiserver answered too slowly, so it exited. Every exit drops stunnerd's websocket to CDS.

- With one operator replica, leader election is useless. Remove `--leader-elect` from the operator args (see `stunner/values-stunner.yaml`).
- Investigate why the apiserver is slow: other controllers with many restarts, etcd disk latency (`etcd_disk_wal_fsync_duration_seconds`), master node load.

  ```bash
  kubectl get pods -A --no-headers | awk '$5+0 > 20'
  kubectl get --raw='/readyz?verbose' | grep -v ok
  ```

- Alert on stunnerd `closing connection` without a reconnect within a minute.

## 6. Jitsi pods do not start

### JVB / jicofo in `CrashLoopBackOff`, liveness `connection refused` on 8080/8888

Both are Java. With `100m` CPU the JVM starts too slowly and the liveness probe kills it before the port opens; with `128Mi` it gets OOMKilled. Give them real resources and cap the heap:

```yaml
jicofo:
  resources: { requests: { cpu: 100m, memory: 512Mi }, limits: { cpu: 500m, memory: 768Mi } }
  extraEnvs: { JICOFO_MAX_MEMORY: "512m" }
jvb:
  resources: { requests: { cpu: 500m, memory: 1Gi }, limits: { cpu: 2, memory: 1536Mi } }
  extraEnvs: { VIDEOBRIDGE_MAX_MEMORY: "1024m" }
```

### Helm: `nil pointer evaluating interface {}._default`

An empty key such as

```yaml
web:
  custom:
    defaults:
```

is `null`, and a null in user values **deletes** the chart default. Remove empty keys entirely. Render before installing:

```bash
helm template test jitsi/jitsi-meet -f jitsi/values.yaml > /dev/null && echo OK
```

## 7. cert-manager cannot issue the certificate

```
Waiting for HTTP-01 challenge propagation: failed to perform self check GET request ... context deadline exceeded
```

A timeout means packets are dropped somewhere. Test from outside and inside the cluster:

```bash
curl -v http://jitsi.example.com/.well-known/acme-challenge/<token>
kubectl run tmp --rm -it --image=curlimages/curl --restart=Never -- \
  curl -v -m 10 http://jitsi.example.com/.well-known/acme-challenge/<token>
```

| Outside | Inside | Cause |
|---|---|---|
| timeout | timeout | TCP 80 not forwarded to the ingress controller, or a network policy blocks ingress controller → solver pod |
| works | timeout | No hairpin NAT. Enable NAT loopback on the router, or rewrite the domain to the ingress Service in CoreDNS |

The solver pod runs in the **Certificate's namespace** (the Jitsi namespace). If that namespace has an ingress policy, the ingress controller's namespace must be allowed. Check with:

```bash
hubble observe --namespace jitsi --verdict DROPPED -f
```

For many tenants, consider a wildcard certificate via DNS-01: no port 80, no hairpin, no Let's Encrypt rate limits.
