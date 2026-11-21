import os, asyncio, logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("game-rewards")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
OWNER_ID = int(os.getenv("OWNER_ID", "9022804804"))
DAILY_DONATION_CAP = 10000

bot = None
dp = Dispatcher()
db: Client | None = None
pending_flow = {}

def is_admin(uid: int) -> bool:
    if uid == OWNER_ID: return True
    if db is None: return False
    try:
        return bool(db.table("admins").select("telegram_id").eq("telegram_id", uid).limit(1).execute().data)
    except Exception:
        return False

def admin_ids():
    rows = db.table("admins").select("telegram_id").execute().data or []
    ids = {int(x["telegram_id"]) for x in rows}
    ids.add(OWNER_ID)
    return list(ids)

def log_action(actor, action, target_type=None, target_id=None, details=None):
    db.table("audit_logs").insert({"actor_id":actor,"action":action,"target_type":target_type,"target_id":str(target_id) if target_id else None,"details":details or {}}).execute()

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Запросить донат", callback_data="request:donation"),
         InlineKeyboardButton(text="💰 Запросить валюту", callback_data="request:currency")],
        [InlineKeyboardButton(text="🏆 Ивенты", callback_data="events"),
         InlineKeyboardButton(text="📨 Поддержка", callback_data="support")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="my_requests")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 Заявки", callback_data="admin:requests"),
         InlineKeyboardButton(text="📸 Работы по ивентам", callback_data="admin:submissions")],
        [InlineKeyboardButton(text="➕ Создать ивент", callback_data="admin:create_event"),
         InlineKeyboardButton(text="📢 Активные ивенты", callback_data="admin:events")],
        [InlineKeyboardButton(text="👥 Администраторы", callback_data="admin:roles")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")]
    ])

async def notify_admins(text, reply_markup=None):
    for aid in admin_ids():
        try: await bot.send_message(aid, text, reply_markup=reply_markup)
        except Exception as e: log.warning("Admin notification failed for %s: %s", aid, e)

def player_upsert(m: Message):
    db.table("players").upsert({
        "telegram_id":m.from_user.id,
        "username":m.from_user.username,
        "first_name":m.from_user.first_name,
        "last_seen_at":datetime.now(timezone.utc).isoformat()
    }).execute()

@dp.message(CommandStart())
async def start(m: Message):
    player_upsert(m)
    text = "🎮 <b>Центр игровых наград</b>\n\nЗдесь можно отправить заявку на донат или валюту, участвовать в ивентах и связаться с администрацией."
    await m.answer(text, reply_markup=main_kb())
    if is_admin(m.from_user.id):
        await m.answer("🔐 <b>Панель администратора</b>", reply_markup=admin_kb())

@dp.message(Command("admin"))
async def admin_command(m: Message):
    if not is_admin(m.from_user.id): return await m.answer("⛔ Нет доступа.")
    await m.answer("🔐 <b>Панель администратора</b>", reply_markup=admin_kb())

@dp.callback_query(F.data.startswith("request:"))
async def request_start(c: CallbackQuery):
    kind = c.data.split(":")[1]
    pending_flow[c.from_user.id] = {"step":"amount","kind":kind}
    label = "доната" if kind=="donation" else "игровой валюты"
    await c.message.answer(f"Введите сумму {label} числом. Для доната действует общий лимит 10 000 в сутки.")
    await c.answer()

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="amount")
async def request_amount(m: Message):
    try:
        amount = float(m.text.replace(",", ".").replace(" ", ""))
        if amount <= 0 or amount > 100000000: raise ValueError()
    except Exception:
        return await m.answer("Введите корректную сумму числом.")
    flow = pending_flow[m.from_user.id]
    if flow["kind"] == "donation":
        today = datetime.now(timezone.utc).date().isoformat()
        rows = db.table("reward_requests").select("amount").eq("kind","donation").gte("created_at",today+"T00:00:00+00:00").in_("status",["pending","approved","delivered"]).execute().data or []
        total = sum(float(x["amount"]) for x in rows)
        if total + amount > DAILY_DONATION_CAP:
            pending_flow.pop(m.from_user.id,None)
            return await m.answer(f"Дневной лимит доната 10 000. Сейчас уже запрошено: {total:g}.")
    flow.update({"step":"note","amount":amount})
    await m.answer("Добавьте комментарий к заявке или отправьте «-», если комментарий не нужен.")

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="note")
async def request_note(m: Message):
    flow = pending_flow.pop(m.from_user.id)
    note = "" if (m.text or "").strip()=="-" else (m.text or "")[:1000]
    db.table("reward_requests").insert({"telegram_id":m.from_user.id,"kind":flow["kind"],"amount":flow["amount"],"note":note}).execute()
    row = db.table("players").select("username,first_name").eq("telegram_id",m.from_user.id).maybe_single().execute().data or {}
    who = f"{row.get('first_name','Игрок')} (@{row.get('username')})" if row.get("username") else row.get("first_name","Игрок")
    r = db.table("reward_requests").select("id").eq("telegram_id",m.from_user.id).order("created_at",desc=True).limit(1).execute().data[0]
    rid = r["id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить",callback_data=f"req:approve:{rid}"),
        InlineKeyboardButton(text="❌ Отклонить",callback_data=f"req:reject:{rid}")
    ]])
    await notify_admins(f"🆕 <b>Новая заявка</b>\nТип: {flow['kind']}\nСумма: {flow['amount']:g}\nИгрок: {who}\nTelegram ID: <code>{m.from_user.id}</code>\nКомментарий: {note or '—'}",kb)
    await m.answer("✅ Заявка отправлена администрации. Статус можно посмотреть в «Мои заявки».",reply_markup=main_kb())

