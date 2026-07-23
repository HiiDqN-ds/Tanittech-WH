from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.urls import reverse
from tickets.models import Ticket
from django.contrib import messages
from django.http import HttpResponse
import csv
from io import StringIO
# -----------------------------
# Staff Authentication
# -----------------------------
from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login
from django.utils.http import url_has_allowed_host_and_scheme
from django.conf import settings

# In-memory storage for paid users (user_id -> True for lifetime access)
# In production, use a database model with a 'has_paid' flag
PAID_USERS = set()

def staff_login(request):
    """Staff login page (supports login by username OR email)."""
    next_url = request.GET.get("next") or request.POST.get("next") or '/staff/dashboard/'

    # Check if user is already authenticated and is staff
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('staff:dashboard')

    if request.method == "POST":
        username_or_email = request.POST.get("username")
        password = request.POST.get("password")

        from django.contrib.auth import get_user_model
        from django.db.models import Q

        UserModel = get_user_model()
        u = UserModel.objects.filter(Q(username=username_or_email) | Q(email=username_or_email)).first()

        # Validate password manually to avoid backend mismatch issues
        if u is not None and u.check_password(password) and u.is_staff:
            login(request, u)

            # Safety check to prevent open redirect
            if url_has_allowed_host_and_scheme(next_url, allowed_hosts=settings.ALLOWED_HOSTS):
                return redirect(next_url)
            return redirect('/staff/dashboard/')

        return render(request, "staff/login.html", {"error": "Invalid credentials", "next": next_url})

    return render(request, "staff/login.html", {"next": next_url})



def payment_required(request):
    """Show payment page with PayPal link"""
    # PayPal.me link for 150 EUR
    paypal_link = "https://paypal.me/YOUR_PAYPAL_ID/150"
    
    return render(request, "staff/payment_required.html", {
        "paypal_link": paypal_link,
        "amount": 150
    })




@login_required(login_url='staff:login')
def staff_logout(request):
    """Log out staff user"""
    logout(request)
    return redirect('staff:login')


# -----------------------------
# Dashboard & Ticket Views
# -----------------------------
@login_required(login_url='staff:login')
def staff_dashboard(request):
    """List all tickets in dashboard"""
    tickets = Ticket.objects.all().order_by('-created_at')
    
    # Calculate stats
    total_tickets = tickets.count()
    total_revenue = sum(ticket.estimated_price or 0 for ticket in tickets)
    
    # Open tickets = all non-closed tickets (open, in_progress, sent_post_dhl, waiting_approval, approved)
    open_tickets = tickets.exclude(status='closed')
    open_tickets_count = open_tickets.count()
    open_amount = sum(ticket.estimated_price or 0 for ticket in open_tickets)
    
    # Closed tickets = only tickets with status='closed'
    closed_tickets = tickets.filter(status='closed')
    closed_tickets_count = closed_tickets.count()
    closed_amount = sum(ticket.estimated_price or 0 for ticket in closed_tickets)
    
    # Profit calculations (closed tickets only)
    from django.db.models import Sum
    total_repair_cost = closed_tickets.aggregate(total_repair=Sum('repair_cost'))['total_repair'] or 0
    total_profit = closed_amount - total_repair_cost
    
    return render(request, 'staff/dashboard.html', {
        'tickets': tickets,
        'total_tickets': total_tickets,
        'total_revenue': total_revenue,
        'open_tickets_count': open_tickets_count,
        'open_amount': open_amount,
        'closed_tickets_count': closed_tickets_count,
        'closed_amount': closed_amount,
        'total_repair_cost': total_repair_cost,
        'total_profit': total_profit,
    })


@login_required(login_url='staff:login')
def staff_ticket_detail(request, id):
    """Detail view for a single ticket"""
    ticket = get_object_or_404(Ticket, id=id)
    return render(request, 'staff/ticket_detail.html', {'ticket': ticket})


# -----------------------------
# Update Ticket Status + Price + Notify Client
# -----------------------------
@login_required(login_url='staff:login')
def update_ticket_status(request, id):
    ticket = get_object_or_404(Ticket, id=id)

    if request.method == "POST":
        ticket.status = request.POST.get("status", ticket.status)
        price = request.POST.get("price")
        if price:
            ticket.estimated_price = price
        repair_cost = request.POST.get("repair_cost")
        if repair_cost:
            ticket.repair_cost = repair_cost
        staff_note = request.POST.get("staff_note")
        ticket.save()

        # Send email
        subject = f"Update zu Ihrem Reparaturauftrag #{ticket.tracking_id}"
        message = f"""
Hallo {ticket.client.first_name},

Es gibt ein neues Update zu Ihrem Auftrag.

Status: {ticket.get_status_display()}
Preis: {ticket.estimated_price} €

Nachricht vom Techniker:
{staff_note or 'Keine zusätzliche Notiz.'}

Tracking Nummer: {ticket.tracking_id}

Mit freundlichen Grüßen  
Tanitech Team
"""
        try:
            send_mail(subject, message, None, [ticket.client.email], fail_silently=False)
            messages.success(request, "Update + Nachricht erfolgreich an Kunden gesendet ✅")
        except Exception as e:
            messages.error(request, f"E-Mail Fehler: {e} ❌")

        # 🔹 Redirect to dashboard after success
        return redirect('staff:dashboard')

    return redirect('staff:staff_ticket_detail', id=ticket.id)

