from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

SECTIONS = [('sociology','Социология'),('fintest','Финтест'),('statistics','Социально-экономические показатели'),('irfk','ИРФК'),('quiz','Онлайн-зачёт')]
PERIOD_KINDS = [('stock','Остаток на дату'),('flow','Значение за период'),('cumulative','Накопительный итог'),('unknown','Тип периода требует проверки')]
DATA_STATES = [('value','Значение'),('missing','Нет данных'),('na','Неприменимо')]
STATUS = [('pending','Требует проверки'),('verified','Проверено'),('conflict','Есть расхождение')]

class Topic(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField('Название',max_length=250)
    order = models.PositiveSmallIntegerField(default=0)
    class Meta:
        ordering=['order','name']; verbose_name='Тема'; verbose_name_plural='Темы'
    def __str__(self): return self.name

class TopicAlias(models.Model):
    name = models.CharField('Вариант названия',max_length=250,unique=True)
    topic = models.ForeignKey(Topic,on_delete=models.PROTECT,related_name='aliases')
    class Meta: verbose_name='Соответствие рубрик'; verbose_name_plural='Соответствия рубрик'
    def __str__(self): return f'{self.name} → {self.topic}'

class Source(models.Model):
    name = models.CharField('Название',max_length=300)
    url = models.URLField('Адрес данных',max_length=2000,blank=True)
    publications_url = models.URLField('Страница публикаций',max_length=2000,blank=True)
    adapter = models.CharField('Загрузчик',max_length=50,default='xlsx',choices=[(x,x) for x in ['registry','xlsx','csv','json','cbr_deposits','cbr_cards','rosstat_income','fns_social','folder']])
    mapping = models.JSONField('Соответствие полей и параметры',default=dict,blank=True)
    territory = models.CharField('Территория',max_length=150,default='Москва')
    unit = models.CharField('Единица',max_length=100,blank=True)
    frequency = models.CharField('Частота проверки',max_length=20,default='monthly',choices=[('monthly','Ежемесячно'),('daily','Ежедневно'),('manual','По запросу')])
    schedule_day = models.PositiveSmallIntegerField('День месяца',default=5,validators=[MinValueValidator(1),MaxValueValidator(28)])
    schedule_time = models.TimeField('Время проверки',default='09:00')
    timezone = models.CharField('Часовой пояс',max_length=80,default='Europe/Moscow')
    enabled = models.BooleanField('Расписание включено',default=False)
    hold_reason = models.TextField('Причина приостановки импорта',blank=True)
    next_due = models.DateTimeField('Следующая проверка',null=True,blank=True)
    last_checked = models.DateTimeField('Последняя проверка',null=True,blank=True)
    last_success = models.DateTimeField('Последнее успешное обновление',null=True,blank=True)
    last_publication_date = models.DateField('Дата публикации',null=True,blank=True)
    status = models.CharField('Состояние',max_length=30,default='not_checked')
    last_error = models.TextField('Последняя ошибка',blank=True)
    class Meta: ordering=['name']; verbose_name='Источник'; verbose_name_plural='Источники'
    def __str__(self): return self.name

class Publication(models.Model):
    source = models.ForeignKey(Source,on_delete=models.PROTECT,related_name='publications')
    url = models.CharField(max_length=2000,blank=True)
    title = models.CharField(max_length=500,blank=True)
    checksum = models.CharField(max_length=64)
    file = models.FileField(upload_to='publications/%Y/%m/')
    published_at = models.DateField(null=True,blank=True)
    loaded_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['source','url','checksum'],name='unique_publication_content')]
        verbose_name='Исходная публикация'; verbose_name_plural='Исходные публикации'
    def __str__(self): return self.title or self.url or self.file.name

class Study(models.Model):
    code = models.CharField(max_length=100,unique=True)
    name = models.CharField('Название',max_length=400)
    description = models.TextField(blank=True)
    class Meta: verbose_name='Исследование'; verbose_name_plural='Исследования'
    def __str__(self): return self.name

class Wave(models.Model):
    study = models.ForeignKey(Study,on_delete=models.PROTECT)
    code = models.CharField(max_length=150)
    name = models.CharField(max_length=300)
    start = models.DateField(null=True,blank=True)
    end = models.DateField(null=True,blank=True)
    base = models.PositiveIntegerField(null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['study','code'],name='unique_study_wave')]
        verbose_name='Волна'; verbose_name_plural='Волны'
    def __str__(self): return self.name

