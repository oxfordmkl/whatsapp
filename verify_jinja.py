import os
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader('templates'))
for template_name in env.list_templates():
    if template_name.endswith('.html'):
        try:
            env.get_template(template_name)
            print(f"PASS: {template_name}")
        except Exception as e:
            print(f"FAIL: {template_name} - {str(e)}")
