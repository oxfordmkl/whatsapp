"""
Patch script: Merges all refactor changes into app.py
Replaces: smart reply engine, buttons, Gemini, welcome, fallback
Keeps: imports, config, INSTITUTE_INFO, course messages, KEYWORD_REPLIES,
       webhook, CRM, followups, broadcast, admin, panel, health
"""

# Read files
with open('app.py', 'r', encoding='utf-8') as f:
    original = f.read()

step1_path = r'C:\Users\admin\.gemini\antigravity\brain\69eaf56a-ee6e-4d41-b6e6-0ca8009dd6ea\scratch\step1_smart_reply.py'
step2_path = r'C:\Users\admin\.gemini\antigravity\brain\69eaf56a-ee6e-4d41-b6e6-0ca8009dd6ea\scratch\step2_buttons_gemini.py'

with open(step1_path, 'r', encoding='utf-8') as f:
    step1 = f.read()

with open(step2_path, 'r', encoding='utf-8') as f:
    step2 = f.read()

# ── SECTION 1: Replace smart reply engine (lines 576-900) ──
# Find the section markers
smart_reply_start = "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n# SMART REPLY ENGINE"
gemini_section = "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n# ✅ GEMINI AI REPLY"
crm_section = "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n# GOOGLE SHEETS CRM"

idx_smart = original.find(smart_reply_start)
idx_gemini = original.find(gemini_section)
idx_crm = original.find(crm_section)

if idx_smart == -1 or idx_gemini == -1 or idx_crm == -1:
    # Try with \r\n
    smart_reply_start = smart_reply_start.replace('\n', '\r\n')
    gemini_section = gemini_section.replace('\n', '\r\n')
    crm_section = crm_section.replace('\n', '\r\n')
    idx_smart = original.find(smart_reply_start)
    idx_gemini = original.find(gemini_section)
    idx_crm = original.find(crm_section)

assert idx_smart != -1, "Could not find SMART REPLY ENGINE section"
assert idx_crm != -1, "Could not find GOOGLE SHEETS CRM section"

# ── SECTION 2: Replace buttons/send section ──
send_section = "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n# SEND WHATSAPP MESSAGE"
broadcast_section = "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n# BROADCAST API"

idx_send = original.find(send_section)
idx_broadcast = original.find(broadcast_section)

if idx_send == -1 or idx_broadcast == -1:
    send_section = send_section.replace('\n', '\r\n')
    broadcast_section = broadcast_section.replace('\n', '\r\n')
    idx_send = original.find(send_section)
    idx_broadcast = original.find(broadcast_section)

assert idx_send != -1, "Could not find SEND WHATSAPP MESSAGE section"
assert idx_broadcast != -1, "Could not find BROADCAST API section"

# Build new file
new_content = (
    # Part 1: Everything before smart reply (imports, config, INSTITUTE_INFO, courses, keywords, webhook)
    original[:idx_smart]
    +
    # Part 2: New smart reply engine + welcome + Gemini + fallback
    step1 + "\n\n\n" + step2 + "\n\n\n"
    +
    # Part 3: CRM section (keep as-is)
    original[idx_crm:idx_send]
    +
    # Part 4: New button system + send_whatsapp_message (keep original send_whatsapp_message)
    "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "# SEND WHATSAPP MESSAGE\n"
    "# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    +
    # Extract just send_whatsapp_message and send_template_message from original
    _extract_send_functions(original, idx_send, idx_broadcast)
    +
    "\n\n"
    +
    # Part 5: Everything from broadcast onwards (broadcast, admin, panel, health, entry point)
    original[idx_broadcast:]
)

# Update the receive_message handler to use new btn_preset pattern
new_content = new_content.replace(
    'if exclude_btn == "NO_BUTTONS":\n            send_whatsapp_message(from_number, reply)\n        else:\n            send_interactive_message(from_number, reply, exclude_btn)',
    'if exclude_btn == "NO_BUTTONS":\n            send_whatsapp_message(from_number, reply)\n        else:\n            send_interactive_message(from_number, reply, exclude_btn)'
)
# Also handle \r\n version
new_content = new_content.replace(
    'if exclude_btn == "NO_BUTTONS":\r\n            send_whatsapp_message(from_number, reply)\r\n        else:\r\n            send_interactive_message(from_number, reply, exclude_btn)',
    'if exclude_btn == "NO_BUTTONS":\r\n            send_whatsapp_message(from_number, reply)\r\n        else:\r\n            send_interactive_message(from_number, reply, exclude_btn)'
)

with open('app.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("✅ app.py patched successfully!")


def _extract_send_functions(text, start_idx, end_idx):
    """Extract send_whatsapp_message and send_template_message from original"""
    section = text[start_idx:end_idx]
    # Find send_whatsapp_message
    swm_start = section.find("def send_whatsapp_message(")
    stm_start = section.find("def send_template_message(")
    if swm_start == -1:
        return ""
    # Get from send_whatsapp_message to end of section
    return section[swm_start:]
