from django.shortcuts import render, redirect
from tickets.models import Ticket
from django.contrib.auth.models import User
from django.http import JsonResponse
import json

def index(request):
    return render(request, 'home/index.html')

def create_ticket(request):
    """Public page for creating repair tickets via AJAX"""
    context = {
        'tickets_url': '/tickets/',  # AJAX endpoint
    }
    return render(request, 'home/create_ticket.html', context)

def static_page(request, slug):
    """Serve static pages like datenschutz, agb, etc."""
    pages = {
        'datenschutz': 'pages/datenschutz.html',
        'agb': 'pages/agb.html',
        'lieferung': 'pages/lieferung.html',
        'widerruf': 'pages/widerruf.html',
        'bestellung': 'pages/bestellung.html',
        'impressum': 'pages/impressum.html',
        'blog': 'pages/blog.html',
        'faqs': 'pages/faqs.html',
        'kontakt': 'pages/kontakt.html',
    }
    template = pages.get(slug)
    if template:
        return render(request, template)
    else:
        from django.http import Http404
        raise Http404("Page not found")

def newsletter(request):
    if request.method == 'POST':
        email = request.POST.get('email', '')
        # Placeholder - send email or save to model
        # TODO: Integrate with mail service or model
        return JsonResponse({'status': 'success', 'message': 'Subscribed!'})
    else:
        return render(request, 'pages/newsletter.html')
