#!/bin/sh
# ev-bridge: active getEvStatus polling + setEvStatus call,
# via adb -> wallpad busybox nc -> EV server(10.0.0.2:29715) -> MQTT.
# Supersedes ev-mqtt.sh. Wallpad stays stock.

[ -f /root/ev-mqtt.conf ]   && . /root/ev-mqtt.conf
[ -f /root/ev-bridge.conf ] && . /root/ev-bridge.conf
MQTT_HOST="${MQTT_HOST:-192.168.0.67}"; MQTT_PORT="${MQTT_PORT:-1883}"
EV_IP="${EV_IP:-10.0.0.2}"; EV_PORT="${EV_PORT:-29715}"
POLL_FAST="${POLL_FAST:-1}"; MON_TTL="${MON_TTL:-120}"
BASE="wallpad/elevator"; CMD="$BASE/cmd"
DEADLINE=/tmp/ev_mon_deadline; PPID_F=/tmp/ev_poller.pid; LAST_F=/tmp/ev_last

pub() { mosquitto_pub -h "$MQTT_HOST" -p "$MQTT_PORT" -u "$MQTT_USER" -P "$MQTT_PASS" "$@" 2>/dev/null; }

GET_REQ='<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:ns="urn:ces"><SOAP-ENV:Body SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><ns:getEvStatus><in>1</in></ns:getEvStatus></SOAP-ENV:Body></SOAP-ENV:Envelope>'

soap() {
  b64=$(printf %s "$1" | base64 | tr -d '\n')
  adb shell "echo $b64 | busybox base64 -d | busybox nc -w 5 $EV_IP $EV_PORT | busybox base64" </dev/null 2>/dev/null | tr -d '\r\n' | base64 -d
}
field() { printf '%s' "$2" | sed -n "s/.*<$1>\([0-9]*\)<\/$1>.*/\1/p"; }

poll_once() {
  resp=$(soap "$GET_REQ")
  printf '%s' "$resp" | grep -q "<carFloor>" || return 1
  floor=$(field carFloor "$resp"); base=$(field isBasement "$resp")
  dir=$(field carDirection "$resp"); ev=$(field evStatus "$resp")
  cu=$(field callUp "$resp"); cd=$(field callDown "$resp")
  disp="$floor"; [ "$base" = "1" ] && disp="B$floor"
  case "$dir" in 1) dirtxt=up ;; 2) dirtxt=down ;; *) dirtxt=idle ;; esac
  cur="$disp|$dirtxt|$ev|$cu|$cd"
  echo "$cur" > "$LAST_F"
  pub -t "$BASE/floor" -m "$disp"
  pub -t "$BASE/direction" -m "$dirtxt"
  pub -t "$BASE/ev_status" -m "${ev:-0}"
  pub -t "$BASE/call_up" -m "${cu:-0}"
  pub -t "$BASE/call_down" -m "${cd:-0}"
}

send_call() {
  if [ "$1" = "up" ]; then cu=1; cd=0; else cu=0; cd=1; fi
  body='<?xml version="1.0" encoding="UTF-8"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:ns="urn:ces"><SOAP-ENV:Body SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><ns:setEvStatus><in xsi:type="ns:inEvStatus"><callUp>'"$cu"'</callUp><callDown>'"$cd"'</callDown></in><isWallpad>1</isWallpad></ns:setEvStatus></SOAP-ENV:Body></SOAP-ENV:Envelope>'
  len=$(printf %s "$body" | wc -c)
  req=$(printf 'POST / HTTP/1.1\r\nHost: %s:%s\r\nUser-Agent: gSOAP/2.8\r\nContent-Type: text/xml; charset=utf-8\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s' "$EV_IP" "$EV_PORT" "$len" "$body")
  resp=$(soap "$req")
  if printf '%s' "$resp" | grep -q "<out>0</out>"; then
    pub -t "$BASE/call_result" -m ok; logger -t ev-bridge "call $1 ok"
  else
    pub -t "$BASE/call_result" -m fail; logger -t ev-bridge "call $1 FAIL"
  fi
  poll_once
}

poller_loop() {
  pub -r -t "$BASE/monitor" -m on
  while [ "$(date +%s)" -lt "$(cat $DEADLINE 2>/dev/null || echo 0)" ]; do
    poll_once; sleep "$POLL_FAST"
  done
  pub -r -t "$BASE/monitor" -m off; rm -f "$PPID_F"
}
start_poller() {
  echo $(( $(date +%s) + MON_TTL )) > "$DEADLINE"
  if [ -f "$PPID_F" ] && kill -0 "$(cat $PPID_F 2>/dev/null)" 2>/dev/null; then return; fi
  poller_loop & echo $! > "$PPID_F"
}
stop_poller() {
  echo 0 > "$DEADLINE"
  [ -f "$PPID_F" ] && kill "$(cat $PPID_F 2>/dev/null)" 2>/dev/null
  rm -f "$PPID_F"; pub -r -t "$BASE/monitor" -m off
}
cleanup() { stop_poller; pub -r -t "$BASE/available" -m offline; exit 0; }
trap cleanup INT TERM

pub -r -t "homeassistant/sensor/wp_ev_floor/config" -m '{"name":"Elevator Floor","uniq_id":"wp_ev_floor","stat_t":"wallpad/elevator/floor","ic":"mdi:elevator","avty_t":"wallpad/elevator/available","dev":{"ids":["wp_elevator"],"name":"Wallpad Elevator"}}'
pub -r -t "homeassistant/sensor/wp_ev_dir/config" -m '{"name":"Elevator Direction","uniq_id":"wp_ev_dir","stat_t":"wallpad/elevator/direction","ic":"mdi:swap-vertical","avty_t":"wallpad/elevator/available","dev":{"ids":["wp_elevator"],"name":"Wallpad Elevator"}}'
pub -r -t "$BASE/available" -m online
logger -t ev-bridge "started (host=$MQTT_HOST ev=$EV_IP:$EV_PORT)"

mosquitto_sub -h "$MQTT_HOST" -p "$MQTT_PORT" -u "$MQTT_USER" -P "$MQTT_PASS" -t "$CMD/#" -v | \
while read -r topic payload; do
  case "$topic" in
    "$CMD/call")    send_call "$payload" ;;
    "$CMD/monitor") [ "$payload" = on ] && start_poller; [ "$payload" = off ] && stop_poller ;;
  esac
done
