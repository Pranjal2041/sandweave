#!/bin/bash
set -euxo pipefail
profile=/home/ga/firefox-sandbox-test
install -d -o ga -g ga "$profile"
rm -f /home/ga/firefox-sandbox-test.png
# All Firefox sandboxes remain enabled. Limit Mesa's software-rendering threads.
runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus LP_NUM_THREADS=1 timeout -k 5s 90s /opt/firefox/firefox --headless --no-remote --profile "$profile" --screenshot /home/ga/firefox-sandbox-test.png http://127.0.0.1:8080/
test -s /home/ga/firefox-sandbox-test.png
printf 'FIREFOX_SANDBOX_HEADLESS_PASS\n'
