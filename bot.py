import asyncio
import os
import sqlite3
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

BOTS_DIR = Path(__file__).parent / "bots"
# 앞 모델이 실패(503 혼잡 등)하면 다음 모델로 재시도
MODELS = ("gemini-flash-lite-latest", "gemini-flash-latest")
# 모델에 보내는 최근 메시지 수 (짝수 유지: user/model 한 쌍씩 저장)
MAX_TURNS = 40
# 메시지가 이 개수만큼 쌓일 때마다 장기 기억 요약 갱신 (MAX_TURNS 이하여야 빠지는 대화 없음)
SUMMARY_EVERY = 20
CHECK_SECONDS = 600
WEEKDAYS = "월화수목금토일"

SUMMARY_PROMPT = """너는 대화 기록을 정리하는 비서.
[기존 메모]와 [최근 대화]를 합쳐 '사용자'에 대한 메모를 새로 써라.
- 이름, 나이, 직업, 가족/반려동물, 취향, 일정, 고민, 봇과 한 약속 같은 오래 기억할 사실만.
- 사소한 인사나 잡담은 빼라. 바뀐 정보는 최신 것으로 고쳐라.
- '내일', '다음 주 토요일' 같은 상대 날짜는 메시지 앞 [시각] 기준으로 실제 날짜로 바꿔 적어라.
- 한국어 불릿(-) 목록, 최대 15줄. 메모 외의 말은 출력하지 마라."""

TIME_GUIDE = """[시간 감각]
- 지금: {now}. 마지막 대화 후 {gap} 지남.
- 사용자 메시지 앞 [ ]는 보낸 시각. 시간 흐름에 맞게 자연스럽게 반응해.
  (예: 저녁에 퇴근했다고 한 뒤 다음 날 아침 메시지면 '출근했어?'처럼 새 하루로 대화)
- 몇 시간 이상 지났으면 이전 대화를 방금 일처럼 이어가지 말고 지금 상황부터 시작해.
- 답장에 [시각] 표기는 쓰지 마."""


def load_bot(name: str) -> tuple[Path, dict, str]:
    bot_dir = BOTS_DIR / name
    if not (bot_dir / "persona.md").exists():
        available = ", ".join(sorted(path.parent.name for path in BOTS_DIR.glob("*/persona.md")))
        sys.exit(f"봇 '{name}' 없음. 사용 가능: {available}")
    config = tomllib.loads((bot_dir / "config.toml").read_text(encoding="utf-8"))
    persona = (bot_dir / "persona.md").read_text(encoding="utf-8").strip()
    return bot_dir, config, persona


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        sys.exit(f".env 파일이 없음: {env_path} (.env.example 복사 후 값 입력)")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))
    for key in ("TELEGRAM_TOKEN", "GEMINI_API_KEY"):
        if not os.environ.get(key) or os.environ[key].startswith("여기에"):
            sys.exit(f"{env_path}에 {key} 값을 입력하세요.")


if len(sys.argv) != 2:
    sys.exit("사용법: python bot.py <봇이름>  (예: python bot.py nara)")
BOT_DIR, CONFIG, PERSONA = load_bot(sys.argv[1])
BOT_NAME = CONFIG["name"]
ERROR_MESSAGE = CONFIG["error_message"]
# 먼저 연락: 마지막 대화 후 idle_hours 지나면 1번 말 걸기 (답장 올 때까지 재연락 안 함)
IDLE_HOURS = CONFIG.get("idle_hours", 6)
ACTIVE_START, ACTIVE_END = CONFIG.get("active_hours", [10, 22])
ACTIVE_HOURS = range(ACTIVE_START, ACTIVE_END + 1)
PING_NOTE = f"(한동안 연락이 없어서 {BOT_NAME} 쪽에서 먼저 연락함)"

load_env(BOT_DIR / ".env")
client = genai.Client()  # GEMINI_API_KEY 환경변수 자동 사용
summary_config = types.GenerateContentConfig(system_instruction=SUMMARY_PROMPT)
db = sqlite3.connect(BOT_DIR / "memory.db")
db.execute(
    "CREATE TABLE IF NOT EXISTS messages "
    "(id INTEGER PRIMARY KEY, chat_id INTEGER, role TEXT, text TEXT, created_at REAL)"
)
db.execute("CREATE TABLE IF NOT EXISTS memories (chat_id INTEGER PRIMARY KEY, summary TEXT)")
db.execute(
    "CREATE TABLE IF NOT EXISTS chats "
    "(chat_id INTEGER PRIMARY KEY, last_user_at REAL, last_ping_at REAL DEFAULT 0)"
)
# 구버전 DB 호환: 시각 컬럼이 없으면 추가 (기존 기록은 NULL로 시각 없이 사용)
if "created_at" not in {row[1] for row in db.execute("PRAGMA table_info(messages)")}:
    db.execute("ALTER TABLE messages ADD COLUMN created_at REAL")


def format_time(timestamp: float) -> str:
    moment = datetime.fromtimestamp(timestamp)
    period = "오전" if moment.hour < 12 else "오후"
    hour = moment.hour % 12 or 12
    return (
        f"{moment.month}월 {moment.day}일({WEEKDAYS[moment.weekday()]}) "
        f"{period} {hour}:{moment.minute:02d}"
    )


def describe_gap(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}분"
    if minutes < 24 * 60:
        return f"{minutes // 60}시간"
    return f"{minutes // (24 * 60)}일"