@dp.callback_query(F.data=="my_requests")
async def my_requests(c: CallbackQuery):
    rows = db.table("reward_requests").select("kind,amount,status,created_at").eq("telegram_id",c.from_user.id).order("created_at",desc=True).limit(10).execute().data or []
    if not rows: return await c.answer("Заявок пока нет.",show_alert=True)
    lines = ["<b>Ваши последние заявки:</b>"]
    labels={"pending":"ожидает проверки","approved":"одобрена — ожидайте выдачу","rejected":"отклонена","delivered":"выдано в игре"}
    for x in rows: lines.append(f"• {x['kind']} — {x['amount']:g}: {labels.get(x['status'],x['status'])}")
    await c.message.answer("\n".join(lines)); await c.answer()

@dp.callback_query(F.data.startswith("req:"))
async def review_request(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    _,action,rid = c.data.split(":",2)
    row = db.table("reward_requests").select("*").eq("id",rid).maybe_single().execute().data
    if not row: return await c.answer("Заявка не найдена",show_alert=True)
    if row["status"] not in ("pending","approved"): return await c.answer("Заявка уже закрыта",show_alert=True)
    if action=="approve":
        db.table("reward_requests").update({"status":"approved","reviewed_by":c.from_user.id,"reviewed_at":datetime.now(timezone.utc).isoformat()}).eq("id",rid).execute()
        await bot.send_message(int(row["telegram_id"]),f"✅ Ваша заявка на {row['amount']:g} одобрена. Администратор подготовит выдачу в игре. Когда награда будет выдана, вы получите уведомление.")
        await c.message.answer(f"Одобрено. После ручной выдачи в игре нажмите «Выдано».",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Выдано в игре",callback_data=f"req:delivered:{rid}")]]))
    elif action=="reject":
        db.table("reward_requests").update({"status":"rejected","reviewed_by":c.from_user.id,"reviewed_at":datetime.now(timezone.utc).isoformat()}).eq("id",rid).execute()
        await bot.send_message(int(row["telegram_id"]),"❌ Ваша заявка отклонена. Напишите в поддержку, если нужны подробности.")
        await c.message.answer("Заявка отклонена.")
    elif action=="delivered":
        db.table("reward_requests").update({"status":"delivered","reviewed_by":c.from_user.id,"delivered_at":datetime.now(timezone.utc).isoformat()}).eq("id",rid).execute()
        await bot.send_message(int(row["telegram_id"]),"🎉 Сейчас можете получить свою награду в игре. Подойдите к администратору.")
        await c.message.answer("Отмечено как выданное.")
    log_action(c.from_user.id,"request_"+action,"reward_request",rid)
    await c.answer("Готово")

@dp.callback_query(F.data=="events")
async def show_events(c: CallbackQuery):
    rows=db.table("events").select("id,title,description,prize,closes_at").eq("status","active").order("created_at",desc=True).execute().data or []
    if not rows: await c.message.answer("Сейчас активных ивентов нет.")
    for e in rows:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📤 Отправить выполнение",callback_data=f"event:submit:{e['id']}")]])
        await c.message.answer(f"🏆 <b>{e['title']}</b>\n{e['description']}\nПриз: {e['prize'] or 'указан администрацией'}",reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("event:submit:"))
async def event_submit_start(c: CallbackQuery):
    eid=c.data.split(":",2)[2]
    e=db.table("events").select("id,title,tasks").eq("id",eid).eq("status","active").maybe_single().execute().data
    if not e: return await c.answer("Ивент уже закрыт.",show_alert=True)
    pending_flow[c.from_user.id]={"step":"event_evidence","event_id":eid}
    tasks=e.get("tasks") or []
    task_text="\n".join(f"{i+1}. {t}" for i,t in enumerate(tasks)) or "Выполните условия и прикрепите подтверждение."
    await c.message.answer(f"📸 <b>{e['title']}</b>\n{task_text}\n\nОтправьте фото или видео выполнения с подписью, если нужно.")
    await c.answer()

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="event_evidence")
async def event_evidence(m: Message):
    flow=pending_flow[m.from_user.id]
    if m.photo: fid=m.photo[-1].file_id; typ="photo"
    elif m.video: fid=m.video.file_id; typ="video"
    elif m.document: fid=m.document.file_id; typ="document"
    else: return await m.answer("Пришлите именно фото, видео или файл.")
    try:
        db.table("event_submissions").insert({"event_id":flow["event_id"],"telegram_id":m.from_user.id,"caption":m.caption or "","file_id":fid,"file_type":typ}).execute()
    except Exception:
        pending_flow.pop(m.from_user.id,None)
        return await m.answer("Вы уже отправляли выполнение этого задания. Обратитесь к администрации, если нужно заменить файл.")
    pending_flow.pop(m.from_user.id,None)
    sub=db.table("event_submissions").select("id").eq("event_id",flow["event_id"]).eq("telegram_id",m.from_user.id).order("created_at",desc=True).limit(1).execute().data[0]
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять",callback_data=f"sub:approve:{sub['id']}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"sub:reject:{sub['id']}")]])
    for aid in admin_ids():
        try:
            await bot.send_message(aid,f"📨 Новое выполнение ивента\nИгрок ID: <code>{m.from_user.id}</code>\nИвент: {flow['event_id']}")
            if typ=="photo": await bot.send_photo(aid,fid,caption=m.caption or "",reply_markup=kb)
            elif typ=="video": await bot.send_video(aid,fid,caption=m.caption or "",reply_markup=kb)
            else: await bot.send_document(aid,fid,caption=m.caption or "",reply_markup=kb)
        except Exception as ex: log.warning("submission notify: %s",ex)
    await m.answer("✅ Материалы отправлены администрации на проверку.")

