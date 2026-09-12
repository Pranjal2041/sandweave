"""Browser routes share the controller listener, with separate read-only sessions."""
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .monitor import Monitor

ASSETS = Path(__file__).with_name('web')
SESSION_SECONDS = 8 * 3600


class Dashboard:
    def __init__(self, controller, token, **settings):
        self.monitor = Monitor(controller, **settings)
        self.token = token
        self.lock = threading.RLock()
        self.sessions, self.tickets, self.failures = {}, {}, {}
        self.requests = threading.BoundedSemaphore(8)
        # Cookies are scoped by cluster, since cookies don't distinguish ports.
        self.cookie_name = 'sw_monitor_' + controller.id[:16]

    def ticket(self):
        with self.lock:
            self._prune()
            if len(self.tickets) >= 256:
                raise RuntimeError('too many unused dashboard links; wait a minute')
            value = secrets.token_urlsafe(32)
            self.tickets[self._hash(value)] = time.time() + 60
        return {'ticket': value, 'expires_in': 60}

    @staticmethod
    def _hash(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def _prune(self):
        now = time.time()
        for table in (self.sessions, self.tickets):
            for key in list(table):
                if table[key] < now:
                    table.pop(key, None)
        for key in list(self.failures):
            if self.failures[key][0] < now - 60:
                self.failures.pop(key, None)

    def session(self, handler):
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get('Cookie', ''))
            value = cookie.get(self.cookie_name)
            key = self._hash(value.value) if value else ''
        except Exception:
            return False
        with self.lock:
            return self.sessions.get(key, 0) > time.time()

    def cookie(self, handler, value, age):
        # Behind a TLS-terminating proxy, a same-origin HTTPS login should still
        # issue a Secure cookie. An arbitrary forwarded header is never trusted.
        secure = getattr(handler, 'secure', False) or isinstance(handler.connection, ssl.SSLSocket) or handler.headers.get('Origin', '').startswith('https://')
        return f'{self.cookie_name}={value}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict' + ('; Secure' if secure else '')

    def reply(self, handler, status, body, content_type='application/json', headers=None):
        if content_type == 'application/json':
            body = json.dumps(body, allow_nan=False, ensure_ascii=True).encode()
        elif isinstance(body, str):
            body = body.encode()
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('X-Frame-Options', 'DENY')
        handler.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        if handler.command != 'HEAD':
            handler.wfile.write(body)

    def handle(self, handler):
        parsed = urlsplit(handler.path)
        path = parsed.path
        if path not in ('/', '/metrics') and not path.startswith('/dashboard'):
            return False
        handler.connection.settimeout(10)
        try:
            if handler.command in ('GET', 'HEAD') and path in ('/', '/dashboard'):
                # Relative redirects preserve reverse-proxy prefixes.
                self.reply(handler, 303, {}, headers={'Location': 'dashboard/' if path == '/' else 'dashboard/'})
                return True
            if handler.command in ('GET', 'HEAD') and path in ('/dashboard/', '/dashboard/app.js', '/dashboard/style.css'):
                name, mime = {'/dashboard/': ('index.html', 'text/html; charset=utf-8'),
                              '/dashboard/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                              '/dashboard/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
                self.reply(handler, 200, (ASSETS / name).read_bytes(), mime)
                return True
            if handler.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.reply(handler, 403, {'error': 'Cross-site requests are not allowed.'})
                return True
            origin = handler.headers.get('Origin')
            if origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc != handler.headers.get('Host')):
                self.reply(handler, 403, {'error': 'Request origin does not match this controller.'})
                return True
            if handler.command == 'POST' and path == '/dashboard/api/session':
                self.login(handler)
                return True
            if path == '/metrics':
                credential = handler.headers.get('Authorization', '').removeprefix('Bearer ')
                authenticated = hmac.compare_digest(credential, self.token) or hmac.compare_digest(handler.headers.get('X-Sandweave-Token', ''), self.token)
            else:
                authenticated = self.session(handler)
            if not authenticated:
                handler.close_connection = True
                self.reply(handler, 401, {'error': 'Sign in to view this cluster.'})
                return True
            if handler.command == 'POST' and path == '/dashboard/api/logout':
                cookie = SimpleCookie(handler.headers.get('Cookie', ''))
                with self.lock:
                    self.sessions.pop(self._hash(cookie[self.cookie_name].value), None)
                self.reply(handler, 200, {}, headers={'Set-Cookie': self.cookie(handler, '', 0)})
                return True
            if handler.command != 'GET':
                handler.close_connection = True
                self.reply(handler, 405, {'error': 'Monitoring endpoints are read-only.'}, headers={'Allow': 'GET'})
                return True
            if not self.requests.acquire(blocking=False):
                self.reply(handler, 503, {'error': 'Monitoring is busy. Retry shortly.'}, headers={'Retry-After': '2'})
                return True
            try:
                query = {k: v[0] for k, v in parse_qs(parsed.query, max_num_fields=20).items()}
                if path == '/metrics':
                    self.reply(handler, 200, self.monitor.prometheus(), 'text/plain; version=0.0.4; charset=utf-8')
                else:
                    function = {'overview': self.monitor.overview, 'resources': self.monitor.listing,
                                'detail': self.monitor.detail, 'history': self.monitor.history,
                                'events': self.monitor.events, 'logs': self.monitor.logs}.get(path.removeprefix('/dashboard/api/'))
                    if not path.startswith('/dashboard/api/') or function is None:
                        self.reply(handler, 404, {'error': 'Monitoring endpoint not found.'})
                    else:
                        self.reply(handler, 200, function(**query))
            finally:
                self.requests.release()
        except (ValueError, TypeError) as error:
            handler.close_connection = True
            self.reply(handler, 400, {'error': str(error)})
        except FileNotFoundError as error:
            self.reply(handler, 404, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # No transport credentials or internal tracebacks in HTTP errors.
            import logging
            logging.getLogger(__name__).exception('Dashboard request failed')
            self.reply(handler, 503, {'error': 'The requested data is unavailable. Check controller activity and retry.'})
        return True

    def login(self, handler):
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('sign in with a JSON request')
        length = int(handler.headers.get('Content-Length', '-1'))
        if not 1 <= length <= 4096:
            raise ValueError('invalid sign-in request size')
        body = handler.rfile.read(length)
        if len(body) != length:
            raise ValueError('incomplete sign-in request')
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError('sign-in request must be a JSON object')
        ip = handler.client_address[0]
        with self.lock:
            self._prune()
            failure = self.failures.get(ip, (time.time(), 0))
            if failure[1] >= 20 or len(self.failures) >= 1024 and ip not in self.failures:
                self.reply(handler, 429, {'error': 'Too many sign-in attempts. Try again in a minute.'}, headers={'Retry-After': '60'})
                return
            valid = False
            ticket = value.get('ticket')
            if isinstance(ticket, str):
                valid = self.tickets.pop(self._hash(ticket), 0) > time.time()
            credential = value.get('token')
            if isinstance(credential, str):
                valid = valid or hmac.compare_digest(credential.encode(), self.token.encode())
            if not valid:
                self.failures[ip] = (failure[0], failure[1] + 1)
                self.reply(handler, 401, {'error': 'The token is invalid or the sign-in link has expired.'})
                return
            if len(self.sessions) >= 1024:
                self.reply(handler, 503, {'error': 'Dashboard session limit reached. Sign out of an unused session.'})
                return
            token = secrets.token_urlsafe(32)
            self.sessions[self._hash(token)] = time.time() + SESSION_SECONDS
            self.failures.pop(ip, None)
        self.reply(handler, 200, {'expires_in': SESSION_SECONDS}, headers={'Set-Cookie': self.cookie(handler, token, SESSION_SECONDS)})
