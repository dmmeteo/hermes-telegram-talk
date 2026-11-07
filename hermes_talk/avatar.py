"""Bounded bot-photo refresh owned by one configured server."""
import json
import re
import threading
import time
import urllib.request

MAX_PHOTO_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def telegram_photo(token, bot_id):
    opener = urllib.request.build_opener(_NoRedirect)

    def read(path, limit):
        with opener.open('https://api.telegram.org/' + path, timeout=4) as response:
            body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError('photo limit')
        return body

    def api(method, fields):
        from urllib.parse import urlencode
        result = json.loads(read(f'bot{token}/{method}?' + urlencode(fields), 32768))
        if result.get('ok') is not True:
            raise ValueError('photo unavailable')
        return result['result']

    photos = api('getUserProfilePhotos', {'user_id': bot_id, 'limit': 1}).get('photos', [])
    if not photos:
        return None
    photo = max(photos[0], key=lambda p: p.get('width', 0) * p.get('height', 0))
    path = api('getFile', {'file_id': photo['file_id']})['file_path']
    if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_/-]+\.(?:jpg|jpeg|png)', path) or '..' in path:
        raise ValueError('photo path')
    body = read(f'file/bot{token}/{path}', MAX_PHOTO_BYTES)
    if body.startswith(b'\xff\xd8\xff'):
        return body, 'image/jpeg'
    if body.startswith(b'\x89PNG\r\n\x1a\n'):
        return body, 'image/png'
    raise ValueError('photo format')


class BotAvatar:
    def __init__(self, token, bot_id, fallback, fetch=telegram_photo, clock=time.monotonic):
        self._token, self._bot_id = token, bot_id
        self._fallback = fallback
        self._fetch, self._clock = fetch, clock
        self._photo = None
        self._next = 0
        self._busy = False
        self._lock = threading.Lock()

    def image(self):
        with self._lock:
            if not self._busy and self._clock() >= self._next:
                self._busy = True
                threading.Thread(target=self._refresh, daemon=True).start()
            return self._photo or self._fallback

    def _refresh(self):
        delay = 300
        try:
            photo = self._fetch(self._token, self._bot_id)
            if photo is not None:
                body, kind = photo
                if kind not in ('image/png', 'image/jpeg') or not 0 < len(body) <= MAX_PHOTO_BYTES:
                    raise ValueError('photo format')
            with self._lock:
                self._photo = photo
        except Exception:
            delay = 30
        finally:
            with self._lock:
                self._next = self._clock() + delay
                self._busy = False