@dp.callback_query(F.data.startswith("sub:"))
async def review_submission(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    _,act,sid=c.data.split(":",2)
    row=db.table("event_submissions").select("*").eq("id",sid).maybe_single().execute().data
    if not row: return await c.answer("Не найдено",show_alert=True)
    status="approved" if act=="approve" else "rejected"
    db.table("event_submissions").update({"status":status,"reviewed_by":c.from_user.id,"reviewed_at":datetime.now(timezone.utc).isoformat()}).eq("id",sid).execute()
    await bot.send_message(int(row["telegram_id"]),"✅ Ваше выполнение принято. Администратор сообщит о выдаче приза." if act=="approve" else "❌ Материалы не приняты. Уточните причину в поддержке.")
    if act=="approve":
        await c.message.answer("Принято. Приз выдаётся администратором вручную в игре. После выдачи используйте команду /rewarded "+sid)
    log_action(c.from_user.id,"submission_"+act,"event_submission",sid)
    await c.answer("Сохранено")

@dp.message(Command("rewarded"))
async def rewarded(m: Message):
    if not is_admin(m.from_user.id): return
    parts=(m.text or "").split(maxsplit=1)
    if len(parts)<2: return await m.answer("Использование: /rewarded ID_ЗАЯВКИ")
    sid=parts[1].strip()
    row=db.table("event_submissions").select("*").eq("id",sid).maybe_single().execute().data
    if not row: return await m.answer("Заявка не найдена.")
    db.table("event_submissions").update({"status":"rewarded","reviewed_by":m.from_user.id,"reviewed_at":datetime.now(timezone.utc).isoformat()}).eq("id",sid).execute()
    await bot.send_message(int(row["telegram_id"]),"🏆 Приз выдан! Сейчас можете получить его в игре у администратора.")
    log_action(m.from_user.id,"event_rewarded","event_submission",sid)
    await m.answer("Игрок уведомлён.")

@dp.callback_query(F.data=="support")
async def support(c: CallbackQuery):
    pending_flow[c.from_user.id]={"step":"support"}
    await c.message.answer("Напишите сообщение администрации. Можно приложить текст; мы передадим его администраторам.")
    await c.answer()

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="support")
async def support_message(m: Message):
    text_body=(m.text or m.caption or "").strip()
    if not text_body: return await m.answer("Пожалуйста, добавьте текст сообщения.")
    db.table("support_messages").insert({"telegram_id":m.from_user.id,"sender_id":m.from_user.id,"sender_role":"player","body":text_body}).execute()
    pending_flow.pop(m.from_user.id,None)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Ответить игроку",callback_data=f"support:reply:{m.from_user.id}")]])
    await notify_admins(f"📨 <b>Сообщение в поддержку</b>\nИгрок ID: <code>{m.from_user.id}</code>\n{text_body}",kb)
    await m.answer("Сообщение отправлено администрации.")

