
def get_all_tasks():
    from app.models import LeadEvent, ConversationState
    from datetime import datetime
    import json
    
    events = LeadEvent.query.filter(LeadEvent.event_type.in_(["FOLLOW_UP_TASK", "FOLLOW_UP_COMPLETED"])).all()
    leads = ConversationState.query.all()
    
    lead_map = {l.phone: l for l in leads}
    
    tasks = {}
    completed_task_ids = set()
    
    for ev in events:
        try:
            data = json.loads(ev.event_data or "{}")
        except:
            data = {}
            
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            tid = data.get("task_id")
            if tid:
                completed_task_ids.add(tid)
        elif ev.event_type == "FOLLOW_UP_TASK":
            tid = data.get("task_id")
            if tid:
                tasks[tid] = {
                    "task_id": tid,
                    "phone": ev.phone,
                    "lead_name": lead_map.get(ev.phone, type("obj", (object,), {"name": "Unknown"})).name or "Unknown",
                    "task": data.get("task", ""),
                    "due_date": data.get("due_date", ""),
                    "staff": data.get("staff", "Unassigned"),
                    "created_by": data.get("created_by", ""),
                    "created_at": ev.created_at,
                    "status": "OPEN",
                    "completed_by": None,
                    "completed_at": None
                }

    for ev in events:
        if ev.event_type == "FOLLOW_UP_COMPLETED":
            try:
                data = json.loads(ev.event_data or "{}")
            except:
                continue
            tid = data.get("task_id")
            if tid in tasks:
                tasks[tid]["status"] = "COMPLETED"
                tasks[tid]["completed_by"] = data.get("completed_by", "")
                tasks[tid]["completed_at"] = ev.created_at

    open_tasks = []
    completed_tasks = []
    
    today_dt = datetime.now()
    
    for t in tasks.values():
        if t["status"] == "OPEN":
            due = t.get("due_date", "")
            if due:
                try:
                    due_dt = datetime.strptime(due, "%Y-%m-%d")
                    diff = (today_dt.date() - due_dt.date()).days
                    if diff > 0:
                        if diff >= 7:
                            t["severity"] = "7+ Days Overdue"
                        elif diff >= 4:
                            t["severity"] = "4-7 Days Overdue"
                        else:
                            t["severity"] = "1-3 Days Overdue"
                        t["is_overdue"] = True
                        t["is_today"] = False
                        t["days_diff"] = diff
                    elif diff == 0:
                        t["severity"] = "Due Today"
                        t["is_overdue"] = False
                        t["is_today"] = True
                        t["days_diff"] = 0
                    else:
                        t["severity"] = "Upcoming"
                        t["is_overdue"] = False
                        t["is_today"] = False
                        t["days_diff"] = diff
                except:
                    t["severity"] = "Unknown"
                    t["is_overdue"] = False
                    t["is_today"] = False
                    t["days_diff"] = 0
            else:
                t["severity"] = "No Due Date"
                t["is_overdue"] = False
                t["is_today"] = False
                t["days_diff"] = 0
                
            open_tasks.append(t)
        else:
            completed_tasks.append(t)
            
    # Sort: overdue first (highest diff), then today, then upcoming
    open_tasks.sort(key=lambda x: x.get("days_diff", 0), reverse=True)
    completed_tasks.sort(key=lambda x: x.get("completed_at", datetime.min), reverse=True)
            
    return open_tasks, completed_tasks

@admin_bp.route("/crm/tasks/create", methods=["POST"])
def crm_tasks_create():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    phone = request.form.get("phone")
    task_title = request.form.get("task", "").strip()
    due_date = request.form.get("due_date", "").strip()
    staff = request.form.get("staff", "").strip()
    key = request.args.get("key", "")
    
    if not phone or not task_title or not due_date:
        return redirect(url_for("admin.crm_lead_detail", phone=phone, key=key))
        
    from app.services.log_service import log_lead_event
    import uuid
    import json
    
    task_id = uuid.uuid4().hex
    
    log_lead_event(
        phone=phone,
        event_type="FOLLOW_UP_TASK",
        event_data=json.dumps({
            "task_id": task_id,
            "lead_phone": phone,
            "task": task_title,
            "due_date": due_date,
            "staff": staff,
            "created_by": "Admin UX"
        })
    )
    
    return redirect(url_for("admin.crm_lead_detail", phone=phone, key=key))

