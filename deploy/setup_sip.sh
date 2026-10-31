#!/usr/bin/env bash
# Puts the SIP trunk in front of the streamed-call agent: Asterisk accepts the trunk's calls and hands each one to Talk (AudioSocket on 127.0.0.1:4574).
# Nothing here touches the callback path: the HTTPS callback keeps working until the number's callback is switched in the provider's dashboard.
#
#   bash setup_sip.sh install    install Asterisk, write its settings, open the SIP port to the trunk's address only, switch the agent on
#   bash setup_sip.sh status     is everything up and listening, and what did the last streamed calls do
#   bash setup_sip.sh selftest   make a test call through Asterisk into the agent (a spoken sentence played as the caller) and show what Talk heard and said
#   bash setup_sip.sh log        the last lines of Asterisk's own log
#   bash setup_sip.sh doctor     the dialplan, the open channels and the recent lines of both logs, for finding a fault
#   bash setup_sip.sh sniff [s]  show the SIP messages that reach this machine for s seconds (default 90): does the trunk's call arrive at all?
#   bash setup_sip.sh off        stop Asterisk, close the ports, switch the agent off (the callback path is untouched)
#
# Run as root, after deploy_health.sh has put the build in /opt/sofa-health.
set -euo pipefail

NEW=/opt/sofa-health
ENVFILE="$NEW/src/.env"
TRUNK_IP="${TRUNK_IP:-102.223.37.68}"      # Africa's Talking's SIP server
RTP_FROM=10000
RTP_TO=10100
AGENT_PORT=4574
STAMP="$(date +%Y%m%d-%H%M)"
MARK="; Talk SIP trunk (written by setup_sip.sh; the earlier file is kept beside it as *.talk-backup-*)"

[[ $EUID -eq 0 ]] || { echo "Run this as root"; exit 1; }

set_env() {  # set_env KEY VALUE  (plain settings only)
  sed -i "/^$1=/d" "$ENVFILE"; echo "$1=$2" >> "$ENVFILE"
}

write_conf() {  # write_conf FILE (content on stdin): keeps the old file once
  local f="$1"
  if [[ -f "$f" ]] && ! grep -qF "Talk SIP trunk" "$f"; then cp "$f" "$f.talk-backup-$STAMP"; fi
  { echo "$MARK"; cat; } > "$f"
}

firewall() {  # firewall add|remove
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    if [[ "$1" == add ]]; then
      ufw allow from "$TRUNK_IP" to any port 5060 proto udp >/dev/null
      ufw allow "$RTP_FROM:$RTP_TO/udp" >/dev/null   # the trunk's media may come from another address than its signalling, so the audio ports are open; Asterisk ignores audio for a call it did not set up
      echo "   firewall: SIP (5060/udp) open to $TRUNK_IP only; audio $RTP_FROM-$RTP_TO/udp open"
    else
      ufw delete allow from "$TRUNK_IP" to any port 5060 proto udp >/dev/null 2>&1 || true
      ufw delete allow "$RTP_FROM:$RTP_TO/udp" >/dev/null 2>&1 || true
      echo "   firewall: SIP and audio ports closed again"
    fi
  else
    echo "   firewall: ufw is not active on this machine. If the provider gives you a firewall panel, allow 5060/udp from $TRUNK_IP and $RTP_FROM-$RTP_TO/udp."
  fi
}