@dp.callback_query(F.data.startswith("support:reply:"))
async def support_reply_start(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    pid=int(c.data.rsplit(":",1)[1])
    pending_flow[c.from_user.id]={"step":"admin_reply","player_id":pid}
    await c.message.answer(f"Напишите ответ игроку ID {pid}.")
    await c.answer()

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="admin_reply")
async def support_reply(m: Message):
    flow=pending_flow.pop(m.from_user.id)
    body=(m.text or m.caption or "").strip()
    if not body: return await m.answer("Пустой ответ не отправлен.")
    pid=flow["player_id"]
    db.table("support_messages").insert({"telegram_id":pid,"sender_id":m.from_user.id,"sender_role":"admin","body":body}).execute()
    await bot.send_message(pid,"💬 <b>Ответ администрации:</b>\n"+body)
    await m.answer("Ответ отправлен.")

@dp.callback_query(F.data=="admin:requests")
async def admin_requests(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    rows=db.table("reward_requests").select("*").in_("status",["pending","approved"]).order("created_at").limit(20).execute().data or []
    if not rows: await c.message.answer("Открытых заявок нет.")
    for x in rows:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Одобрить",callback_data=f"req:approve:{x['id']}"),InlineKeyboardButton(text="❌ Отклонить",callback_data=f"req:reject:{x['id']}")]])
        if x["status"]=="approved": kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Выдано в игре",callback_data=f"req:delivered:{x['id']}")]])
        await c.message.answer(f"Заявка {x['id']}\nИгрок ID: <code>{x['telegram_id']}</code>\nТип: {x['kind']}\nСумма: {x['amount']}\nСтатус: {x['status']}\nКомментарий: {x.get('note') or '—'}",reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data=="admin:submissions")
async def admin_submissions(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    rows=db.table("event_submissions").select("*").eq("status","pending").order("created_at").limit(15).execute().data or []
    if not rows: await c.message.answer("Новых материалов нет.")
    for x in rows:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Принять",callback_data=f"sub:approve:{x['id']}"),InlineKeyboardButton(text="Отклонить",callback_data=f"sub:reject:{x['id']}")]])
        await c.message.answer(f"Игрок ID: <code>{x['telegram_id']}</code>\nИвент: {x['event_id']}\nПодпись: {x.get('caption') or '—'}",reply_markup=kb)
        if x["file_type"]=="photo": await c.message.answer_photo(x["file_id"])
        elif x["file_type"]=="video": await c.message.answer_video(x["file_id"])
        else: await c.message.answer_document(x["file_id"])
    await c.answer()

@dp.callback_query(F.data=="admin:create_event")
async def create_event_start(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    pending_flow[c.from_user.id]={"step":"event_title"}
    await c.message.answer("Введите название ивента.")
    await c.answer()

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="event_title")
async def event_title(m: Message):
    pending_flow[m.from_user.id]={"step":"event_description","title":(m.text or "")[:150]}
    await m.answer("Введите описание и условия. Можно перечислить задания с новой строки.")

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="event_description")
async def event_description(m: Message):
    pending_flow[m.from_user.id].update({"step":"event_prize","description":(m.text or "")[:3000]})
    await m.answer("Введите приз и способ его получения.")

@dp.message(lambda m: m.from_user.id in pending_flow and pending_flow[m.from_user.id].get("step")=="event_prize")
async def event_prize(m: Message):
    f=pending_flow.pop(m.from_user.id)
    tasks=[x.strip() for x in f["description"].splitlines() if x.strip()]
    db.table("events").insert({"title":f["title"],"description":f["description"],"tasks":tasks,"prize":(m.text or "")[:500],"created_by":m.from_user.id,"status":"active"}).execute()
    log_action(m.from_user.id,"event_created","event",None,{"title":f["title"]})
    await m.answer("✅ Ивент опубликован. Игроки увидят его в разделе «Ивенты».")

@dp.callback_query(F.data=="admin:events")
async def admin_events(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    rows=db.table("events").select("id,title,status").order("created_at",desc=True).limit(20).execute().data or []
    for e in rows:
        kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔒 Закрыть",callback_data=f"event:close:{e['id']}")]]) if e["status"]=="active" else None
        await c.message.answer(f"{e['title']} — {e['status']}\nID: {e['id']}",reply_markup=kb)
    await c.answer()

@dp.callback_query(F.data.startswith("event:close:"))
async def event_close(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    eid=c.data.split(":",2)[2]
    db.table("events").update({"status":"closed"}).eq("id",eid).execute()
    log_action(c.from_user.id,"event_closed","event",eid)
    await c.answer("Ивент закрыт")

@dp.callback_query(F.data=="admin:roles")
async def roles(c: CallbackQuery):
    if c.from_user.id!=OWNER_ID: return await c.answer("Только владелец",show_alert=True)
    rows=db.table("admins").select("telegram_id,role").execute().data or []
    lines=["<b>Администраторы:</b>"]+[f"• {x['telegram_id']} — {x['role']}" for x in rows]
    await c.message.answer("\n".join(lines)+"\n\nНазначение: /addadmin TELEGRAM_ID\nСнятие: /deladmin TELEGRAM_ID")
    await c.answer()

@dp.message(Command("addadmin"))
async def add_admin(m: Message):
    if m.from_user.id!=OWNER_ID: return
    p=(m.text or "").split()
    if len(p)!=2 or not p[1].isdigit(): return await m.answer("Использование: /addadmin TELEGRAM_ID")
    uid=int(p[1])
    db.table("admins").upsert({"telegram_id":uid,"role":"admin","added_by":OWNER_ID}).execute()
    log_action(m.from_user.id,"admin_added","admin",uid)
    await m.answer(f"✅ Администратор {uid} назначен.")

@dp.message(Command("deladmin"))
async def del_admin(m: Message):
    if m.from_user.id!=OWNER_ID: return
    p=(m.text or "").split()
    if len(p)!=2 or not p[1].isdigit(): return await m.answer("Использование: /deladmin TELEGRAM_ID")
    uid=int(p[1])
    if uid==OWNER_ID: return await m.answer("Владельца нельзя снять.")
    db.table("admins").delete().eq("telegram_id",uid).execute()
    log_action(m.from_user.id,"admin_removed","admin",uid)
    await m.answer(f"Администратор {uid} снят.")

@dp.callback_query(F.data=="admin:stats")
async def stats(c: CallbackQuery):
    if not is_admin(c.from_user.id): return await c.answer("Нет доступа",show_alert=True)
    req=db.table("reward_requests").select("id",count="exact").execute()
    ev=db.table("events").select("id",count="exact").eq("status","active").execute()
    players=db.table("players").select("telegram_id",count="exact").execute()
    await c.message.answer(f"📊 <b>Статистика</b>\nИгроков: {players.count or 0}\nЗаявок: {req.count or 0}\nАктивных ивентов: {ev.count or 0}")
    await c.answer()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot, db
    if BOT_TOKEN and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
        db=create_client(SUPABASE_URL,SUPABASE_SERVICE_ROLE_KEY)
        bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        asyncio.create_task(dp.start_polling(bot))
        log.info("Telegram bot polling started")
    else:
        log.warning("Missing BOT_TOKEN or Supabase server key; set Render environment variables")
    yield
    if bot:
        await bot.session.close()

app=FastAPI(title="Game Rewards Bot",lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok":True,"service":"game-rewards-bot","bot_configured":bool(BOT_TOKEN and SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)}
