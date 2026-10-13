#!/usr/bin/env python3
"""Bounded authenticated media download. Media errors never poison credentials."""
import os
import sys
sys.dont_write_bytecode = True
sys.path[0] = os.path.expanduser(os.environ.get('FM_STATE', '~/.filament'))
from fm_lib import *
import urllib.parse


def main():
    rotate_logs()
    if len(sys.argv) != 2:
        print('DOWNLOAD_FAILED usage: fm_media.py <mxc_url>')
        return 1
    url = sys.argv[1]
    limit = 20 * 1024 * 1024
    exts = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif',
            'image/webp': 'webp', 'application/pdf': 'pdf', 'text/plain': 'txt', 'video/mp4': 'mp4'}
    temp = None
    try:
        req = urllib.request.Request(EP + '/media?' + urllib.parse.urlencode({'mxc_url': url}),
                                     headers={'Authorization': 'Bearer ' + token()})
        with urllib.request.urlopen(req, timeout=30) as response:
            if int(response.headers.get('Content-Length', '0')) > limit:
                print('TOO_LARGE')
                return 1
            ext = exts.get(response.headers.get_content_type(), 'bin')
            directory = STATE / 'media'
            directory.mkdir(mode=0o700, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=str(directory), suffix='.tmp')
            size = 0
            with os.fdopen(fd, 'wb') as f:
                while True:
                    data = response.read(min(65536, limit + 1 - size))
                    if not data:
                        break
                    size += len(data)
                    if size > limit:
                        print('TOO_LARGE')
                        return 1
                    f.write(data)
                f.flush()
                os.fsync(f.fileno())
            path = directory / (hashlib.sha1(url.encode()).hexdigest() + '.' + ext)
            os.replace(temp, path)
            print(path)
            return 0
    except (OSError, ValueError, http.client.HTTPException) as e:
        reason = 'HTTP ' + str(e.code) if isinstance(e, urllib.error.HTTPError) else type(e).__name__
        log('media ' + reason)
        print('DOWNLOAD_FAILED ' + reason)
        return 1
    finally:
        if temp:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)


if __name__ == '__main__':
    sys.exit(main())
