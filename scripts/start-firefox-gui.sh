#!/bin/bash
set -euxo pipefail
browser_binary=${LAB_FIREFOX_BINARY:-/opt/firefox/firefox}
browser_profile=${LAB_FIREFOX_PROFILE:-/home/ga/firefox-sandbox-test}
test -x "$browser_binary"
install -d -o ga -g ga -m 700 "$browser_profile"
cd /root/lab-compose
docker compose exec -T web sh -c 'cat > /usr/share/nginx/html/lab.html' <<'HTML'
<!doctype html><html><head><title>UML browser experiment</title></head>
<body style="font:24px sans-serif;padding:48px;background:#eef3f7;color:#17324d">
<h1>Linux without KVM</h1>
<p>Firefox on GNOME, opening a page served by Docker nginx.</p>
<label>Test input <input id="entry" autofocus style="font:24px sans-serif"></label>
<button style="font:24px sans-serif" onclick="document.getElementById('result').textContent='Browser input received: '+document.getElementById('entry').value; document.title='UML_BROWSER_INPUT_PASS'">Submit</button>
<p id="result">Waiting for browser input.</p></body></html>
HTML
runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemd-run --user --unit=lab-firefox --collect --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/home/ga/.Xauthority --setenv=MOZ_ENABLE_WAYLAND=0 --setenv=LP_NUM_THREADS=1 "$browser_binary" --no-remote --profile "$browser_profile" http://127.0.0.1:8080/lab.html
