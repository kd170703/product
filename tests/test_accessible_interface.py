"""Accessibility regressions with synthetic observations and no live integrations."""
from pathlib import Path
import re

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.staticfiles import finders
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from monitoring.models import Indicator
from monitoring.services.observations import upsert_observation


class SemanticWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('accessible-viewer')
        cls.user.user_permissions.add(Permission.objects.get(
            content_type__app_label='monitoring', codename='view_observation'))
        cls.indicator = Indicator.objects.create(
            code='SYNTHETIC-A11Y', name='Синтетический показатель доступности',
            section='statistics', unit='ед.', period_kind='flow')
        cls.observation, _ = upsert_observation({
            'indicator_code': cls.indicator.code, 'value': '0',
            'data_state': 'value', 'unit': 'ед.', 'territory': 'Тестовая территория',
            'period_label': 'I квартал 2025', 'period_start': '2025-01-01',
            'period_end': '2025-03-31', 'period_kind': 'flow',
            'provenance': {'url': 'https://example.org/accessibility.csv'},
        }, method='auto')

    def setUp(self):
        self.client.force_login(self.user)

    def test_workspace_has_no_focusable_controls_inside_hidden_aria(self):
        for name in ['dashboard', 'sources', 'imports', 'reports', 'gpt']:
            with self.subTest(page=name):
                response = self.client.get(reverse('monitoring:' + name))
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.content, 'html.parser')
                for hidden in soup.select('[aria-hidden="true"]'):
                    controls = hidden.select('a[href],button,input,select,textarea,[tabindex]')
                    self.assertFalse(controls, 'Focusable element in aria-hidden subtree')
                    if hidden.name == 'svg':
                        self.assertEqual(hidden.get('focusable'), 'false')

    def test_svg_navigation_and_optimized_brand_do_not_imply_live_state(self):
        soup = BeautifulSoup(self.client.get(reverse('monitoring:dashboard')).content, 'html.parser')
        nav_links = soup.select('.main-nav a')
        self.assertTrue(nav_links)
        self.assertTrue(all(link.select_one('svg use') for link in nav_links))
        sidebar = soup.select_one('.sidebar')
        self.assertIsNotNone(sidebar)
        self.assertFalse(sidebar.select('.status-dot'))
        self.assertNotIn('Рабочее пространство', soup.select_one('.topbar').get_text(' ', strip=True))
        statistics = soup.select_one('a[href="' + reverse('monitoring:section', args=['statistics']) + '"]')
        self.assertIsNotNone(statistics)
        self.assertEqual(statistics.get_text(' ', strip=True), 'Статистика')
        logo = soup.select_one('.brand img')
        self.assertIsNotNone(logo)
        self.assertIn('.webp', logo['src'])
        favicon = soup.select_one('link[rel="icon"]')
        self.assertIsNotNone(favicon)
        self.assertIn('favicon', favicon['href'])

    def test_observation_cards_keep_column_names_and_checkbox_labels(self):
        soup = BeautifulSoup(self.client.get(reverse('monitoring:dashboard')).content, 'html.parser')
        table = soup.select_one('.observations-table')
        self.assertIsNotNone(table)
        self.assertTrue(table.select('caption'))
        self.assertTrue(all(th.get('scope') == 'col' for th in table.select('thead th')))
        rows = table.select('tbody tr')
        self.assertEqual(len(rows), 1)
        for cell in rows[0].select('td'):
            has_control = cell.select_one('input[aria-label],a[aria-label]')
            self.assertTrue(cell.get('data-label') or has_control, 'Mobile card field lacks a label')
        checkbox = table.select_one('input.row-select')
        self.assertIsNotNone(checkbox)
        self.assertTrue(checkbox.get('aria-label'))
        self.assertIsNotNone(checkbox.find_parent('label', class_='selection-control'))

    def test_interactive_chart_container_is_group_and_not_an_image(self):
        soup = BeautifulSoup(self.client.get(reverse('monitoring:dashboard')).content, 'html.parser')
        charts = soup.select('.chart-container')
        self.assertTrue(charts)
        for chart in charts:
            self.assertEqual(chart.get('role'), 'group')
            self.assertTrue(chart.get('aria-label'))

    def test_views_and_login_do_not_render_inline_style_attributes(self):
        pages = [reverse('monitoring:' + name) for name in
                 ['dashboard', 'sources', 'imports', 'reports', 'gpt']]
        pages.append(reverse('monitoring:observation_detail', args=[self.observation.pk]))
        for url in pages:
            with self.subTest(url=url):
                soup = BeautifulSoup(self.client.get(url).content, 'html.parser')
                self.assertFalse(soup.select('[style]'))
        self.client.logout()
        soup = BeautifulSoup(self.client.get(reverse('login')).content, 'html.parser')
        self.assertFalse(soup.select('[style]'))


class InterfaceAssetTests(SimpleTestCase):
    def test_stylesheet_font_floor_and_readable_source_files(self):
        css_dir = Path(finders.find('monitoring/app.css')).parent
        sources = [css_dir / name for name in ['base.css', 'layout.css', 'components.css', 'charts.css']]
        self.assertTrue(all(path.is_file() for path in sources))
        for path in sources:
            css = path.read_text(encoding='utf-8')
            with self.subTest(path=path.name):
                self.assertGreater(css.count('\n'), 20)
                for match in re.finditer(r'font-size\s*:\s*(\d+(?:\.\d+)?)px', css):
                    self.assertGreaterEqual(float(match.group(1)), 12)
        base = sources[0].read_text(encoding='utf-8')
        self.assertIn('--fs-xs', base)
        self.assertIn('--fs-base', base)

    def test_brand_assets_are_small_and_have_distinct_favicon(self):
        mascot = Path(finders.find('monitoring/mascot-256.webp'))
        favicon = Path(finders.find('monitoring/favicon-64.png'))
        with Image.open(mascot) as image:
            self.assertEqual(image.size, (256, 256))
        with Image.open(favicon) as image:
            self.assertEqual(image.size, (64, 64))
        self.assertLessEqual(mascot.stat().st_size, 60_000)
        self.assertLessEqual(favicon.stat().st_size, 20_000)
