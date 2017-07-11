# -*- coding: utf-8 -*-
# Generated by Django 1.11.2 on 2017-07-10 12:51
from __future__ import unicode_literals

import autoslug.fields
from django.db import migrations
import lib.utils


class Migration(migrations.Migration):

    dependencies = [
        ('devices', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='deviceentry',
            name='slug',
            field=autoslug.fields.AutoSlugField(always_update=True, editable=False, null=True, populate_from=lib.utils.secure_random_id, unique=True),
        ),
    ]