@admin_bp.route("/crm/tasks/complete", methods=["POST"])
def crm_tasks_complete():
    # Supports both Form (from lead detail) and JSON (from dashboards)
    if request.args.get("key", "") != ADMIN_KEY and request.headers.get("X-Admin-Key") != ADMIN_KEY:
        return jsonify({"error": "Unauthorized"}), 401
        
    is_json = request.is_json
    
    if is_json:
        data = request.get_json(silent=True) or {}
        task_id = data.get("task_id")
        phone = data.get("phone")
        completed_by = data.get("completed_by", "Admin")
    else:
        task_id = request.form.get("task_id")
        phone = request.form.get("phone")
        completed_by = "Admin"
        
    if not task_id or not phone:
        if is_json:
            return jsonify({"error": "Missing parameters"}), 400
        else:
            return redirect(url_for("admin.crm_lead_detail", phone=phone, key=request.args.get("key", "")))
            
    from app.models import LeadEvent
    from app.services.log_service import log_lead_event
    import json
    
    # Duplicate completion protection
    existing = LeadEvent.query.filter_by(phone=phone, event_type="FOLLOW_UP_COMPLETED").all()
    already_completed = False
    for ev in existing:
        try:
            d = json.loads(ev.event_data or "{}")
            if d.get("task_id") == task_id:
                already_completed = True
                break
        except:
            pass
            
    if not already_completed:
        log_lead_event(
            phone=phone,
            event_type="FOLLOW_UP_COMPLETED",
            event_data=json.dumps({
                "task_id": task_id,
                "completed_by": completed_by
            })
        )
        
    if is_json:
        return jsonify({"success": True})
    else:
        return redirect(url_for("admin.crm_lead_detail", phone=phone, key=request.args.get("key", "")))

@admin_bp.route("/crm/tasks/my", methods=["GET"])
def crm_my_tasks():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    staff_name = request.args.get("staff", "").strip()
    open_tasks, completed_tasks = get_all_tasks()
    
    if staff_name:
        open_tasks = [t for t in open_tasks if t.get("staff") == staff_name]
        completed_tasks = [t for t in completed_tasks if t.get("staff") == staff_name]
        
    overdue = [t for t in open_tasks if t.get("is_overdue")]
    today = [t for t in open_tasks if t.get("is_today")]
    upcoming = [t for t in open_tasks if not t.get("is_overdue") and not t.get("is_today")]
    
    # Filter completed this week (simple implementation: last 7 days)
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=7)
    recent_completed = [t for t in completed_tasks if t.get("completed_at") and t.get("completed_at") > week_ago]
    
    registry = load_staff_registry()
    active_staff = [data["display_name"] for code, data in registry.items() if data.get("active")]
    active_staff.sort()
    
    return render_template(
        "crm_my_tasks.html",
        key=request.args.get("key", ""),
        staff_name=staff_name,
        active_staff=active_staff,
        overdue=overdue,
        today=today,
        upcoming=upcoming,
        completed=recent_completed
    )

@admin_bp.route("/crm/tasks/admin", methods=["GET"])
def crm_admin_tasks():
    if request.args.get("key", "") != ADMIN_KEY:
        return _deny()
        
    open_tasks, completed_tasks = get_all_tasks()
    
    staff_summary = {}
    registry = load_staff_registry()
    for code, data in registry.items():
        if data.get("active"):
            staff_summary[data["display_name"]] = {"pending": 0, "overdue": 0, "completed": 0}
            
    # Add staff dynamically if they have tasks but are no longer active
    for t in open_tasks + completed_tasks:
        s = t.get("staff", "Unassigned")
        if s not in staff_summary:
            staff_summary[s] = {"pending": 0, "overdue": 0, "completed": 0}
            
    for t in open_tasks:
        s = t.get("staff", "Unassigned")
        staff_summary[s]["pending"] += 1
        if t.get("is_overdue"):
            staff_summary[s]["overdue"] += 1
            
    # filter completed this week
    from datetime import datetime, timedelta
    week_ago = datetime.now() - timedelta(days=7)
    recent_completed = [t for t in completed_tasks if t.get("completed_at") and t.get("completed_at") > week_ago]
    
    for t in recent_completed:
        s = t.get("staff", "Unassigned")
        staff_summary[s]["completed"] += 1
        
    summary_list = [{"name": k, **v} for k, v in staff_summary.items()]
    summary_list.sort(key=lambda x: x["pending"], reverse=True)
    
    kpis = {
        "open": len(open_tasks),
        "today": len([t for t in open_tasks if t.get("is_today")]),
        "overdue": len([t for t in open_tasks if t.get("is_overdue")]),
        "completed_week": len(recent_completed)
    }
    
    return render_template(
        "crm_admin_tasks.html",
        key=request.args.get("key", ""),
        kpis=kpis,
        staff_summary=summary_list
    )
