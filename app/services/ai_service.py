from google import genai
from app.config import GEMINI_API_KEY
from app.bot.prompts import AALIZA_PROMPT

if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ Gemini AI initialised (google-genai SDK, gemini-2.0-flash)")
else:
    gemini_client = None
    print("⚠️  GEMINI_API_KEY not set — AI replies disabled")

def gemini_reply(user_msg: str, name: str, context: str = "") -> str | None:
    if not gemini_client:
        return None
    try:
        prompt = (
            f"{AALIZA_PROMPT}\n\n"
            f"{'Conversation so far:\n' + context + chr(10) if context else ''}"
            f"Student name: {name}\n"
            f"Student says: \"{user_msg}\"\n\n"
            f"Reply as Oxford Nova:"
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        err = str(e).lower()
        if "429" in str(e) or "quota" in err or "resource" in err:
            print("⚠️  Gemini quota exceeded")
        else:
            print(f"⚠️  Gemini error: {e}")
        return None

def smart_fallback(name: str, msg: str = "") -> str:
    m = msg.lower()
    if any(w in m for w in ["fee", "price", "cost", "vila", "ethra","fees"]):
        return (
            f"😊 {name}, fees ariyaan government approved rates und!\n\n"
            "Courses ₹1,999 muthal thudangunnu.\n"
            "EMI / installment option um und! 📊\n\n"
            "Exact fee kaanan: *FEES* reply cheyyoo 💰\n"
            "Coursukal kanan *COURSES* ennu reply cheyyuka 📚\n"
            "📞 9447329972"
        )
    if any(w in m for w in ["job", "placement", "work", "career"]):
        return (
            f"{name}, nalla chodyam! 💪\n\n"
            "Oxford-il 100% placement assistance und.\n"
            "Students Kerala & Gulf-il work cheyyunnu. 🌍\n\n"
            "Best course ariyaan: *COURSES* reply cheyyoo 📚\n"
            "Or demo: *DEMO* 🎓"
        )
    return (
        f"😊 Nandi {name}!\n\n"
        "Njan Oxford Nova — The Oxford Computers-nte counselor.\n"
        "Ningalkku njan enthu help cheyyanam?\n\n"
        "📚 *COURSES* | 🎓 *DEMO* | 💰 *FEES*\n"
        "📞 9447329972"
    )
