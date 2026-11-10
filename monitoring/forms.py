from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django import forms
from django.core.exceptions import ValidationError
from .models import Indicator, Source, Topic, TopicAlias, QuestionVersion, Wave, SECTIONS, PERIOD_KINDS, DATA_STATES


class IndicatorForm(forms.ModelForm):
    class Meta:
        model = Indicator
        fields = ['code', 'name', 'section', 'unit', 'period_kind', 'definition', 'notes', 'topics', 'source']
        widgets = {'name': forms.Textarea(attrs={'rows': 3}), 'definition': forms.Textarea(attrs={'rows': 3}), 'notes': forms.Textarea(attrs={'rows': 3}), 'topics': forms.SelectMultiple()}
        help_texts = {'code': 'Постоянный уникальный код. После импорта используйте тот же код.', 'topics': 'Один показатель может относиться к нескольким темам. Для выбора нескольких удерживайте Ctrl.', 'period_kind': 'Остаток на дату, значение за период и накопительный итог не смешиваются.'}


class SourceForm(forms.ModelForm):
    class Meta:
        model = Source
        fields = ['name', 'publications_url', 'url', 'adapter', 'mapping', 'territory', 'unit', 'frequency', 'schedule_day', 'schedule_time', 'timezone', 'enabled', 'hold_reason']
        widgets = {'mapping': forms.Textarea(attrs={'rows': 8, 'spellcheck': 'false'}), 'hold_reason': forms.Textarea(attrs={'rows': 3}), 'schedule_time': forms.TimeInput(attrs={'type': 'time'})}
        help_texts = {'publications_url': 'Официальная страница, на которой появляются новые публикации.', 'url': 'Прямой адрес файла или API. Его содержимое проверяется повторно по контрольной сумме.', 'mapping': 'JSON: соответствие полей, лист, столбцы, территория и период. Примеры приведены в docs/connectors.md.', 'enabled': 'При работающем фоновом процессе проверка выполняется без открытого браузера.', 'hold_reason': 'При неоднозначном определении показателя укажите причину: автоматическая запись будет приостановлена.'}

    def clean_timezone(self):
        value = self.cleaned_data['timezone']
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError('Укажите действительный часовой пояс, например Europe/Moscow.') from exc
        return value

    def clean_mapping(self):
        value = self.cleaned_data.get('mapping') or {}
        if not isinstance(value, dict):
            raise ValidationError('Параметры должны быть JSON-объектом: {"поле": "значение"}.')
        return value

    def clean(self):
        data = super().clean()
        if data.get('enabled') and not (data.get('url') or data.get('publications_url')) and data.get('adapter') != 'folder':
            self.add_error('url', 'Для автоматической проверки нужен адрес данных или страницы публикаций.')
        return data


