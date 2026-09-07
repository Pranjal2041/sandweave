#!/usr/bin/env python3
"""Disposable real GTK application for input delivery and screenshot acceptance."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import threading
import time
import uuid
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

state = {'nonce': uuid.uuid4().hex, 'pid': os.getpid(), 'events': [], 'clicks': 0,
         'text': '', 'color': [30, 90, 180], 'paint_count': 0}
window = Gtk.Window(title='Fast I/O acceptance')
window.set_decorated(False)
window.set_default_size(900, 650)
window.move(80, 90)
fixed = Gtk.Fixed(); window.add(fixed)
label = Gtk.Label(label='Fast I/O: real GTK input and pixels')
fixed.put(label, 30, 25)
entry = Gtk.Entry(); entry.set_size_request(800, 50); fixed.put(entry, 30, 75)
entry.connect('changed', lambda widget: state.update(text=widget.get_text()))
canvas = Gtk.EventBox(); canvas.set_size_request(800, 430); fixed.put(canvas, 30, 160)
canvas.add(Gtk.Label(label='Click / drag / scroll here'))
style = Gtk.CssProvider()
canvas.get_style_context().add_provider(style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def paint():
    style.load_from_data(('* {background-color: rgb(%d,%d,%d); color: white;}' % tuple(state['color'])).encode())
    state['paint_count'] += 1


paint()
canvas.set_can_focus(True)
canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                  Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                  Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.KEY_RELEASE_MASK)


def record(widget, event):
    row = {'type': event.type.value_nick, 'state': int(event.state), 'time': time.monotonic_ns()}
    if event.type in (Gdk.EventType.KEY_PRESS, Gdk.EventType.KEY_RELEASE):
        row.update(key=Gdk.keyval_name(event.keyval), hardware=event.hardware_keycode)
    elif event.type == Gdk.EventType.SCROLL:
        row.update(direction=event.direction.value_nick)
    else:
        row.update(x=event.x, y=event.y)
        if event.type in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE,
                          Gdk.EventType.DOUBLE_BUTTON_PRESS, Gdk.EventType.TRIPLE_BUTTON_PRESS):
            row['button'] = event.button
    state['events'].append(row)
    state['events'] = state['events'][-2000:]
    if widget is canvas and event.type == Gdk.EventType.BUTTON_PRESS:
        canvas.grab_focus()
        state['clicks'] += 1
        state['color'] = [180, 60, 30] if state['clicks'] % 2 else [30, 90, 180]
        paint()
    return False


for name in ('button-press-event', 'button-release-event', 'motion-notify-event',
             'scroll-event', 'key-press-event', 'key-release-event'):
    canvas.connect(name, record)
entry.connect('key-press-event', record)
entry.connect('key-release-event', record)


window.connect('destroy', Gtk.main_quit)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/gpu':
            body = GPU_PAGE.encode()
            self.send_response(200); self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        body = json.dumps(state).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass

    def do_POST(self):
        state['gpu'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(204); self.end_headers()


GPU_PAGE = '''<!doctype html><meta charset="utf-8"><style>
html,body {margin:0;overflow:hidden;background:#111;color:white;font:24px sans-serif}
canvas{width:100vw;height:80vh} p{margin:12px}</style>
<canvas id="canvas" width="1280" height="640"></canvas><p id="label">GPU fast I/O test</p>
<script>
const canvas=document.getElementById('canvas'), label=document.getElementById('label');
const gl=canvas.getContext('webgl'); const ext=gl.getExtension('WEBGL_debug_renderer_info');
const renderer=ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);
let frames=0, clicks=0, red=false;
canvas.onmousedown=()=>{clicks++;red=!red;};
function render(){let rgb=red?[180,60,30]:[30,90,180];
gl.clearColor(...rgb.map(x=>x/255),1);gl.clear(gl.COLOR_BUFFER_BIT);frames++;
label.textContent='GPU fast I/O | '+renderer+' | frame '+frames+' | clicks '+clicks;
if(frames%5===0)fetch('/gpu',{method:'POST',body:JSON.stringify({renderer,frames,clicks,color:rgb})});
requestAnimationFrame(render);};render();
</script>'''


threading.Thread(target=HTTPServer(('0.0.0.0', 8000), Handler).serve_forever, daemon=True).start()
window.show_all(); window.present(); entry.grab_focus()
Gtk.main()
