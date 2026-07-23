from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='home'),
    path('create-ticket/', views.create_ticket, name='create_ticket'),

    # Slug-based static pages (existing)
    path('pages/<slug>/', views.static_page, name='static_page'),

    # Root-level aliases (fixes 404 for /agb, /lieferung, etc.)
    path('agb/', views.static_page, {'slug': 'agb'}, name='agb'),
    path('datenschutz/', views.static_page, {'slug': 'datenschutz'}, name='datenschutz'),
    path('lieferung/', views.static_page, {'slug': 'lieferung'}, name='lieferung'),
    path('widerruf/', views.static_page, {'slug': 'widerruf'}, name='widerruf'),
    path('bestellung/', views.static_page, {'slug': 'bestellung'}, name='bestellung'),
    path('impressum/', views.static_page, {'slug': 'impressum'}, name='impressum'),
    path('blog/', views.static_page, {'slug': 'blog'}, name='blog'),
    path('faqs/', views.static_page, {'slug': 'faqs'}, name='faqs'),
    path('kontakt/', views.static_page, {'slug': 'kontakt'}, name='kontakt'),

    path('newsletter/', views.newsletter, name='newsletter'),
]