class ObservationForm(forms.Form):
    indicator = forms.ModelChoiceField(queryset=Indicator.objects.filter(is_active=True), label='Показатель')
    question_version = forms.ModelChoiceField(queryset=QuestionVersion.objects.filter(question__indicator__is_active=True), label='Версия вопроса', required=False, help_text='Выберите для социологии или финтеста. При отсутствии будет использована последняя версия вопроса.')
    wave = forms.ModelChoiceField(queryset=Wave.objects.all(), label='Волна исследования', required=False)
    territory = forms.CharField(label='Территория', initial='Москва', max_length=150)
    period_label = forms.CharField(label='Период в источнике', max_length=300, help_text='Например: «I квартал 2026» или «01.07.2026».')
    period_start = forms.DateField(label='Начало периода', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    period_end = forms.DateField(label='Конец периода / дата наблюдения', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    period_kind = forms.ChoiceField(label='Тип периода', choices=PERIOD_KINDS)
    group = forms.CharField(label='Группа респондентов', required=False, max_length=300)
    answer = forms.CharField(label='Вариант ответа', required=False, max_length=1500)
    data_state = forms.ChoiceField(label='Состояние данных', choices=DATA_STATES)
    value = forms.DecimalField(label='Опубликованное значение', required=False, max_digits=28, decimal_places=8, help_text='Ноль — допустимое значение. Для пропуска и неприменимости оставьте поле пустым.')
    unit = forms.CharField(label='Единица измерения', max_length=100)
    numerator = forms.DecimalField(label='Точный числитель', required=False, max_digits=28, decimal_places=8, help_text='Не восстанавливайте число правильных ответов из округлённого процента.')
    denominator = forms.DecimalField(label='Точный знаменатель', required=False, max_digits=28, decimal_places=8)
    base = forms.CharField(label='База расчёта', max_length=300, required=False)
    publication_date = forms.DateField(label='Дата публикации', required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    source_url = forms.URLField(label='Ссылка на публикацию', required=False, max_length=2000)
    source_detail = forms.CharField(label='Файл, лист, ячейка или страница', required=False, max_length=1000)
    reason = forms.CharField(label='Обоснование добавления', widget=forms.Textarea(attrs={'rows': 3}))

    def clean(self):
        data = super().clean()
        if (data.get('data_state') == 'value') != (data.get('value') is not None):
            self.add_error('value', 'Для состояния «Значение» укажите число; для остальных состояний оставьте поле пустым.')
        if not data.get('source_url') and not data.get('source_detail', '').strip():
            self.add_error('source_detail', 'Укажите ссылку на источник или реквизиты файла, листа, ячейки или страницы.')
        if data.get('period_start') and data.get('period_end') and data['period_start'] > data['period_end']:
            self.add_error('period_end', 'Конец периода не может быть раньше начала.')
        if data.get('question_version') and data.get('indicator') and data['question_version'].question.indicator_id != data['indicator'].pk:
            self.add_error('question_version', 'Выбранная версия относится к другому показателю.')
        if data.get('numerator') is not None and (data.get('denominator') is None or data['denominator'] <= 0):
            self.add_error('denominator', 'Для числителя нужен положительный знаменатель.')
        if data.get('numerator') is not None and data.get('denominator') and not 0 <= data['numerator'] <= data['denominator']:
            self.add_error('numerator', 'Числитель должен быть от 0 до знаменателя.')
        return data


class CorrectionForm(forms.Form):
    data_state = forms.ChoiceField(label='Состояние данных', choices=DATA_STATES)
    value = forms.DecimalField(label='Применяемое значение', required=False, max_digits=28, decimal_places=8)
    reason = forms.CharField(label='Обоснование поправки', widget=forms.Textarea(attrs={'rows': 3}), help_text='Исходное значение сохранится. Новая публикация не сотрёт поправку.')
    def clean(self):
        data = super().clean()
        if (data.get('data_state') == 'value') != (data.get('value') is not None):
            self.add_error('value', 'Проверьте число и состояние данных.')
        return data


class ResolutionForm(forms.Form):
    choice = forms.ChoiceField(label='Применять', choices=[('correction', 'Ручную поправку'), ('source', 'Новое значение источника')])
    reason = forms.CharField(label='Обоснование решения', widget=forms.Textarea(attrs={'rows': 2}))


class ImportForm(forms.Form):
    kind = forms.ChoiceField(label='Вид файла', choices=[('registry', 'Исходный реестр мониторинга (три листа Excel)'), ('generic', 'Универсальный импорт (XLSX, CSV, JSON)'), ('source', 'Сохранённый файл зарегистрированного источника')])
    source = forms.ModelChoiceField(queryset=Source.objects.all(), label='Источник', required=False, help_text='Для файла внешнего источника выберите зарегистрированное подключение: используется тот же загрузчик.')
    file = forms.FileField(label='Файл', help_text='XLSX, CSV или JSON, не больше 25 МБ.')
    def clean(self):
        data = super().clean()
        if data.get('kind') == 'source' and not data.get('source'):
            self.add_error('source', 'Выберите источник для сохранённого файла.')
        file = data.get('file')
        if file and file.size > 25 * 1024 * 1024:
            self.add_error('file', 'Файл больше 25 МБ.')
        return data


class TopicForm(forms.ModelForm):
    class Meta:
        model = Topic
        fields = ['name', 'slug', 'order']
        labels = {'slug': 'Постоянный код', 'order': 'Порядок отображения'}


class TopicAliasForm(forms.ModelForm):
    class Meta:
        model = TopicAlias
        fields = ['name', 'topic']
        labels = {'topic': 'Каноническая тема'}


class GptForm(forms.Form):
    file = forms.FileField(label='Документ', required=False, help_text='DOCX, XLSX, PDF, TXT, CSV или JSON до 20 МБ. Документ передаётся в OpenAI после запуска извлечения.')
    text = forms.CharField(label='Или вставьте текст', required=False, max_length=120000, widget=forms.Textarea(attrs={'rows': 9, 'class': 'gpt-textarea', 'placeholder': 'Вставьте фрагмент публикации вместе с периодом, территорией и источником…'}))
    instruction = forms.CharField(label='Что извлечь', max_length=5000, widget=forms.Textarea(attrs={'rows': 3}), initial='Найти опубликованные значения и сопоставить с существующими показателями. Сохранить период, территорию, единицы и ссылку на источник.')
    section = forms.ChoiceField(label='Раздел справочника', required=False, choices=[('', 'Все разделы')] + SECTIONS)
    def clean(self):
        data = super().clean()
        if not data.get('file') and not data.get('text', '').strip():
            raise ValidationError('Загрузите документ или вставьте текст.')
        if data.get('file') and data['file'].size > 20 * 1024 * 1024:
            self.add_error('file', 'Документ больше 20 МБ.')
        return data
