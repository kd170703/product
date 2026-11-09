from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, Client
from django.urls import reverse
from monitoring.models import Indicator, Source, Observation, ManualCorrection
from monitoring.services.observations import upsert_observation


class WorkspaceAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user('ui-viewer')
        cls.viewer.user_permissions.add(Permission.objects.get(codename='view_observation', content_type__app_label='monitoring'))
        cls.unassigned = User.objects.create_user('ui-unassigned')
        cls.editor = User.objects.create_user('ui-editor')
        cls.editor.user_permissions.add(*Permission.objects.filter(content_type__app_label='monitoring', codename__in=['view_observation', 'add_observation', 'change_observation', 'add_source', 'change_source', 'add_indicator', 'change_indicator']))
        cls.indicator = Indicator.objects.create(code='UI_TEST', name='Проверочный показатель', section='statistics', unit='чел.', period_kind='flow')
        cls.source = Source.objects.create(name='Проверочный источник', url='https://example.com/publication.xlsx', frequency='manual')
        cls.observation, _ = upsert_observation({'indicator_code': 'UI_TEST', 'value': '0', 'data_state': 'value', 'unit': 'чел.', 'territory': 'Москва', 'period_label': 'I квартал 2026', 'period_start': '2026-01-01', 'period_end': '2026-03-31', 'period_kind': 'flow', 'provenance': {'url': 'https://example.com/publication.xlsx'}}, user=cls.editor, method='manual')

    def test_anonymous_redirects_and_unassigned_account_denied(self):
        response = self.client.get(reverse('monitoring:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/accounts/login/', response.url)
        self.client.force_login(self.unassigned)
        for name in ['dashboard', 'sources', 'imports', 'reports', 'gpt']:
            self.assertEqual(self.client.get(reverse('monitoring:' + name)).status_code, 403, name)

    def test_viewer_can_read_workspace_but_cannot_change_values(self):
        self.client.force_login(self.viewer)
        for name in ['dashboard', 'sources', 'imports', 'reports', 'gpt']:
            self.assertEqual(self.client.get(reverse('monitoring:' + name)).status_code, 200, name)
        detail = reverse('monitoring:observation_detail', args=[self.observation.pk])
        self.assertContains(self.client.get(detail), 'Применяемое значение')
        result = self.client.post(detail, {'action': 'correct', 'value': '7', 'data_state': 'value', 'reason': 'Попытка без прав'})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(ManualCorrection.objects.exists())
        self.assertEqual(self.client.post(reverse('monitoring:enqueue_source', args=[self.source.pk])).status_code, 403)

    def test_mutations_require_csrf_and_manual_correction_keeps_source(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        detail = reverse('monitoring:observation_detail', args=[self.observation.pk])
        self.assertEqual(client.post(detail, {'action': 'correct', 'value': '9', 'data_state': 'value', 'reason': 'Проверено по публикации'}).status_code, 403)
        client.get(detail)
        token = client.cookies['csrftoken'].value
        response = client.post(detail, {'action': 'correct', 'value': '9', 'data_state': 'value', 'reason': 'Проверено по публикации', 'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 302)
        self.observation.refresh_from_db()
        self.assertEqual(self.observation.current_version.value, 0)
        self.assertEqual(self.observation.corrections.get(active=True).value, 9)

    def test_zero_is_displayed_and_missing_source_reference_rejected(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse('monitoring:dashboard')), '<strong class="">0</strong>', html=True)
        response = self.client.post(reverse('monitoring:observation_create'), {'indicator': self.indicator.pk, 'territory': 'Москва', 'period_label': 'II квартал 2026', 'period_start': '2026-04-01', 'period_end': '2026-06-30', 'period_kind': 'flow', 'data_state': 'value', 'value': '5', 'unit': 'чел.', 'reason': 'Импорт вручную'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Укажите ссылку на источник')
        self.assertEqual(Observation.objects.count(), 1)
