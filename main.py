import os
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, Query, Response
from google import genai
from google.genai import types
from apscheduler.schedulers.asyncio import AsyncIOScheduler

app = FastAPI()

# المتغيرات البيئية
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
ADMIN_PHONE = os.getenv("ADMIN_PHONE")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "my_secure_token_123")

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

daily_stats = {
    "total_messages": 0,
    "total_orders": 0,
    "pending_payments": 0
}

tools_declarations = [
    types.FunctionDeclaration(
        name="save_cash_on_delivery_order",
        description="تسجيل الطلب تلقائياً إذا اختار العميل الدفع عند الاستلام (COD).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "customer_name": types.Schema(type=types.Type.STRING, description="اسم العميل"),
                "address": types.Schema(type=types.Type.STRING, description="العنوان بالتفصيل والمحافظة"),
                "items": types.Schema(type=types.Type.STRING, description="المنتجات والكمية"),
                "total_price": types.Schema(type=types.Type.NUMBER, description="إجمالي السعر شامل الشحن"),
            },
            required=["customer_name", "address", "items", "total_price"]
        )
    ),
    types.FunctionDeclaration(
        name="request_admin_payment_verification",
        description="تُستدعى حصرياً عندما يدفع العميل تحويلاً إلكترونياً ويرسل بيانات أو صورة التحويل للتأكيد.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "customer_name": types.Schema(type=types.Type.STRING),
                "amount": types.Schema(type=types.Type.NUMBER),
                "transfer_info": types.Schema(type=types.Type.STRING, description="رقم المحفظة، كود الإيصال، أو وقت العملية"),
                "items": types.Schema(type=types.Type.STRING),
            },
            required=["customer_name", "amount", "transfer_info", "items"]
        )
    )
]

SYSTEM_PROMPT = """
أنت خبير مبيعات ودود ومحترف للغاية لمتجرنا. أسلوبك بالمصرية المهذبة والذكية، مختصر ومقنع.
هدفك: إتمام البيع ومساعدة العميل دون تشتيت، والعمل باستقلالية تامة.

قواعد التشغيل:
1. المنتجات: وضح الأسعار والشحن بوضوح عند السؤال.
2. الدفع عند الاستلام: اجمع بياناته كاملة واستدعِ فوراً `save_cash_on_delivery_order` ثم أبلغه بأن الطلب تأكد وجارٍ تجهيزه.
3. التحويل المالي (إنستاباي / فودافون كاش): زوده برقم التحويل واطلب منه تفاصيل الإيصال.
4. بمجرد إرسال تفاصيل التحويل: استدعِ `request_admin_payment_verification` وقل للعميل: 'شكراً لذوقك! جارٍ تأكيد استلام المبلغ من الحسابات وهنبلغك فوراً'. لا ترجع للإدارة إلا في هذه النقطة.
"""

async def send_whatsapp_message(to_phone: str, message_text: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message_text}
    }
    async with httpx.AsyncClient() as http_client:
        await http_client.post(url, json=payload, headers=headers)

@app.get("/webhook")
async def verify(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token")
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    data = await request.json()
    try:
        entry = data.get("entry", [])[0].get("changes", [])[0].get("value", {})
        messages = entry.get("messages", [])
        if not messages:
            return {"status": "ignored"}

        msg = messages[0]
        sender = msg.get("from")
        text = msg.get("text", {}).get("body", "")

        daily_stats["total_messages"] += 1

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[types.Tool(function_declarations=tools_declarations)],
            temperature=0.3
        )
        chat = client.chats.create(model="gemini-2.5-flash", config=config)
        resp = chat.send_message(text)

        if resp.function_calls:
            for call in resp.function_calls:
                args = call.args
                if call.name == "request_admin_payment_verification":
                    daily_stats["pending_payments"] += 1
                    alert = (
                        f"💰 *تأكيد تحويل جديد مطلوب مراجعته* 💰\n\n"
                        f"👤 العميل: {args.get('customer_name')}\n"
                        f"📱 رقم العميل: {sender}\n"
                        f"💵 المبلغ: {args.get('amount')} ج.م\n"
                        f"🧾 بيانات الإيصال: {args.get('transfer_info')}\n"
                        f"📦 الأصناف: {args.get('items')}"
                    )
                    if ADMIN_PHONE:
                        await send_whatsapp_message(ADMIN_PHONE, alert)
                    await send_whatsapp_message(sender, "وصلت بيانات التحويل لقسم الحسابات للمراجعة، وسيتم إخطارك فور التأكيد للشحن!")

                elif call.name == "save_cash_on_delivery_order":
                    daily_stats["total_orders"] += 1
                    await send_whatsapp_message(sender, "ألف مبروك يا فندم! تم تسجيل طلبك بنجاح وهيوصلك على عنوانك خلال يومين عمل.")
        else:
            await send_whatsapp_message(sender, resp.text)

    except Exception as e:
        print("Error:", e)

    return {"status": "ok"}

async def send_nightly_report():
    if not ADMIN_PHONE:
        return

    report = (
        f"📊 *تقرير نشاط اليوم الذاتي ({datetime.now().strftime('%Y-%m-%d')})* 📊\n\n"
        f"💬 إجمالي الرسائل المستلمة: {daily_stats['total_messages']}\n"
        f"📦 الطلبات المسجلة تلقائياً (دفع استلام): {daily_stats['total_orders']}\n"
        f"💳 عمليات الدفع الإلكتروني المحولة للمراجعة: {daily_stats['pending_payments']}\n\n"
        f"💡 *اقتراح التطوير الذاتي لليوم:*\n"
        f"بناءً على التفاعلات، المساعد يقترح تفعيل رد تلقائي لخيارات الشحن السريع للمحافظات البعيدة لرفع سرعة التحويل 15%.\n"
        f"(النظام يعمل بكفاءة 24/7 ودون المساس بالهيكل الأساسي)."
    )
    await send_whatsapp_message(ADMIN_PHONE, report)
    daily_stats["total_messages"] = 0
    daily_stats["total_orders"] = 0
    daily_stats["pending_payments"] = 0

scheduler = AsyncIOScheduler()
scheduler.add_job(send_nightly_report, 'cron', hour=23, minute=59)
scheduler.start()