# -----------------------------
# Delete Ticket 
# -----------------------------
@login_required(login_url='staff:login')
def delete_ticket(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    ticket.delete()
    messages.success(request, "Ticket deleted successfully.")
    return redirect('staff:dashboard')


# -----------------------------
# Printable Ticket Label
# -----------------------------
@login_required(login_url='staff:login')
def print_ticket(request, id):
    """Printable ticket for sticking on device"""
    ticket = get_object_or_404(Ticket, id=id)
    return render(request, 'staff/print_ticket.html', {'ticket': ticket})


@login_required(login_url='staff:login')
def regenerate_pdf(request, id):
    """Regenerate and save agreement PDF for existing ticket"""
    ticket = get_object_or_404(Ticket, id=id)
    pdf_buffer = generate_pdf(ticket)
    ticket.agreement_pdf.save(
        f'agreement_{ticket.tracking_id}.pdf',
        ContentFile(pdf_buffer.read()),
        save=False
    )
    ticket.save(update_fields=['agreement_pdf'])
    messages.success(request, f"PDF regenerated for {ticket.tracking_id}")
    return redirect('staff:staff_ticket_detail', id=ticket.id)



from django.shortcuts import render, redirect
from django.core.mail import EmailMessage
from tickets.models import Ticket
from django.contrib.auth.models import User
from tickets.utils import generate_pdf
from django.core.files.base import ContentFile

@login_required(login_url='staff:login')
def create_ticket(request):
    if request.method == "POST":
        # Ticket number (tracking_id) is auto-generated by the Ticket model.
        # Staff only needs to enter email + phone.
        email = (request.POST.get("client_email") or "").strip()
        phone = (request.POST.get("client_phone") or "").strip()

        # Optional staff fields (support both old split fields and new single `client_name`)
        client_name = (request.POST.get("client_name") or "").strip()

        first = (request.POST.get("client_first_name") or "").strip()
        last = (request.POST.get("client_last_name") or "").strip()

        if client_name:
            parts = client_name.split()
            first = parts[0] if parts else "Unknown"
            last = " ".join(parts[1:]) if len(parts) > 1 else ""

        if not first:
            first = "Unknown"

        device_type = (request.POST.get("device_type") or "").strip() or "Unknown"
        device_model = (request.POST.get("device_model") or "").strip() or None
        description = (request.POST.get("problem_description") or "").strip()
        price = request.POST.get("estimated_price") or 0

        # Validation: only email + phone are necessary
        if not email:
            messages.error(request, "Email is required.")
            return redirect('staff:dashboard')
        if not phone:
            messages.error(request, "Phone is required.")
            return redirect('staff:dashboard')

        # Get or create client
        user, _ = User.objects.get_or_create(
            username=email,
            defaults={
                "email": email,
                "first_name": first,
                "last_name": last,
            }
        )

        # Create ticket WITHOUT manual tracking_id
        ticket = Ticket.objects.create(
            title=f"{device_type} repair",
            description=description,
            client=user,
            client_phone=phone,
            device_type=device_type,
            device_model=device_model,
            estimated_price=price,
        )

        # Ensure agreement PDF is saved
        pdf = generate_pdf(ticket)
        if not ticket.agreement_pdf:
            ticket.agreement_pdf.save(
                f'agreement_{ticket.tracking_id}.pdf',
                ContentFile(pdf.read()),
                save=False
            )
            ticket.save(update_fields=['agreement_pdf'])

        # Send Email
        subject = f"Reparaturauftrag #{ticket.tracking_id}"
        body = f"""Hallo {first},

Ihr Reparaturauftrag wurde erstellt.

Tracking Nummer: {ticket.tracking_id}

Das PDF finden Sie im Anhang.

Mit freundlichen Grüßen  
Tanitech Team
"""

        msg = EmailMessage(subject, body, None, [email])
        msg.attach(
            f"auftrag_{ticket.tracking_id}.pdf",
            pdf.getvalue(),
            "application/pdf"
        )
        msg.send()

        messages.success(request, "Ticket created and email sent successfully.")
        return redirect("staff:dashboard")

    return redirect("staff:dashboard")




# -----------------------------
# Example view for landscape ticket orientation
# -----------------------------
from django.utils import timezone
@login_required(login_url='staff:login')
def export_tickets_csv(request):

    tickets = Ticket.objects.all().order_by('-created_at').values(
        'tracking_id', 'client__username', 'client__email', 'client_phone', 
        'device_type', 'device_model', 'description', 'status', 
        'estimated_price', 'created_at'
    )
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="tickets_export_' + str(request.user.username) + '_' + str(timezone.now().strftime('%Y%m%d_%H%M%S')) + '.csv"'
    
    fieldnames = [
        'tracking_id', 'client__username', 'client__email', 'client_phone',
        'device_type', 'device_model', 'description', 'status', 
        'estimated_price', 'created_at'
    ]
    writer = csv.DictWriter(response, fieldnames=fieldnames)
    writer.writeheader()
    for ticket in tickets:
        row = dict(ticket)
        writer.writerow(row)
    
    return response

def print_ticket_example(request):
    """Example page showing landscape ticket orientation"""
    return render(request, 'staff/print_ticket_example.html')
