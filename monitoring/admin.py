from django.contrib import admin
from .models import *

class ImmutableAdmin(admin.ModelAdmin):
    def has_add_permission(self,request):return False
    def has_delete_permission(self,request,obj=None):return False
    def has_change_permission(self,request,obj=None):return False
    def get_readonly_fields(self,request,obj=None):return [f.name for f in self.model._meta.fields]

@admin.register(Indicator)
class IndicatorAdmin(admin.ModelAdmin):
    def has_delete_permission(self,request,obj=None):return False
    list_display=['code','name','section','unit','is_active'];list_filter=['section','topics','is_active'];search_fields=['code','name'];filter_horizontal=['topics']
    def get_readonly_fields(self,request,obj=None):return ['code'] if obj and obj.observations.exists() else []

@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display=['name','adapter','enabled','last_checked','status'];list_filter=['adapter','enabled'];search_fields=['name']

@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):list_display=['name','slug','order']

@admin.register(DataIssue)
class DataIssueAdmin(admin.ModelAdmin):
    list_display=['code','severity','resolved'];list_filter=['severity','resolved'];search_fields=['code','description'];readonly_fields=['code','description','references','batch','indicator']

for model in [Observation,ObservationVersion,ManualCorrection,Publication,ReportSnapshot,ImportBatch,LoadRun]:admin.site.register(model,ImmutableAdmin)
class VersionedAdmin(admin.ModelAdmin):
    def get_readonly_fields(self,request,obj=None):return [f.name for f in self.model._meta.fields] if obj else []
    def has_delete_permission(self,request,obj=None):return False
for model in [QuestionVersion,AnswerOption,Methodology,Formula]:admin.site.register(model,VersionedAdmin)
for model in [TopicAlias,Study,Wave,Question]:admin.site.register(model)
