from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import LawProfile, ProcurementRecord, StatusUpdate, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (
        (None, {'fields': ('role',)}),
    )
    list_display = ('username', 'email', 'role', 'is_staff')


@admin.register(LawProfile)
class LawProfileAdmin(admin.ModelAdmin):
    list_display = ('slug', 'governing_law', 'jurisdiction_type', 'regulating_body')


class StatusUpdateInline(admin.TabularInline):
    model = StatusUpdate
    extra = 0
    readonly_fields = ('id', 'old_status', 'new_status', 'note', 'updated_by', 'updated_at')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        # status_updates must only ever be created via services.transition_status,
        # never hand-entered — see PRODAP_AGENT_BUILD_PROMPT_V2.md section 3.6.
        return False


@admin.register(ProcurementRecord)
class ProcurementRecordAdmin(admin.ModelAdmin):
    list_display = ('title', 'department', 'status', 'budget_source', 'display_cost')
    list_filter = ('status', 'budget_source', 'department')
    search_fields = ('title', 'vendor_name')
    readonly_fields = ('status', 'created_at', 'updated_at')
    inlines = [StatusUpdateInline]
