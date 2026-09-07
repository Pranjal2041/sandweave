#!/bin/bash
set -euo pipefail
export DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority
browser_window=
for attempt in $(seq 1 60); do
    browser_window=$(xdotool search --onlyvisible --class firefox | head -n 1 || true)
    if [ -n "$browser_window" ]; then break; fi
    sleep 1
done
test -n "$browser_window"
xdotool key --clearmodifiers Escape Escape
activated=0
for attempt in $(seq 1 30); do
    if xdotool windowactivate --sync "$browser_window" windowfocus --sync "$browser_window"; then activated=1; break; fi
    sleep 1
done
test "$activated" = 1
cd /root/lab-compose
docker compose exec -T web sh -c 'cat > /usr/share/nginx/html/lab.html' <<'HTML'
<!doctype html><title>UML browser experiment</title>
<body style="font:24px sans-serif;padding:48px;background:#eef3f7;color:#17324d">
<h1>Linux without KVM</h1><p>Firefox on GNOME, opening a page served by Docker nginx.</p>
<label>Test input <input id="entry" autofocus style="font:24px sans-serif"></label>
<button style="font:24px sans-serif" onclick="const v=document.getElementById('entry').value; document.getElementById('result').textContent='Browser input received: '+v; document.title='UML_BROWSER_INPUT_PASS'; fetch('/__input_pass?value='+encodeURIComponent(v));">Submit</button>
<p id="result">Waiting for browser input.</p></body>
HTML
mkdir -p /home/ga/headful-evidence
for trial in 1 2 3; do
    token="headful-$(date +%s)-$trial"
    xdotool key --clearmodifiers ctrl+l
    sleep 1
    xdotool type --clearmodifiers --delay 50 "http://127.0.0.1:8080/lab.html?trial=$token"
    xdotool key Return
    loaded=0
    for attempt in $(seq 1 20); do
        if docker compose logs --no-color web 2>&1 | grep -Fq "GET /lab.html?trial=$token HTTP"; then loaded=1; break; fi
        sleep 1
    done
    test "$loaded" = 1
    sleep 2
    xdotool mousemove 300 365 click 1
    sleep 0.5
    xdotool key --clearmodifiers ctrl+a
    sleep 0.5
    xdotool type --clearmodifiers --delay 50 "$token"
    sleep 0.5
    xdotool mousemove 578 365 click 1
    passed=0
    for attempt in $(seq 1 20); do
        if docker compose logs --no-color web 2>&1 | grep -F "GET /__input_pass?value=$token HTTP"; then passed=1; break; fi
        sleep 1
    done
    test "$passed" = 1
    import -window root "/home/ga/headful-evidence/input-$trial.png"
    printf 'HEADFUL_INPUT_TRIAL_%s=PASS token=%s\n' "$trial" "$token"
done
main_pid=$(runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show lab-firefox.service -p MainPID --value)
test "$main_pid" -gt 0
ps -p "$main_pid" -o pid,comm,stat,etime,args
sandboxed=0
browser_cgroup=$(runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show lab-firefox.service -p ControlGroup --value)
test -n "$browser_cgroup"
for pid in $(pgrep -f '/firefox-bin -contentproc' || true); do
    test -f "/proc/$pid/status" || continue
    grep -Fxq "0::$browser_cgroup" "/proc/$pid/cgroup" || continue
    awk '/^(Name|Pid|NoNewPrivs|Seccomp):/' "/proc/$pid/status"
    if grep -Eq '^Seccomp:[[:space:]]+2$' "/proc/$pid/status"; then sandboxed=$((sandboxed + 1)); fi
done
test "$sandboxed" -gt 0
printf 'SANDBOXED_CONTENT_PROCESSES=%s\n' "$sandboxed"
journalctl -b --no-pager -g 'clock_gettime failed|segfault' -n 10 || test "$?" = 1
printf 'HEADFUL_INPUT_TEST_PASS\n'
