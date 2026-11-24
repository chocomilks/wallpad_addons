# ev-bridge

Elevator status polling + call bridge for a Commax wallpad, running on the
in-line mango router (GL-MT300N-V2 / OpenWrt). Replaces the passive
`ev-mqtt.sh` tcpdump collector.

## How it works

The wallpad is rooted via USB `adb` (J15 OTG). The complex elevator server
(`10.0.0.2:29715`, gSOAP) authenticates by source IP, so the wallpad's own
network identity is the only one it accepts. ev-bridge therefore sends the
SOAP requests **from inside the wallpad shell** over adb, using a
PTY-safe `base64 | nc | base64` pipe — no network spoofing, nothing
installed on the wallpad.

- **Monitor**: on MQTT `wallpad/elevator/cmd/monitor on`, polls
  `getEvStatus` every `POLL_FAST`s (self-stops after `MON_TTL`s or on
  `off`) and publishes floor / direction / ev_status / call_up / call_down.
- **Call**: on `wallpad/elevator/cmd/call up|down`, sends `setEvStatus`
  (direction only; server maps source IP -> household floor).
- Creates HA sensors via MQTT Discovery (`wallpad/elevator/floor`,
  `.../direction`).

## Install (on mango)

```sh
cp ev-bridge.sh   /root/ev-bridge.sh
cp ev-bridge.conf /root/ev-bridge.conf
cp ev-bridge.init /etc/init.d/ev-bridge
chmod +x /etc/init.d/ev-bridge
/etc/init.d/ev-mqtt stop; /etc/init.d/ev-mqtt disable   # retire old collector
/etc/init.d/ev-bridge enable; /etc/init.d/ev-bridge start
```

## MQTT topics

| topic | dir | payload |
|---|---|---|
| `wallpad/elevator/cmd/monitor` | in  | `on` / `off` |
| `wallpad/elevator/cmd/call`    | in  | `up` / `down` |
| `wallpad/elevator/floor`       | out | e.g. `7`, `B1` |
| `wallpad/elevator/direction`   | out | `up` / `down` / `idle` |
| `wallpad/elevator/ev_status`   | out | numeric |
| `wallpad/elevator/call_up` / `call_down` | out | `0` / `1` |
| `wallpad/elevator/monitor`     | out | `on` / `off` (retained) |
| `wallpad/elevator/available`   | out | `online` / `offline` (retained) |
