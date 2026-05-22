#!/bin/bash
# start_desktop.sh - Starts Xvfb, Openbox, x11vnc and noVNC on display :1

echo 'Stopping any existing VNC/Xvfb services...'
pkill -9 -f 'Xvfb :1' || true
pkill -9 -f 'x11vnc -display :1' || true
pkill -9 -f 'websockify' || true
pkill -9 -f 'openbox' || true
sleep 1

echo 'Starting Xvfb on display :1...'
Xvfb :1 -screen 0 1280x720x24 > /var/log/xvfb.log 2>&1 &
sleep 2

export DISPLAY=:1

echo 'Starting Openbox window manager...'
openbox > /var/log/openbox.log 2>&1 &
sleep 1

echo 'Starting x11vnc on display :1...'
x11vnc -display :1 -nopw -listen 127.0.0.1 -shared -forever -bg -o /var/log/x11vnc.log
sleep 1

echo 'Starting websockify/noVNC on port 6080...'
websockify --web /usr/share/novnc 6080 127.0.0.1:5900 > /var/log/websockify.log 2>&1 &
sleep 2

echo 'Graphical and VNC services started!'
echo 'Web noVNC (bind your own nginx reverse-proxy if remote): http://127.0.0.1:6080/vnc.html?autoconnect=true'
echo "LAN URL example: http://$(hostname -I | awk '{print $1}'):6080/vnc.html?autoconnect=true"
