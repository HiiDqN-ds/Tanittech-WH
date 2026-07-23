import os
import sys
sys.path.insert(0, 'c:/Users/Hamza/Desktop/repair_tracker')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'repair_system.settings')

import django
django.setup()

from django.contrib.auth.models import User
from tickets.models import Ticket

# Check all tickets
all_tickets = Ticket.objects.all()
print(f'Total tickets in database: {all_tickets.count()}')
for t in all_tickets:
    print(f'- {t.tracking_id} | {t.client.username} | {t.title} | {t.status}')

