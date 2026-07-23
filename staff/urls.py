from django.urls import path
from . import views

app_name = 'staff'

urlpatterns = [
    path('login/', views.staff_login, name='login'),
    path('payment-required/', views.payment_required, name='payment_required'),
    path('logout/', views.staff_logout, name='logout'),
    path('dashboard/', views.staff_dashboard, name='dashboard'),
    path('ticket/<int:id>/', views.staff_ticket_detail, name='staff_ticket_detail'),
    path('ticket/<int:id>/update/', views.update_ticket_status, name='update_ticket_status'),
    path('ticket/delete/<int:ticket_id>/', views.delete_ticket, name='delete_ticket'),
    path('ticket/<int:id>/print/', views.print_ticket, name='print_ticket'),  # <--- new
    path("create-ticket/", views.create_ticket, name="create_ticket"),
    path('export-csv/', views.export_tickets_csv, name='export_csv'),
    path('ticket/<int:id>/regenerate-pdf/', views.regenerate_pdf, name='regenerate_pdf'),
    path('print-example/', views.print_ticket_example, name='print_ticket_example'),  # Example view
]
