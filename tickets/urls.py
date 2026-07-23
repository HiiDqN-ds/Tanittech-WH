from django.urls import path
from .views import ticket_list, ticket_detail
from .views import generate_auftrag


urlpatterns = [
    path('tickets/', ticket_list, name='ticket_list'),
    path('tickets/<str:tracking_id>/', ticket_detail, name='ticket_detail'),
    path("auftrag/<int:ticket_id>/", generate_auftrag, name="generate_auftrag"),
]
