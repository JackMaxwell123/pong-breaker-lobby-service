"""Small first-party support site. No templates, user content or third-party code."""
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).with_name('public')
FILES = {'/privacy': ('privacy.html', 'text/html; charset=utf-8'),
         '/account': ('account.html', 'text/html; charset=utf-8'),
         '/support.css': ('support.css', 'text/css; charset=utf-8'),
         '/account.js': ('account.js', 'text/javascript; charset=utf-8')}

def response(connection, path):
    entry = FILES.get(path)
    if entry is None:
        return None
    result = connection.respond(HTTPStatus.OK, (ROOT / entry[0]).read_text(encoding='utf-8'))
    del result.headers['Content-Type']
    result.headers['Content-Type'] = entry[1]
    result.headers['Cache-Control'] = 'no-store'
    result.headers['X-Content-Type-Options'] = 'nosniff'
    result.headers['Referrer-Policy'] = 'no-referrer'
    result.headers['X-Frame-Options'] = 'DENY'
    result.headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    result.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=()'
    return result
