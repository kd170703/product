"""Regression scenarios for the requested November interface changes.

Every fixture is synthetic; no owner's workbook or mascot bytes are embedded.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, SimpleTestCase, override_settings
from django.urls import reverse

from monitoring.forms import ObservationForm
from monitoring.models import DataIssue, Indicator, Observation, ObservationVersion, Publication, ReportSnapshot, Source
from monitoring.services.charts import build_chart_specs
from monitoring.services.importer import create_preview, confirm_batch
from monitoring.services.observations import upsert_observation
from monitoring.services.selectors import filtered_observations
from monitoring.services.workers import apply_payload


class TargetedWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.viewer = User.objects.create_user('target-viewer')
        cls.viewer.user_permissions.add(Permission.objects.get(content_type__app_label='monitoring', codename='view_observation'))
        cls.editor = User.objects.create_user('target-editor')
        cls.editor.user_permissions.add(*Permission.objects.filter(content_type__app_label='monitoring', codename__in=['view_observation','add_observation','add_indicator','change_indicator','delete_indicator']))
        cls.restricted_editor = User.objects.create_user('target-restricted-editor')
        cls.restricted_editor.user_permissions.add(*Permission.objects.filter(content_type__app_label='monitoring', codename__in=['view_observation','add_observation','change_indicator']))
        cls.unassigned = User.objects.create_user('target-no-role')

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        setting = override_settings(MEDIA_ROOT=Path(temporary.name))
        setting.enable()
        self.addCleanup(setting.disable)
        self.indicator = self.make_indicator('TARGET-PRIMARY')

    def make_indicator(self, code, **changes):
        fields = {'code':code,'name':'Синтетический показатель '+code,'section':'statistics','unit':'ед.','period_kind':'flow'}
        fields.update(changes)
        return Indicator.objects.create(**fields)

    def record(self, indicator=None, **changes):
        indicator = indicator or self.indicator
        fields = {'indicator_code':indicator.code,'value':'12','data_state':'value','unit':indicator.unit,'territory':'Тестовая территория','period_label':'I квартал 2024','period_start':'2024-01-01','period_end':'2024-03-31','period_kind':'flow','provenance':{'url':'https://example.org/synthetic.xlsx','cell':'D3'}}
        fields.update(changes)
        return fields

    def save_record(self, indicator=None, **changes):
        return upsert_observation(self.record(indicator,**changes),method='auto')[0]

    def test_quarter_finds_raw_heading_and_closing_stock_without_inventing_dates(self):
        raw = self.save_record(period_label='IV кв. 2024 / 2024',period_start=None,period_end=None,period_kind='unknown')
        closing = self.save_record(period_label='на 01.01.2025',period_start='2025-01-01',period_end='2025-01-01',period_kind='stock')
        self.save_record(period_label='2024 год',period_start='2024-01-01',period_end='2024-12-31')
        self.save_record(period_label='I полугодие 2024',period_start='2024-01-01',period_end='2024-06-30')
        self.save_record(period_label='IV кв. 2025 / 2025',period_start=None,period_end=None,period_kind='unknown')
        rows = filtered_observations({'period_mode':'quarter','period_year':'2024','period_quarter':'4'})
        self.assertEqual({r['id'] for r in rows},{raw.pk,closing.pk})
        raw.refresh_from_db();closing.refresh_from_db()
        self.assertIsNone(raw.period_start)
        self.assertIsNone(raw.period_end)
        self.assertEqual(closing.period_end.isoformat(),'2025-01-01')

    def test_period_query_is_understood_by_dashboard_and_chart_api(self):
        target = self.save_record()
        self.save_record(period_label='I квартал 2025',period_start='2025-01-01',period_end='2025-03-31')
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('monitoring:dashboard'),{'q':'1 квартал 2024 года'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['stats']['observations'],1)
        self.assertEqual(response.context['filters']['period_year'],'2024')
        payload = self.client.get(reverse('monitoring:chart_data'),{'q':'I кв. 2024'}).json()
        self.assertEqual(payload['observations_count'],1)
        self.assertEqual(payload['charts'][0]['observation_ids'],[target.pk])

    def test_report_link_keeps_section_and_quarter_from_dashboard(self):
        self.client.force_login(self.viewer)
        response=self.client.get(reverse('monitoring:section',args=['fintest']),{'period_mode':'quarter','period_year':'2024','period_quarter':'4'})
        from urllib.parse import parse_qs
        query=parse_qs(response.context['report_query'])
        self.assertEqual(query['section'],['fintest'])
        self.assertEqual(query['period_year'],['2024'])
        self.assertEqual(query['period_quarter'],['4'])

    def test_chart_builder_carries_the_applied_section_from_section_route(self):
        sociology = self.make_indicator('TARGET-SOCIOLOGY-DEMO', section='sociology', name='Демо социология', unit='%')
        fintest = self.make_indicator('TARGET-FINTEST-DEMO', section='fintest', name='Демо финтест', unit='%')
        target = self.save_record(sociology)
        self.save_record(fintest)
        self.client.force_login(self.viewer)
        response = self.client.get(reverse('monitoring:section', args=['sociology']), {'q':'Демо'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['filters']['section'], 'sociology')
        self.assertContains(response, 'data-chart-section="sociology"')
        payload = self.client.get(reverse('monitoring:chart_data'), {'q':'Демо', 'section':'sociology'}).json()
        self.assertEqual(payload['observations_count'], 1)
        self.assertEqual({chart['section'] for chart in payload['charts']}, {'sociology'})
        self.assertEqual(payload['charts'][0]['observation_ids'], [target.pk])

    def test_chart_api_has_role_gate_and_never_uses_selected_ids_outside_filter(self):
        selected = self.save_record()
        outside = self.save_record(territory='Другая территория')
        endpoint = reverse('monitoring:chart_data')
        self.assertEqual(self.client.get(endpoint).status_code,302)
        self.client.force_login(self.unassigned)
        self.assertEqual(self.client.get(endpoint).status_code,403)
        self.client.force_login(self.viewer)
        response = self.client.get(endpoint,{'territory':'Тестовая территория','ids':f'{selected.pk},{outside.pk}'})
        self.assertEqual(response.status_code,200)
        payload = response.json()
        self.assertEqual(payload['observations_count'],1)
        self.assertEqual(payload['charts'][0]['observation_ids'],[selected.pk])

    def test_chart_constructor_exposes_every_indicator_and_dynamic_period(self):
        for n in range(5):
            indicator = self.make_indicator('TARGET-CHART-'+str(n))
            self.save_record(indicator)
            self.save_record(indicator,period_label='II квартал 2024',period_start='2024-04-01',period_end='2024-06-30',value=str(n+13))
        self.client.force_login(self.viewer)
        payload = self.client.get(reverse('monitoring:chart_data')).json()
        self.assertEqual(payload['chart_count'],5)
        self.assertEqual(payload['observations_count'],10)
        self.assertTrue(all(c['dynamic'] and c['kind']=='line' for c in payload['charts']))
        self.assertTrue(all(len(c['labels'])==2 for c in payload['charts']))

    def test_delete_requires_explicit_post_and_delete_permission(self):
        self.save_record()
        endpoint = reverse('monitoring:indicator_delete',args=[self.indicator.pk])
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(endpoint).status_code,403)
        self.client.force_login(self.restricted_editor)
        self.assertEqual(self.client.post(endpoint).status_code,403)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(endpoint).status_code,200)
        self.indicator.refresh_from_db()
        self.assertTrue(self.indicator.is_active)
        self.assertEqual(self.client.post(endpoint).status_code,302)
        self.indicator.refresh_from_db()
        self.assertFalse(self.indicator.is_active)
        self.assertEqual(self.indicator.deleted_by_id,self.editor.pk)
        self.assertIsNotNone(self.indicator.deleted_at)

    def test_delete_hides_current_data_and_forms_but_keeps_versions_and_old_report(self):
        obs = self.save_record()
        published = ReportSnapshot.objects.create(title='Ранее выпущенный синтетический отчёт',snapshot={'rows':[{'id':obs.pk,'value':'12','version_id':obs.current_version_id}]},filters={})
        version_id = obs.current_version_id
        self.client.force_login(self.editor)
        self.client.post(reverse('monitoring:indicator_delete',args=[self.indicator.pk]))
        self.assertEqual(filtered_observations({}),[])
        self.assertTrue(Observation.objects.filter(pk=obs.pk).exists())
        self.assertTrue(ObservationVersion.objects.filter(pk=version_id).exists())
        published.refresh_from_db()
        self.assertEqual(published.snapshot['rows'][0]['value'],'12')
        self.assertNotIn(self.indicator.pk,ObservationForm().fields['indicator'].queryset.values_list('pk',flat=True))
        self.assertNotContains(self.client.get(reverse('monitoring:sources')+'?tab=indicators'),self.indicator.name)
        self.assertEqual(self.client.get(reverse('monitoring:indicator_update',args=[self.indicator.pk])).status_code,404)
        payload = self.client.get(reverse('monitoring:chart_data')).json()
        self.assertEqual(payload['charts'],[])

    def test_reimport_and_automatic_refresh_do_not_resurrect_deleted_indicator(self):
        obs = self.save_record()
        self.client.force_login(self.editor)
        self.client.post(reverse('monitoring:indicator_delete',args=[self.indicator.pk]))
        payload={'observations':[self.record(value='99')]}
        upload=SimpleUploadedFile('synthetic-deleted.json',json.dumps(payload).encode(),content_type='application/json')
        batch=create_preview(upload,self.editor,kind='generic')
        result=confirm_batch(batch,self.editor)
        self.assertEqual(result['skipped_deleted'],1)
        self.assertEqual(result['versions_created'],0)
        source=Source.objects.create(name='Синтетический автоисточник',frequency='manual')
        pub=Publication(source=source,url='https://example.org/synthetic.json',checksum='0'*64)
        pub.file.save('synthetic.json',ContentFile(b'{}'),save=True)
        automatic=apply_payload(payload,pub)
        self.assertEqual(automatic['versions_created'],0)
        self.indicator.refresh_from_db();obs.refresh_from_db()
        self.assertFalse(self.indicator.is_active)
        self.assertEqual(str(obs.current_version.value),'12.00000000')
        self.assertEqual(obs.versions.count(),1)

    def test_old_pending_phrase_is_absent_from_workspace_and_mascot_is_used(self):
        obs=self.save_record(status='pending',issues=['Требует проверки'])
        DataIssue.objects.create(code='TARGET-LEGACY',description='Требует проверки',references=[{'note':'Требуют проверки'}])
        self.client.force_login(self.viewer)
        for url in [reverse('monitoring:dashboard'),reverse('monitoring:sources')+'?tab=issues',reverse('monitoring:observation_detail',args=[obs.pk])]:
            with self.subTest(url=url):
                response=self.client.get(url)
                self.assertEqual(response.status_code,200)
                self.assertNotIn('требует проверки',response.content.decode().casefold())
                self.assertNotIn('требуют проверки',response.content.decode().casefold())
                self.assertContains(response,'monitoring/mascot-256.webp')
        self.client.logout()
        response=self.client.get(reverse('login'))
        self.assertContains(response,'monitoring/mascot-256.webp')


class ChartSelectionUnitTests(SimpleTestCase):
    def row(self,identifier=1,**changes):
        row={'id':identifier,'indicator_code':'SYNTHETIC-CHART','indicator_name':'Синтетическая доля','section':'sociology','unit':'%','territory':'Тестовая территория','group':'Все','question_version_id':1,'period_kind':'flow','period_label':'I квартал 2024','period_start':'2024-01-01','period_end':'2024-03-31','answer':'Ответ А','data_state':'value','value':'0','source_name':'Синтетическая публикация','source_url':'https://example.org/synthetic.xlsx'}
        row.update(changes)
        return row

    def test_multiple_answers_become_distinct_series_in_chronological_order(self):
        records=[self.row(4,answer='Ответ Б',value='45',period_label='II квартал 2024',period_start='2024-04-01',period_end='2024-06-30'),self.row(2,answer='Ответ Б',value='40'),self.row(3,value='65',period_label='II квартал 2024',period_start='2024-04-01',period_end='2024-06-30'),self.row(1,value='60')]
        charts=build_chart_specs(records)
        self.assertEqual(len(charts),1)
        spec=charts[0]
        self.assertEqual(spec['kind'],'line')
        self.assertEqual(spec['labels'],['I квартал 2024','II квартал 2024'])
        values={d['label']:d['values'] for d in spec['datasets']}
        self.assertEqual(values,{'Ответ А':[60.0,65.0],'Ответ Б':[40.0,45.0]})
        self.assertEqual(set(spec['observation_ids']),{1,2,3,4})

    def test_units_territories_and_question_versions_are_not_combined(self):
        records=[self.row(),self.row(2,unit='чел.'),self.row(3,territory='Иная территория'),self.row(4,question_version_id=2)]
        charts=build_chart_specs(records)
        self.assertEqual(len(charts),4)
        self.assertTrue(all(len(s['observation_ids'])==1 for s in charts))

    def test_zero_remains_plotted_while_missing_and_not_applicable_do_not_become_zero(self):
        spec=build_chart_specs([self.row(),self.row(2,value=None,data_state='missing'),self.row(3,value=None,data_state='na')])[0]
        self.assertEqual(spec['values'],[0.0])
        self.assertEqual(spec['observation_ids'],[1])

    def test_duplicate_publications_are_separate_bars_without_averaging(self):
        records=[self.row(1,value='10'),self.row(2,value='20',source_name='Другая публикация'),self.row(3,value='30',period_label='II квартал 2024',period_start='2024-04-01',period_end='2024-06-30')]
        spec=build_chart_specs(records)[0]
        self.assertEqual(spec['kind'],'bar')
        self.assertEqual(spec['values'],[10.0,20.0,30.0])
        self.assertEqual(set(spec['observation_ids']),{1,2,3})
        self.assertIn('без усреднения',spec['notes'])


class RawQuarterChartRegression(SimpleTestCase):
    def test_mixed_source_caption_is_preserved_in_one_chronological_quarter_chart(self):
        base={'indicator_code':'RAW-QUARTERS','indicator_name':'Синтетический ряд','section':'statistics','unit':'ед.','territory':'Тест','period_kind':'unknown','data_state':'value'}
        rows=[dict(base,id=3,period_label='I кв. 2026',period_start='2026-01-01',period_end='2026-03-31',value='30'),dict(base,id=1,period_label='IV кв. 2024 / 2024',value='10'),dict(base,id=2,period_label='IV кв. 2025 / 2025',value='20')]
        spec=build_chart_specs(rows)[0]
        self.assertEqual(len(build_chart_specs(rows)),1)
        self.assertEqual(spec['values'],[10.0,20.0,30.0])
        self.assertEqual(spec['labels'],['IV кв. 2024 / 2024','IV кв. 2025 / 2025','I кв. 2026'])
        self.assertTrue(spec['dynamic'])
        self.assertGreater(spec['x_values'][1]-spec['x_values'][0],spec['x_values'][2]-spec['x_values'][1])
