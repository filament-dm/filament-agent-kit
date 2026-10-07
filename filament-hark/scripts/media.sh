#!/usr/bin/env bash
STATE=${FILAMENT_STATE:-/workspace/filament}
# shellcheck source=package/scripts/restart_listener.sh
source "$STATE/restart_listener.sh"
if ! mkdir -p "$STATE" || ! json_helper rotate; then echo 'DOWNLOAD_FAILED initialisation'; exit 1; fi
python3 - "$STATE" "$@" <<'PY'
import hashlib, os, pathlib, subprocess, sys, tempfile
from urllib.parse import quote

try:
    if len(sys.argv) != 3 or not sys.argv[2]: raise ValueError('expected mxc_url')
    state, url = pathlib.Path(os.path.abspath(sys.argv[1])), sys.argv[2]
    media = state / 'media'
    media.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.download-', dir=media) as temporary:
        body = pathlib.Path(temporary) / 'body'
        headers = pathlib.Path(temporary) / 'headers'
        result = subprocess.run(['curl', '-sS', '-m', '60', '-f', '-D', str(headers),
                                 '-o', str(body), 'https://api.filament.dm/mcp/agents/media?mxc_url=' + quote(url, safe='')],
                                capture_output=True, timeout=60)
        if result.returncode:
            raise ValueError(result.stderr.decode('utf-8', errors='replace').strip() or 'curl exit ' + str(result.returncode))
        # Reset at each status line: only the final response determines type/size.
        fields = {}
        for line in headers.read_text(encoding='iso-8859-1').splitlines():
            if line.startswith('HTTP/'): fields = {}
            elif ':' in line:
                key, value = line.split(':', 1)
                fields[key.lower()] = value.strip()
        length = int(fields.get('content-length', '0'))
        if max(length, body.stat().st_size) > 20 * 1024 * 1024:
            print('TOO_LARGE')
            sys.exit(1)
        extensions = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
                      'image/webp': 'webp', 'application/pdf': 'pdf', 'text/plain': 'txt', 'video/mp4': 'mp4'}
        ext = extensions.get(fields.get('content-type', '').split(';')[0].strip().lower(), 'bin')
        path = media / (hashlib.sha1(url.encode('utf-8')).hexdigest() + '.' + ext)
        os.replace(body, path)
        print(path)
except (OSError, ValueError, subprocess.TimeoutExpired) as error:
    print('DOWNLOAD_FAILED ' + ' '.join(str(error).split())[:1900])
    sys.exit(1)
PY
