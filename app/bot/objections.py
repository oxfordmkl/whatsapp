from app.bot.constants import FEES_VALUE_LINES, pick

def detect_objection(low: str) -> str | None:
    """Return objection type string or None."""
    if any(x in low for x in ["fees high", "fee high", "high fee", "rate high", "expensive",
                               "costly", "afford", "budget", "kooduthal",
                               "\u0d15\u0d42\u0d1f\u0d41\u0d24\u0d32\u0d4d", "high aanu"]):
        return "fees_high"
    if any(x in low for x in ["think", "nokkatte", "alochikkam", "later", "pinne",
                               "\u0d28\u0d4b\u0d15\u0d4d\u0d15\u0d1f\u0d4d\u0d1f\u0d46", "\u0d06\u0d32\u0d4b\u0d1a\u0d3f\u0d15\u0d4d\u0d15\u0d3e\u0d02"]):
        return "think_later"
    if any(x in low for x in ["interest illa", "not interested", "vend",
                               "\u0d35\u0d47\u0d23\u0d4d\u0d1f", "illa interest"]):
        return "not_interested"
    if any(x in low for x in ["time illa", "busy", "samayam illa", "\u0d38\u0d2e\u0d2f\u0d02 \u0d07\u0d32\u0d4d\u0d32"]):
        return "time_issue"
    if any(x in low for x in ["already job", "job und", "working", "work cheyyunnu"]):
        return "already_working"
    if any(x in low for x in ["confused", "doubt", "ariyilla", "not sure", "\u0d38\u0d02\u0d36\u0d2f\u0d02"]):
        return "confused"
    if any(x in low for x in ["free undo", "free aano", "free course", "\u0d38\u0d57\u0d1c\u0d28\u0d4d\u0d2f\u0d02"]):
        return "free_ask"
    return None


def handle_objection(kind: str, name: str, st: dict) -> tuple[str, str | None]:
    """Return (reply_text, preset) for known objection types."""
    from app.bot.constants import CONFUSED_LINES
    if kind == "fees_high":
        text = (
            "Athu doubt varunnath normal aanu 😊\n\n"
            "Pakshe ithu expense alla\u2026 skill investment aanu 👍\n"
            f"{pick(FEES_VALUE_LINES)}\n\n"
            "Demo kaanumbo value clear aavum\u2026 book cheyyatte? 🎓"
        )
        return text, "FEES"

    if kind == "think_later":
        text = (
            "Sure 😊 take your time.\n\n"
            "Pakshe demo kaanathe decision edukkaruthu 👍\n"
            "Just 1 free class kaanumbo full clarity varum.\n\n"
            "Demo book cheyyatte?"
        )
        return text, "COURSE"

    if kind == "not_interested":
        text = (
            "Ok 😊 problem illa.\n\n"
            "Just ariyan\u2026 interest illa ennath course type kondaano,\n"
            "time issue aano, alle fees concern aano?\n\n"
            "Njan better option suggest cheyyam 👍"
        )
        return text, "COURSE"

    if kind == "time_issue":
        text = (
            "Athu common issue aanu 😊\n\n"
            "Athinu flexible batches und — morning / evening choose cheyyam 👍\n"
            "Schedule adjust cheythu padikkaam.\n\n"
            "Preferred time parayamo?"
        )
        return text, "COURSE"

    if kind == "already_working":
        text = (
            "Super 👍 already working aanenkil ithu upgrade aayi use cheyyam.\n\n"
            "Better salary / better role kittan extra skill help cheyyum 💪\n"
            "Part-time batch option und.\n\n"
            "Demo kaanumbo idea clear aavum\u2026 varamo?"
        )
        return text, "COURSE"

    if kind == "confused":
        text = (
            f"{pick(CONFUSED_LINES)}\n\n"
            "Ningal +2 / Degree / Working aano?\n"
            "Job aanu main goal alle?\n\n"
            "Reply cheyyoo — best course njan suggest cheyyam 🎓"
        )
        st["stage"] = "not_sure"
        return text, "GOAL"

    if kind == "free_ask":
        text = (
            "Full course free alla 😊\n\n"
            "Pakshe free demo class und 👍\n"
            "Athil course, fees, timing ellaam clear aayi explain cheyyam.\n\n"
            "Demo book cheyyatte?"
        )
        return text, "COURSE"

    return None, None
