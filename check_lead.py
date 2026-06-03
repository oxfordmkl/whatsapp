from app import create_app
from app.routes.admin import crm_course_admissions

with create_app().app_context():
    print('admin.py loaded successfully')
