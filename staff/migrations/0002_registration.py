from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone
from datetime import timedelta


class Migration(migrations.Migration):

    dependencies = [
        ('staff', '0001_initial'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='Registration',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('start_date', models.DateField(default=timezone.now)),
                ('end_date', models.DateField()),
                ('is_active', models.BooleanField(default=True)),
                ('monthly_fee_paid', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('payment_method', models.CharField(blank=True, max_length=50, null=True)),
                ('payment_reference', models.CharField(blank=True, max_length=100, null=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='registration', to='auth.user')),
            ],
        ),
    ]
