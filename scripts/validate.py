#!/usr/bin/env python3
"""Offline validation. Does not contact GitLab, a registry, Argo CD or Kubernetes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    value = {}
    for key, item in node.value:
        name = loader.construct_object(key, deep=deep)
        if name in value:
            raise ValueError(f"Duplicate YAML key: {name}")
        value[name] = loader.construct_object(item, deep=deep)
    return value


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def main():
    for path in ROOT.rglob('*'):
        if not path.is_file() or any(part in {'.git', '.venv', '__pycache__'} for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix == '.json':
            json.loads(path.read_text())
        elif path.suffix in {'.yaml', '.yml'} and 'templates' not in path.parts:
            list(yaml.load_all(path.read_text(), Loader=UniqueLoader))
    for directory in ('tests', 'tools/tests', 'platform/registry-sync/tests',
                      'charts/previews/tests', 'examples/example-app', 'charts/example-app/tests'):
        subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', directory,
                        '-p', 'test_*.py'], cwd=ROOT, check=True)
    if not shutil.which('helm'):
        raise RuntimeError('Install Helm 3 to complete chart validation')
    subprocess.run(['helm', 'lint', 'charts/previews', '-f', 'charts/previews/examples/example-app.yaml'], cwd=ROOT, check=True)
    subprocess.run(['helm', 'lint', 'charts/example-app', '-f', 'charts/example-app/values-test.yaml',
                    '--set-string', 'image.digest=sha256:' + 'a' * 64], cwd=ROOT, check=True)
    print('Offline checks passed. Live deployment acceptance is still required.')


if __name__ == '__main__':
    main()
