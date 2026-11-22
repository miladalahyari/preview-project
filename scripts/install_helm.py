#!/usr/bin/env python3
"""Install a checksum-verified Helm release for validation CI; no cluster access."""
import argparse
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

VERSION = 'v3.22.1'
BASE = f'https://get.helm.sh/helm-{VERSION}-linux-amd64.tar.gz'
with urllib.request.urlopen(BASE, timeout=60) as response:
    archive = response.read()
with urllib.request.urlopen(BASE + '.sha256sum', timeout=30) as response:
    expected = response.read().decode().split()[0]
if hashlib.sha256(archive).hexdigest() != expected:
    raise RuntimeError('Helm release checksum mismatch')
with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as release:
    binary = release.extractfile('linux-amd64/helm').read()
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=Path('/usr/local/bin/helm'))
path = parser.parse_args().output
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(binary)
path.chmod(0o755)
