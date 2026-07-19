from django.db import migrations


def rename_forward(apps, schema_editor):
    User = apps.get_model('procurement', 'User')
    User.objects.filter(role='procurement_officer').update(role='procurement_unit')


def rename_backward(apps, schema_editor):
    User = apps.get_model('procurement', 'User')
    User.objects.filter(role='procurement_unit').update(role='procurement_officer')


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0003_foundation_schema'),
    ]

    operations = [
        migrations.RunPython(rename_forward, rename_backward),
    ]