def stamp(text: str, timestamp: float | None) -> str:
    return f"[{format_time(timestamp)}] {text}" if timestamp else text


def touch_user(chat_id: int) -> None:
    with db:
        db.execute(
            "INSERT INTO chats (chat_id, last_user_at) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_user_at = excluded.last_user_at",
            (chat_id, time.time()),
        )


def load_history(chat_id: int, limit: int = MAX_TURNS) -> list:
    rows = db.execute(
        "SELECT role, text, created_at FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    # 시각은 사용자 메시지에만 붙임 (봇 답장에 붙이면 답장에도 시각을 따라 씀)
    return [
        {"role": role, "parts": [{"text": stamp(text, created_at) if role == "user" else text}]}
        for role, text, created_at in reversed(rows)
    ]


def save_turn(chat_id: int, user_text: str, model_text: str) -> None:
    now = time.time()
    with db:
        db.executemany(
            "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            [(chat_id, "user", user_text, now), (chat_id, "model", model_text, now)],
        )


def load_summary(chat_id: int) -> str:
    row = db.execute("SELECT summary FROM memories WHERE chat_id = ?", (chat_id,)).fetchone()
    return row[0] if row else ""


def persona_config(chat_id: int) -> types.GenerateContentConfig:
    now = time.time()
    last_at = db.execute(
        "SELECT MAX(created_at) FROM messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0]
    gap = describe_gap(now - last_at) if last_at else "알 수 없음(첫 대화이거나 기록 없음)"
    parts = [PERSONA, TIME_GUIDE.format(now=format_time(now), gap=gap)]
    summary = load_summary(chat_id)
    if summary:
        parts.append(f"[사용자에 대해 기억하는 것]\n{summary}")
    return types.GenerateContentConfig(system_instruction="\n\n".join(parts))


async def generate(contents: list, config: types.GenerateContentConfig) -> str | None:
    for model in MODELS:
        try:
            res = await client.aio.models.generate_content(
                model=model, contents=contents, config=config
            )
            if res.text:
                return res.text
        except Exception as error:
            print(f"Gemini error ({model}): {error}")
    return None


async def update_summary(chat_id: int) -> None:
    count = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    if count % SUMMARY_EVERY:
        return
    dialog = "\n".join(
        f"{'사용자' if msg['role'] == 'user' else BOT_NAME}: {msg['parts'][0]['text']}"
        for msg in load_history(chat_id, SUMMARY_EVERY)
    )
    prompt = f"[기존 메모]\n{load_summary(chat_id) or '(없음)'}\n\n[최근 대화]\n{dialog}"
    summary = await generate([{"role": "user", "parts": [{"text": prompt}]}], summary_config)
    if summary is None:
        return
    with db:
        db.execute(
            "INSERT INTO memories (chat_id, summary) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET summary = excluded.summary",
            (chat_id, summary.strip()),
        )


async def reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    touch_user(chat_id)
    await ctx.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    current = {"role": "user", "parts": [{"text": stamp(user_text, time.time())}]}
    text = await generate(load_history(chat_id) + [current], persona_config(chat_id))
    if text is None:
        await update.message.reply_text(ERROR_MESSAGE)
        return
    save_turn(chat_id, user_text, text)
    await update.message.reply_text(text)
    await update_summary(chat_id)  # 답장 먼저 보낸 뒤 갱신해서 응답 지연 없음


async def send_pings(bot) -> None:
    if datetime.now().hour not in ACTIVE_HOURS:
        return
    now = time.time()
    rows = db.execute(
        "SELECT chat_id, last_user_at FROM chats "
        "WHERE last_ping_at < last_user_at AND last_user_at < ?",
        (now - IDLE_HOURS * 3600,),
    ).fetchall()
    for chat_id, last_user_at in rows:
        hours = int((now - last_user_at) // 3600)
        prompt = (
            f"(상황: 사용자와 {hours}시간째 대화가 없음. "
            "네가 먼저 카톡 보내듯 자연스럽게 1~2문장으로 말을 걸어. "
            "기억하는 내용이 있으면 활용해.)"
        )
        contents = load_history(chat_id) + [{"role": "user", "parts": [{"text": prompt}]}]
        text = await generate(contents, persona_config(chat_id))
        if text is None:
            continue
        # 전송 실패(차단 등)여도 재시도 반복 안 하게 먼저 기록
        with db:
            db.execute("UPDATE chats SET last_ping_at = ? WHERE chat_id = ?", (now, chat_id))
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as error:
            print(f"Telegram error ({chat_id}): {error}")
            continue
        save_turn(chat_id, PING_NOTE, text)
        await update_summary(chat_id)


async def ping_loop(bot) -> None:
    while True:
        await asyncio.sleep(CHECK_SECONDS)
        try:
            await send_pings(bot)
        except Exception as error:
            print(f"Ping error: {error}")


async def start_pinger(application) -> None:
    application.bot_data["pinger"] = asyncio.create_task(ping_loop(application.bot))


async def stop_pinger(application) -> None:
    application.bot_data["pinger"].cancel()


app = (
    ApplicationBuilder()
    .token(os.environ["TELEGRAM_TOKEN"])
    .post_init(start_pinger)
    .post_shutdown(stop_pinger)
    .build()
)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply))
print(f"{BOT_NAME} 봇 실행 중... (종료: Ctrl+C)")
app.run_polling()