class Indicator(models.Model):
    code = models.CharField('Код',max_length=150,unique=True)
    name = models.TextField('Показатель')
    section = models.CharField('Раздел',max_length=20,choices=SECTIONS)
    unit = models.CharField('Единица измерения',max_length=100,blank=True)
    period_kind = models.CharField('Тип периода',max_length=20,choices=PERIOD_KINDS,default='unknown')
    definition = models.TextField('Определение',blank=True)
    notes = models.TextField('Замечания',blank=True)
    topics = models.ManyToManyField(Topic,blank=True,related_name='indicators',verbose_name='Темы')
    source = models.ForeignKey(Source,on_delete=models.PROTECT,null=True,blank=True,verbose_name='Источник')
    class Meta: ordering=['section','code']; verbose_name='Показатель'; verbose_name_plural='Показатели'
    def __str__(self): return self.name

class Question(models.Model):
    indicator = models.OneToOneField(Indicator,on_delete=models.PROTECT,related_name='question')
    study = models.ForeignKey(Study,on_delete=models.PROTECT,null=True,blank=True)
    multiple_choice = models.BooleanField('Множественный выбор',null=True,blank=True,default=None)
    class Meta: verbose_name='Вопрос'; verbose_name_plural='Вопросы'
    def __str__(self): return self.indicator.name

class QuestionVersion(models.Model):
    question = models.ForeignKey(Question,on_delete=models.PROTECT,related_name='versions')
    version = models.PositiveIntegerField(default=1)
    text = models.TextField()
    effective_from = models.DateField(null=True,blank=True)
    notes = models.TextField(blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['question','version'],name='unique_question_version')]
        verbose_name='Версия вопроса'; verbose_name_plural='Версии вопросов'
    def __str__(self): return f'{self.text[:80]} (v{self.version})'

class AnswerOption(models.Model):
    question_version = models.ForeignKey(QuestionVersion,on_delete=models.PROTECT,related_name='options')
    code = models.CharField(max_length=100)
    label = models.TextField()
    is_correct = models.BooleanField(null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['question_version','code'],name='unique_answer_option')]
        verbose_name='Вариант ответа'; verbose_name_plural='Варианты ответов'
    def __str__(self): return self.label

class Methodology(models.Model):
    code = models.CharField(max_length=100)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    approved = models.BooleanField('Методика согласована',default=False)
    source = models.ForeignKey(Publication,on_delete=models.PROTECT,null=True,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['code','version'],name='unique_methodology_version')]
        verbose_name='Версия методики'; verbose_name_plural='Версии методики'
    def __str__(self): return f'{self.name} v{self.version}'

class Formula(models.Model):
    indicator = models.ForeignKey(Indicator,on_delete=models.PROTECT,related_name='formulas')
    methodology = models.ForeignKey(Methodology,on_delete=models.PROTECT)
    expression = models.JSONField('Формула (JSON)',default=dict)
    # {operation:ratio|sum|weighted_ratio, components:[code,...], multiplier:'100'}
    class Meta: verbose_name='Формула'; verbose_name_plural='Формулы'
    def __str__(self): return f'{self.indicator.code}: {self.methodology}'

class Observation(models.Model):
    dimension_key = models.CharField(max_length=64,unique=True)
    indicator = models.ForeignKey(Indicator,on_delete=models.PROTECT,related_name='observations')
    question_version = models.ForeignKey(QuestionVersion,on_delete=models.PROTECT,null=True,blank=True)
    answer_option = models.ForeignKey(AnswerOption,on_delete=models.PROTECT,null=True,blank=True)
    wave = models.ForeignKey(Wave,on_delete=models.PROTECT,null=True,blank=True)
    territory = models.CharField('Территория',max_length=150)
    period_start = models.DateField('Начало периода',null=True,blank=True)
    period_end = models.DateField('Конец периода / дата',null=True,blank=True)
    period_label = models.CharField('Период в источнике',max_length=300)
    period_kind = models.CharField('Тип периода',max_length=20,choices=PERIOD_KINDS,default='unknown')
    group = models.CharField('Группа респондентов',max_length=300,blank=True)
    answer = models.CharField('Вариант ответа',max_length=1500,blank=True)
    current_version = models.ForeignKey('ObservationVersion',on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['indicator__code','period_end']; verbose_name='Наблюдение'; verbose_name_plural='Наблюдения'
    def __str__(self): return f'{self.indicator.code}, {self.period_label}, {self.territory}'

class ObservationVersion(models.Model):
    observation = models.ForeignKey(Observation,on_delete=models.PROTECT,related_name='versions')
    number = models.PositiveIntegerField()
    value = models.DecimalField(max_digits=28,decimal_places=8,null=True,blank=True)
    data_state = models.CharField(max_length=10,choices=DATA_STATES,default='value')
    unit = models.CharField(max_length=100)
    numerator = models.DecimalField(max_digits=28,decimal_places=8,null=True,blank=True)
    denominator = models.DecimalField(max_digits=28,decimal_places=8,null=True,blank=True)
    base = models.CharField(max_length=300,blank=True)
    publication = models.ForeignKey(Publication,on_delete=models.PROTECT,null=True,blank=True)
    publication_date = models.DateField(null=True,blank=True)
    provenance = models.JSONField(default=dict,blank=True)
    method = models.CharField(max_length=20,choices=[('auto','Автоматически'),('manual','Вручную'),('gpt','Через GPT')])
    methodology = models.ForeignKey(Methodology,on_delete=models.PROTECT,null=True,blank=True)
    status = models.CharField(max_length=15,choices=STATUS,default='pending')
    issues = models.JSONField(default=list,blank=True)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    reason = models.TextField(blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['observation','number'],name='unique_observation_version'),models.CheckConstraint(condition=(Q(data_state='value',value__isnull=False)|Q(data_state__in=['missing','na'],value__isnull=True)),name='observation_value_state_consistent')]
        ordering=['-number']; verbose_name='Версия наблюдения'; verbose_name_plural='Версии наблюдений'
    def __str__(self): return f'{self.observation} v{self.number}'

