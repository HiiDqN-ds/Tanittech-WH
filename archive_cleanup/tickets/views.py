from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.shortcuts import get_object_or_404
import uuid

from .models import Ticket
from .utils import generate_pdf
from django.core.files.base import ContentFile


# -----------------------------
# CREATE TICKET + SEND EMAIL WITH PDF
# -----------------------------
@csrf_exempt
def ticket_list(request):
    if request.method == "POST":
        try:
            first_name = request.POST.get("client_first_name")
            last_name = request.POST.get("client_last_name")
            client_name = request.POST.get("client_name")

            email = request.POST.get("client_email")
            phone = request.POST.get("client_phone")
            device_type = request.POST.get("device_type")
            device_model = request.POST.get("device_model")
            description = request.POST.get("problem_description") or request.POST.get("description", "")
            price = request.POST.get("estimated_price") or 0
            photo = request.FILES.get("device_photo")

            # All fields are optional (except email/phone). Handle missing/empty values with defaults.
            # Required fields validation
            if not email:
                return JsonResponse({"error": "Email is required"}, status=400)
            if not phone:
                return JsonResponse({"error": "Phone is required"}, status=400)

            # Some frontend versions send `client_name` instead of split fields.
            if (not first_name or not last_name) and client_name:
                full_name = client_name.strip()
                parts = full_name.split()
                first_name = parts[0] if parts else "Unknown"
                last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

            if not first_name:
                first_name = "Unknown"
            if not last_name:
                last_name = ""

            if not device_type:
                device_type = "Unknown"

            device_model = device_model or None
            description = (description or "").strip()
            price = price if price not in (None, "") else 0

            # Create / Get User
            user, created = User.objects.get_or_create(
                username=email,
                defaults={
                    "email": email,
                    "first_name": first_name,
                    "last_name": last_name,
                },
            )

            # Create Ticket (tracking_id generated automatically by model)
            ticket = Ticket.objects.create(
                title=f"{device_type} repair",
                description=description,
                client=user,
                client_phone=phone,
                device_type=device_type,
                device_model=device_model,
                estimated_price=price,
                device_photo=photo,
            )

            # Ensure agreement PDF is saved (model triggers, but explicit for buffer)
            pdf_buffer = generate_pdf(ticket)
            if not ticket.agreement_pdf:
                ticket.agreement_pdf.save(
                    f'agreement_{ticket.tracking_id}.pdf',
                    ContentFile(pdf_buffer.read()),
                    save=False
                )
                ticket.save(update_fields=['agreement_pdf'])

            # Send Email with PDF only if email is provided (not a guest)
            email_sent = False
            if email and not email.startswith("guest_"):
                try:
                    subject = f"Reparaturauftrag – Tracking #{ticket.tracking_id}"
                    body = f"""
Hallo {first_name} {last_name},

Ihr Reparaturauftrag wurde erfolgreich erstellt.

Tracking Nummer: {ticket.tracking_id}

Im Anhang finden Sie Ihren Auftrag als PDF.

Vielen Dank!
Tanitech Team
"""

                    email_msg = EmailMessage(subject, body, None, [email])
                    email_msg.attach(
                        f"auftrag_{ticket.tracking_id}.pdf",
                        pdf_buffer.getvalue(),
                        "application/pdf"
                    )
                    email_msg.send(fail_silently=False)
                    email_sent = True
                except Exception as email_exc:
                    # Email sending failed; return error to frontend for easier debugging
                    return JsonResponse(
                        {
                            "error": "Email sending failed",
                            "details": str(email_exc),
                        },
                        status=500,
                    )



            return JsonResponse({
                "success": True,
                "ticket_id": ticket.id,
                "tracking_id": ticket.tracking_id,
            })

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"error": "Invalid method"}, status=405)


# -----------------------------
# GET TICKET BY TRACKING ID
# -----------------------------
@csrf_exempt
def ticket_detail(request, tracking_id):
    try:
        ticket = Ticket.objects.get(tracking_id=tracking_id)

        data = {
            "tracking_id": ticket.tracking_id,
            "title": ticket.title,
            "description": ticket.description,
            "status": ticket.status,
            "client": f"{ticket.client.first_name} {ticket.client.last_name}",
            "device_type": ticket.device_type,
            "device_model": ticket.device_model,
            "estimated_price": float(ticket.estimated_price),
            "client_phone": ticket.client_phone,
            "created_at": ticket.created_at.strftime("%Y-%m-%d %H:%M"),
            "updated_at": ticket.updated_at.strftime("%Y-%m-%d %H:%M")
        }

        return JsonResponse(data)

    except Ticket.DoesNotExist:
        return JsonResponse({"error": "Ticket not found"}, status=404)


# -----------------------------
# Manual PDF View (Download/Preview)
# -----------------------------
def generate_auftrag(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    # Use SAME PDF style from utils.py
    pdf_buffer = generate_pdf(ticket)

    response = HttpResponse(pdf_buffer, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="auftrag_{ticket.tracking_id}.pdf"'
    return response
    
    
    