install_all() {
  [[ -d "$NEW/src" ]] || { echo "The new build is not in $NEW: run deploy_health.sh first."; exit 1; }
  echo "1/5 Installing Asterisk (a few minutes)"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq asterisk >/dev/null
  systemctl stop asterisk >/dev/null 2>&1 || true

  echo "2/5 Writing its settings"
  grep -q "noload *= *chan_sip.so" /etc/asterisk/modules.conf || sed -i '/^\[modules\]/a noload = chan_sip.so' /etc/asterisk/modules.conf   # the old SIP stack would fight for port 5060
  write_conf /etc/asterisk/pjsip.conf <<EOF
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060

[africastalking]
type=endpoint
transport=transport-udp
context=from-trunk
disallow=all
allow=alaw
allow=ulaw
direct_media=no
rtp_symmetric=yes
force_rport=yes
dtmf_mode=rfc4733
trust_id_inbound=yes

; the trunk is recognised by the address its calls come from
[africastalking-address]
type=identify
endpoint=africastalking
match=$TRUNK_IP
EOF
  write_conf /etc/asterisk/rtp.conf <<EOF
[general]
rtpstart=$RTP_FROM
rtpend=$RTP_TO
strictrtp=yes
EOF
  # AudioSocket carries no caller id of its own, so the caller's digits are written into the call's id: right-aligned in 32 places, zero-filled
  write_conf /etc/asterisk/extensions.conf <<EOF
[from-trunk]
exten => _.,1,NoOp(a call for Talk)
 same => n,Answer()
 same => n,Set(DIGITS=\${FILTER(0-9,\${CALLERID(num)})})
 same => n,Set(PADDED=00000000000000000000000000000000\${DIGITS})
 same => n,Set(ID=\${PADDED:-32})
 same => n,Set(CALLID=\${ID:0:8}-\${ID:8:4}-\${ID:12:4}-\${ID:16:4}-\${ID:20:12})
 same => n,Log(NOTICE,talk: call \${CALLID} goes to the agent)
 same => n,AudioSocket(\${CALLID},127.0.0.1:$AGENT_PORT)
 same => n,Log(NOTICE,talk: call \${CALLID} is over)
 same => n,Hangup()

; only reachable from this machine (setup_sip.sh selftest), never from the trunk. The first context is the Talk side of the call (as if
; the trunk had sent it); the second is the caller's side: one sound file that is quiet through the greeting, then the sentence, then quiet again.
[selftest]
exten => 1,1,Set(CALLERID(num)=+2348055550000)
 same => n,Goto(from-trunk,self,1)

[selftest-caller]
exten => caller,1,Log(NOTICE,talk selftest: the caller side is up)
 same => n,Playback(talk/selftest)
 same => n,Log(NOTICE,talk selftest: the sound ended: \${PLAYBACKSTATUS})
 same => n,Hangup()
EOF
  write_conf /etc/asterisk/logger.conf <<EOF
[general]
dateformat=%F %T

[logfiles]
console => notice,warning,error
messages => notice,warning,error
EOF
  chown -R asterisk:asterisk /etc/asterisk 2>/dev/null || true

  echo "3/5 Opening the ports"
  firewall add

  echo "4/5 Starting Asterisk and switching the agent on"
  systemctl enable asterisk >/dev/null 2>&1
  systemctl restart asterisk
  set_env SIP_ENABLED 1
  set_env SIP_PORT "$AGENT_PORT"
  chown sofa:sofa "$ENVFILE"
  systemctl restart sofa-health
  sleep 6

  echo "5/5 Checking"
  status_all
  echo
  echo "Next: bash setup_sip.sh selftest"
}

status_all() {
  echo "   asterisk: $(systemctl is-active asterisk)"
  echo "   AudioSocket modules: $(asterisk -rx 'module show like audiosocket' 2>/dev/null | grep -c 'Running' || true) running (need 3: res_, app_, chan_)"
  asterisk -rx 'pjsip show endpoints' 2>/dev/null | grep -E "Endpoint:|Identify" | sed 's/^/   /' || true
  echo "   listening: $(ss -lun | grep -c ':5060') SIP port(s), agent port $AGENT_PORT: $(ss -ltn | grep -c ":$AGENT_PORT ") listener(s)"
  echo "   agent: $(journalctl -u sofa-health -n 200 --no-pager 2>/dev/null | grep -c 'streamed calls: listening') start line(s) in the log"
  echo "   last streamed calls:"
  journalctl -u sofa-health --no-pager 2>/dev/null | grep -E "streamed (call|turn)" | tail -8 | sed 's/^/     /' || true
}