class ManualCorrection(models.Model):
    observation = models.ForeignKey(Observation,on_delete=models.PROTECT,related_name='corrections')
    based_on = models.ForeignKey(ObservationVersion,on_delete=models.PROTECT)
    value = models.DecimalField(max_digits=28,decimal_places=8,null=True,blank=True)
    data_state = models.CharField(max_length=10,choices=DATA_STATES,default='value')
    reason = models.TextField('Обоснование')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)
    conflict = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True,blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True,related_name='+')
    resolution = models.TextField(blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['observation'],condition=Q(active=True),name='one_active_correction'),models.CheckConstraint(condition=(Q(data_state='value',value__isnull=False)|Q(data_state__in=['missing','na'],value__isnull=True)),name='correction_value_state_consistent')]
        verbose_name='Ручная поправка'; verbose_name_plural='Ручные поправки'

class ImportBatch(models.Model):
    file = models.FileField(upload_to='imports/%Y/%m/',blank=True)
    checksum = models.CharField(max_length=64,blank=True)
    kind = models.CharField(max_length=30,default='registry')
    source = models.ForeignKey(Source,on_delete=models.PROTECT,null=True,blank=True)
    publication = models.ForeignKey(Publication,on_delete=models.PROTECT,null=True,blank=True)
    preview = models.JSONField(default=dict)
    status = models.CharField(max_length=20,default='preview',choices=[('preview','Предварительный просмотр'),('confirmed','Импортировано'),('error','Ошибка')])
    result = models.JSONField(default=dict,blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True,blank=True)
    class Meta: ordering=['-created_at']; verbose_name='Импорт'; verbose_name_plural='Импорты'

class LoadRun(models.Model):
    source = models.ForeignKey(Source,on_delete=models.PROTECT,related_name='runs')
    status = models.CharField(max_length=20,default='queued',choices=[('queued','В очереди'),('running','Выполняется'),('retry','Повтор'),('success','Успешно'),('error','Ошибка')])
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    trigger = models.CharField(max_length=30,default='manual')
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True,blank=True)
    finished_at = models.DateTimeField(null=True,blank=True)
    log = models.TextField(blank=True)
    result = models.JSONField(default=dict,blank=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['source'],condition=Q(status__in=['queued','running','retry']),name='one_active_source_run')]
        ordering=['-created_at']; verbose_name='Задание загрузки'; verbose_name_plural='Журнал загрузок'

class DataIssue(models.Model):
    code = models.CharField(max_length=150)
    severity = models.CharField(max_length=20,default='warning')
    description = models.TextField()
    references = models.JSONField(default=list)
    batch = models.ForeignKey(ImportBatch,on_delete=models.PROTECT,null=True,blank=True)
    indicator = models.ForeignKey(Indicator,on_delete=models.PROTECT,null=True,blank=True)
    resolved = models.BooleanField(default=False)
    resolution = models.TextField(blank=True)
    class Meta: verbose_name='Вопрос проверки'; verbose_name_plural='Вопросы проверки'

class ReportSnapshot(models.Model):
    title = models.CharField(max_length=300)
    period_label = models.CharField(max_length=300,blank=True)
    territory = models.CharField(max_length=150,blank=True)
    filters = models.JSONField(default=dict)
    snapshot = models.JSONField(default=dict)
    docx_file = models.FileField(upload_to='reports/%Y/%m/',blank=True)
    xlsx_file = models.FileField(upload_to='reports/%Y/%m/',blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT,null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-created_at']; verbose_name='Версия отчёта'; verbose_name_plural='Версии отчётов'
