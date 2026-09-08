"""Cookie snapshots and redacted diagnostics. Cookie presence is not authentication."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading
import time

AUTH_NAMES = {'SAPISID', '__Secure-1PAPISID', '__Secure-3PAPISID', 'SID', '__Secure-1PSID', '__Secure-3PSID'}


def redact(message, limit=600):
    text = str(message or '').replace('\x00', '')
    text = re.sub(r'https?://[^\s\"\'<>]+', '[url]', text, flags=re.I)
    text = re.sub(r'\S*[?&](?:expire|itag|source|mime|sparams|lsparams|bui|spc|sig|lsig|pot|n)=\S*', '[media-query]', text, flags=re.I)
    text = re.sub(r'(?i)(?:cookie|authorization)\s*[:=]\s*[^\r\n]+', '[credential-header]', text)
    text = re.sub(r'''(?ix)(\b(?:po[_ -]?token|integrity[_ -]?token|pot|sig|lsig|spc)\b)[\\'"\s]*[:=][\\'"\s]*[^\s,;\)\]}>]+''', r'\1=<redacted>', text)
    text = re.sub(r'[A-Za-z0-9_+/=%-]{64,}', '[opaque-value]', text)
    return text[:limit]


def failure_code(message, stage='extract'):
    low = str(message).lower()
    if any(x in low for x in ('sign in to confirm', 'login_required', 'not a bot')):
        return 'SOURCE_ACCESS_DENIED'
    if '403' in low or 'forbidden' in low:
        return 'MEDIA_HTTP_403' if stage in {'download', 'transcode', 'media-fetch'} else 'SOURCE_HTTP_403'
    if '429' in low or 'too many requests' in low:
        return 'SOURCE_RATE_LIMITED'
    if 'timed out' in low or 'timeout' in low:
        return 'SOURCE_TIMEOUT'
    if 'requested format' in low or 'no usable source' in low:
        return 'SOURCE_FORMAT_UNAVAILABLE'
    if 'live broadcasts' in low:
        return 'LIVE_SOURCE_NOT_SUPPORTED'
    return 'SOURCE_ACQUISITION_FAILED'


class SourceAttemptError(RuntimeError):
    def __init__(self, message, *, stage='extract', evidence=None, diagnostics=None, code=None):
        super().__init__(redact(message))
        self.stage, self.code = stage, code or failure_code(message, stage)
        self.evidence = evidence or {}
        self.diagnostics = [redact(x, 220) for x in (diagnostics or [])][-3:]


def inspect_cookies(path):
    result = {'present': False, 'formatValid': False, 'youtubeCookieCount': 0,
              'expiredYoutubeCookieCount': 0, 'activeAuthCookieFieldsPresent': False,
              'status': 'missing', 'authenticationVerified': None}
    try:
        source = Path(path)
        if not source.is_file():
            return result
        result['present'] = True
        if source.stat().st_size > 1024 * 1024:
            result['status'] = 'file_too_large'
            return result
        rows = source.read_text(encoding='utf-8-sig').splitlines()
        if not rows or not re.search(r'(?:Netscape )?HTTP Cookie File', rows[0], re.I):
            result['status'] = 'invalid_netscape_header'
            return result
        active = set()
        for number, row in enumerate(rows, 1):
            if row.startswith('#HttpOnly_'):
                row = row[len('#HttpOnly_'):]
            elif not row.strip() or row.startswith('#'):
                continue
            parts = row.split('\t')
            if len(parts) != 7 or parts[1] not in {'TRUE', 'FALSE'} or parts[3] not in {'TRUE', 'FALSE'}:
                result.update(status='invalid_netscape_row', invalidLine=number)
                return result
            domain, _, _, _, expires, name, value = parts
            try:
                expiry = int(expires) if expires else 0
            except ValueError:
                result.update(status='invalid_expiry', invalidLine=number)
                return result
            domain = domain.lstrip('.').lower()
            if domain != 'youtube.com' and not domain.endswith('.youtube.com'):
                continue
            result['youtubeCookieCount'] += 1
            # yt-dlp treats a Netscape zero expiry as a session cookie.
            if expiry > 0 and expiry <= time.time():
                result['expiredYoutubeCookieCount'] += 1
            elif value:
                active.add(name)
        result['formatValid'] = True
        result['activeAuthCookieFieldsPresent'] = bool(active & AUTH_NAMES)
        result['status'] = 'fields_present_unverified' if result['activeAuthCookieFieldsPresent'] else 'no_active_auth_fields'
    except (OSError, UnicodeError):
        result['status'] = 'unreadable'
    return result


class CookieStore:
    """Refresh changed secrets, preserve server rotation, never write the secret."""
    def __init__(self, source, runtime):
        self.source, self.runtime = Path(source), Path(runtime)
        self._digest = None
        self._lock = threading.Lock()

    def _write(self, data):
        self.runtime.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='.veeb-cookie-', dir=self.runtime.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            os.chmod(temp, 0o600)
            os.replace(temp, self.runtime)
        finally:
            Path(temp).unlink(missing_ok=True)

    def snapshot(self):
        with self._lock:
            audit = inspect_cookies(self.source)
            if not audit['formatValid'] or not audit['activeAuthCookieFieldsPresent']:
                return None
            raw = self.source.read_bytes()
            digest = hashlib.sha256(raw).digest()
            if digest != self._digest or not self.runtime.is_file():
                normal = raw.decode('utf-8-sig').replace('\r\n', '\n').rstrip('\n') + '\n'
                self._write(normal.encode())
                self._digest = digest
            return str(self.runtime)

    def accept_update(self, expected_digest, candidate):
        with self._lock:
            if not self.runtime.is_file() or hashlib.sha256(self.runtime.read_bytes()).hexdigest() != expected_digest:
                return False
            if not inspect_cookies(candidate)['formatValid']:
                return False
            self._write(Path(candidate).read_bytes())
            return True


def reported_login_state(text):
    flags = re.findall(r'"LOGGED_IN"\s*:\s*(true|false)', str(text))
    return flags[0] == 'true' if flags and len(set(flags)) == 1 else None


class DiagnosticLog:
    def __init__(self):
        self.lines = []
        self.evidence = {'sourceSelected': False}

    def record(self, message, warning=False):
        raw = str(message)
        low = raw.lower()
        if 'invoking ' in low and ' downloader' in low:
            self.evidence.update(sourceSelected=True, stage='download')
        if 'challenge solving failed' in low or 'signature extraction failed' in low:
            self.evidence['jsChallengeFailed'] = True
        if 'retrieved a gvs po token' in low:
            self.evidence['gvsTokenReturned'] = True
        if 'retrieved a player po token' in low:
            self.evidence['playerTokenReturned'] = True
        if '[pot:cache]' in low or 'potokenresponse(' in low:
            return
        keep = warning or any(x in low for x in ('player api', 'js runtimes:', 'challenge solving',
            'signature extraction', 'no supported javascript', 'formats have been skipped',
            'only images', 'requested format', 'http error', 'unable to download',
            'sign in to confirm', 'po token providers:', 'retrieved a gvs po token', 'sabr'))
        if keep:
            clean = redact(raw, 280)
            if clean not in self.lines:
                self.lines.append(clean)
                self.lines = self.lines[-10:]

    def debug(self, message): self.record(message)
    def info(self, message): self.record(message)
    def warning(self, message): self.record(message, True)
    def error(self, message): self.record(message, True)

    def progress(self, data):
        if data.get('status') in {'downloading', 'finished'}:
            self.evidence.update(sourceSelected=True, stage='download')
        if data.get('downloaded_bytes') is not None:
            self.evidence['downloadedBytes'] = int(data['downloaded_bytes'])
        info = data.get('info_dict') or {}
        for key, field in [('format_id', 'sourceFormat'), ('protocol', 'sourceProtocol')]:
            if info.get(key) is not None:
                self.evidence[field] = str(info[key])[:60]
