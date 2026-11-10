from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0002_refunded_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="origin",
            field=models.CharField(
                choices=[("stripe", "Compra con Stripe"), ("agreement", "Código de convenio")],
                db_index=True, default="stripe", max_length=16,
            ),
        ),
    ]
