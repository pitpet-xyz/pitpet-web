# Generated by Django 3.2.6 on 2021-09-02 13:38

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sic", "0079_add_show_in_about_to_flatpages"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_validated",
            field=models.BooleanField(blank=True, default=False),
        ),
    ]
