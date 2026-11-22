#!/usr/bin/env python3
"""Install the checksum-verified scanner used by validation CI on Linux amd64."""
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

VERSION = '8.30.1'
BASE = f'https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/'
NAME = f'gitleaks_{VERSION}_linux_x64.tar.gz'
with urllib.request.urlopen(BASE + NAME, timeout=60) as response:
    archive = response.read()
with urllib.request.urlopen(BASE + f'gitleaks_{VERSION}_checksums.txt', timeout=30) as response:
    checksums = response.read().decode()
expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == NAME)
if hashlib.sha256(archive).hexdigest() != expected:
    raise RuntimeError('Gitleaks release checksum mismatch')
with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as release:
    binary = release.extractfile('gitleaks').read()
path = Path('/usr/local/bin/gitleaks')
path.write_bytes(binary)
path.chmod(0o755)
