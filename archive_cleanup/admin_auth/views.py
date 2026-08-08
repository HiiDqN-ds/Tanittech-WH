from django.shortcuts import render

# Create your views here.
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import user_passes_test
from tickets.models import Ticket

# Only superusers/admins can access
def is_admin(user):
    return user.is_staff or user.is_superuser

def admin_login(request):
    if request.user.is_authenticated and is_admin(request.user):
        return redirect('admin_dashboard')

    msg = ''
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)
        if user is not None and is_admin(user):
            login(request, user)
            return redirect('admin_dashboard')
        else:
            msg = '❌ Ungültiger Benutzername oder Passwort'

    return render(request, 'admin_auth/login.html', {'msg': msg})

@user_passes_test(is_admin, login_url='admin_login')
def dashboard(request):
    tickets = Ticket.objects.all().order_by('-created_at')
    return render(request, 'admin_auth/dashboard.html', {'tickets': tickets})

def admin_logout(request):
    logout(request)
    return redirect('admin_login')