selftest() {
  [[ "$(systemctl is-active asterisk)" == active ]] || { echo "Asterisk is not running: bash setup_sip.sh install"; exit 1; }
  # The test sound must be where Asterisk looks for sounds (on Ubuntu that is /usr/share/asterisk/sounds, not /var/lib): ask it, else take the first that exists.
  local data sounds since
  data="$(asterisk -rx 'core show settings' 2>/dev/null | grep -i -E 'data +dir' | head -1 | sed 's/^[^:]*: *//' | tr -d '[:space:]')"
  [[ -n "$data" && -d "$data" ]] || data="$(for d in /usr/share/asterisk /var/lib/asterisk; do [[ -d "$d/sounds" ]] && { echo "$d"; break; }; done)"
  sounds="${data:-/var/lib/asterisk}/sounds/talk"
  install -d -o sofa -g asterisk -m 775 "$sounds"
  rm -f "$sounds"/selftest.*
  echo "   test sound goes in $sounds"
  since="$(date +%s)"
  (cd "$NEW/src" && SELFTEST_SOUNDS="$sounds" sudo -E -u sofa "$NEW/venv/bin/python" -m scripts.sip_selftest make "$@")
  echo "2. calling Talk through Asterisk"
  asterisk -rx "channel originate Local/1@selftest/n extension caller@selftest-caller" | sed 's/^/   /'
  if ! (cd "$NEW/src" && SELFTEST_SINCE="$since" sudo -E -u sofa "$NEW/venv/bin/python" -m scripts.sip_selftest result); then
    diagnose
    return 1
  fi
  echo
  echo "Timing of that call, from the agent's log (milliseconds after the caller stopped speaking):"
  { journalctl -u sofa-health --no-pager --since "3 min ago" 2>/dev/null | grep -E "streamed turn" | sed 's/^.*INFO:sofa.sip://' | sed 's/^/   /'; } || true
}

sniff() {  # sniff [seconds]: what arrives on the SIP port while a call is made (to see whether the trunk's call reaches this machine at all)
  command -v tcpdump >/dev/null 2>&1 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tcpdump >/dev/null
  echo "Listening for SIP on port 5060 for ${1:-90} seconds. Make the call now."
  timeout "${1:-90}" tcpdump -nn -A -s0 -l -i any udp port 5060 2>/dev/null | grep -E "^[0-9:.]+ IP |^INVITE |^SIP/2.0 |^From: |^To: |^User-Agent: " | cut -c1-160 || true
  echo "Done listening."
}

diagnose() {
  echo
  echo "---- what Asterisk and the agent say (for finding the fault) ----"
  echo "dialplan:"; asterisk -rx 'dialplan show selftest' 2>&1 | head -8 | sed 's/^/   /'
  asterisk -rx 'dialplan show selftest-caller' 2>&1 | head -6 | sed 's/^/   /' || true
  echo "channels now:"; asterisk -rx 'core show channels' 2>&1 | head -8 | sed 's/^/   /'
  echo "Asterisk log (files: $(ls /var/log/asterisk 2>/dev/null | tr '\n' ' ')):"
  { tail -n 25 /var/log/asterisk/messages 2>/dev/null | cut -c1-220 | sed 's/^/   /'; } || true
  echo "Asterisk in the system journal (last 12 lines):"
  { journalctl -u asterisk --no-pager -n 12 2>/dev/null | cut -c1-200 | sed 's/^/   /'; } || true
  echo "agent log (streamed calls and errors, last 20 lines):"
  { journalctl -u sofa-health --no-pager -n 400 2>/dev/null | grep -E "streamed|sofa.sip|Traceback|Error" | tail -20 | cut -c1-240 | sed 's/^/   /'; } || true
}

case "${1:-}" in
  install) install_all ;;
  status) status_all ;;
  selftest) shift; selftest "$@" ;;
  log) tail -n 60 /var/log/asterisk/messages 2>/dev/null || journalctl -u asterisk -n 60 --no-pager ;;
  doctor) diagnose ;;
  sniff) shift; sniff "$@" ;;
  off)
    systemctl disable --now asterisk >/dev/null 2>&1 || true
    firewall remove
    set_env SIP_ENABLED 0; chown sofa:sofa "$ENVFILE"; systemctl restart sofa-health
    echo "Streamed calls are off. The callback path was not touched." ;;
  *) echo "usage: bash setup_sip.sh install|status|selftest|log|off"; exit 2 ;;
esac
