"""Distribution wiring: use the exact checked-in files, not a live cluster."""
import importlib.util
import json
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_placeholder_catalog_is_disabled_and_wiring_matches(self):
        catalog = json.loads((ROOT / 'catalog/example-app.json').read_text())
        if any(catalog[key]['approvedRevision'] == '0' * 40 for key in ('chart', 'provisioning')):
            self.assertFalse(catalog['enabled'])
            self.assertFalse(catalog['provisioning']['approved'])
            self.assertFalse(catalog['provisioning']['dependenciesReviewed'])
        config = json.loads((ROOT / 'config/platform.json').read_text())
        self.assertEqual(catalog['chart']['repository'], config['gitopsRepository'])
        self.assertEqual(catalog['chart']['path'], config['workloadChartPath'])
        self.assertEqual(catalog['provisioning']['path'], config['bootstrapChartPath'])

    def test_image_and_project_configuration_agree_across_delivery_components(self):
        catalog = json.loads((ROOT / 'catalog/example-app.json').read_text())
        workload = yaml.safe_load((ROOT / 'charts/example-app/values-test.yaml').read_text())
        self.assertEqual(workload['image']['repository'], catalog['image']['repository'])
        objects = list(yaml.safe_load_all((ROOT / 'platform/bootstrap/controllers/registry-sync.yaml').read_text()))
        deployment = next(obj for obj in objects if obj['kind'] == 'Deployment')
        controller = deployment['spec']['template']['spec']['containers'][0]
        source_id = next(item['value'] for item in controller['env'] if item['name'] == 'SOURCE_PROJECT_ID')
        self.assertEqual(source_id, catalog['source']['projectId'])
        lock = json.loads((ROOT / 'tools/image-lock.json').read_text())['image']
        self.assertEqual(controller['image'], lock)
        reaper = next(obj for obj in yaml.safe_load_all((ROOT / 'platform/bootstrap/controllers/cluster-reaper.yaml').read_text()) if obj['kind'] == 'CronJob')
        self.assertEqual(reaper['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'], lock)

    def test_generated_controller_matches_reviewed_source_and_image(self):
        spec = importlib.util.spec_from_file_location('registry_renderer', ROOT / 'platform/registry-sync/render_manifests.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual = (ROOT / 'platform/bootstrap/controllers/registry-sync.yaml').read_text()
        self.assertEqual(actual, module.render())

    def test_source_trigger_keeps_metadata_forwarding_narrow_and_stop_opt_in(self):
        config = yaml.safe_load((ROOT / 'examples/example-app/.gitlab-ci.yml').read_text())
        template = config['.preview-trigger']
        self.assertFalse(template['inherit']['variables'])
        self.assertEqual(template['trigger']['project'], json.loads((ROOT / 'config/platform.json').read_text())['centralProjectPath'])
        self.assertEqual(template['trigger']['branch'], 'main')
        self.assertFalse(template['trigger']['forward']['pipeline_variables'])
        self.assertEqual(set(template['variables']), {'PREVIEW_SOURCE_PROJECT_ID', 'PREVIEW_SOURCE_PIPELINE_ID', 'PREVIEW_SOURCE_SHA', 'PREVIEW_SOURCE_TAG'})
        self.assertEqual(config['stop:test']['rules'], [{'if': '$CI_PIPELINE_SOURCE == "web" && $TEST_PREVIEW_ACTION == "stop"'}])
        self.assertIn('-u ARGOCD_AUTH_TOKEN', config['build:test']['script'][0])
        self.assertEqual(config['build:test']['variables']['DOCKER_HOST'], 'tcp://docker:2376')

    def test_state_mutations_share_lock_and_readiness_waits_outside_it(self):
        config = yaml.safe_load((ROOT / '.gitlab-ci.yml').read_text())
        for name in ('prepare', 'register', 'activate', 'stop', 'stop:requested', 'stop:standalone'):
            self.assertEqual(config[name]['resource_group'], 'preview-state')
        self.assertNotIn('resource_group', config['ready'])
        self.assertEqual(config['stop:requested']['script'], ['python3 scripts/preview.py stop-request'])

    def test_applicationset_uses_bundled_charts_and_foreground_deletion(self):
        appset = yaml.safe_load((ROOT / 'platform/bootstrap/applications/main.yaml').read_text())
        template = appset['spec']['template']
        self.assertIn('resources-finalizer.argocd.argoproj.io', template['metadata']['finalizers'])
        self.assertEqual([s['path'] for s in template['spec']['sources']], ['charts/previews', 'charts/example-app'])
        self.assertFalse(appset['spec']['syncPolicy']['preserveResourcesOnDeletion'])
        self.assertIn('RespectIgnoreDifferences=true', template['spec']['syncPolicy']['syncOptions'])
        self.assertEqual(template['spec']['ignoreDifferences'][0]['jsonPointers'], ['/data'])

    def test_bootstrap_has_references_without_secret_values(self):
        for path in (ROOT / 'platform/bootstrap').rglob('*.yaml'):
            for item in yaml.safe_load_all(path.read_text()):
                self.assertNotEqual(item['kind'], 'Secret', path)
