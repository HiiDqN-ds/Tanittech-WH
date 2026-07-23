from django.contrib import admin
from .models import Ticket, Note

@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'title',
        'status',
        'device_type',
        'created_at',
        'updated_at',
    )
    search_fields = ('title', 'device_model', 'client_phone')
    list_filter = ('status', 'device_type')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ('id', 'ticket', 'author', 'created_at')
