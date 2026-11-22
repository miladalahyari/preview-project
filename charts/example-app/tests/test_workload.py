"""Render the sample used by the ApplicationSet, including security and isolation."""
from pathlib import Path
import shutil
import subprocess
import unittest
import yaml

CHART = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('helm'), 'Helm is required')
class WorkloadTests(unittest.TestCase):
    def render(self, digest='sha256:' + 'd' * 64):
        return subprocess.run(['helm', 'template', 'sample', str(CHART), '-n', 'test-example-app-100-12345',
                               '-f', str(CHART / 'values-test.yaml'), '--set-string', 'image.digest=' + digest,
                               '--set-string', 'fullnameOverride=example-app-100-12345',
                               '--set-string', 'ingress.hosts[0].host=test-abc-example-app.company.example'],
                              text=True, capture_output=True)

    def test_sample_is_digest_pinned_token_free_and_non_root(self):
        rendered = self.render()
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        objects = {item['kind']: item for item in yaml.safe_load_all(rendered.stdout)}
        self.assertEqual(set(objects), {'Deployment', 'Service', 'ServiceAccount', 'Ingress'})
        pod = objects['Deployment']['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(objects['ServiceAccount']['automountServiceAccountToken'])
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        app = pod['containers'][0]
        self.assertTrue(app['image'].endswith('@sha256:' + 'd' * 64))
        self.assertTrue(app['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(app['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(app['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertEqual(app['ports'][0]['containerPort'], 8080)
        self.assertEqual(app['readinessProbe']['httpGet']['path'], '/health')
        self.assertTrue(all(x['metadata']['annotations']['argocd.argoproj.io/sync-wave'] == '0' for x in objects.values()))
        self.assertEqual(objects['Ingress']['spec']['rules'][0]['host'], 'test-abc-example-app.company.example')

    def test_mutable_or_missing_digest_fails_render(self):
        for value in ('', 'latest', 'sha256:abc'):
            with self.subTest(digest=value):
                self.assertNotEqual(self.render(value).returncode, 0)
