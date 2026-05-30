import sys
sys.path.append(r'd:\oxford\2026\theoxfordedu-main\whatsapp API\oxford-whatsapp_2')

from app import create_app
from app.config import ADMIN_KEY

app = create_app()

with app.test_client() as client:
    response = client.post(f"/crm/lead/919995787020/update?key={ADMIN_KEY}", data={
        "lead_status": "Contacted",
        "assigned_staff": "anju",
        "lead_score": "50",
        "is_admitted": "0",
        "notes": "test"
    })
    print(response.status_code)
    if response.status_code == 500:
        print(response.data.decode('utf-8'))
