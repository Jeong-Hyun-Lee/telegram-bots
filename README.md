# telegram-bots

성격을 가진 텔레그램 대화 봇 모음. 엔진(`bot.py`) 하나로 여러 봇을 각각 실행한다.
AI는 Gemini API 무료 등급을 사용한다.

## 기능

- `persona.md`로 성격과 말투 정의
- 대화 기억: 최근 40개 메시지 + 10번 주고받을 때마다 장기 기억 요약
- 시간 감각: 보낸 시각과 지난 시간을 고려해서 대화
- 먼저 연락: 오래 대화가 없으면 정해진 시간대에 한 번 말을 검

## 구조

```
bot.py                      공통 엔진
bots/
  nara/
    persona.md              성격, 말투, 예시 대화
    config.toml             이름, 에러 문구, 먼저 연락 설정
    .env.example            키 형식 (실제 .env는 git 제외)
    .env                    TELEGRAM_TOKEN, GEMINI_API_KEY  ← 직접 생성
    memory.db               대화 기록 (자동 생성, git 제외)
deploy/
  setup.sh                  Ubuntu 서버 설치 + 서비스 등록
  telegram-bot@.service     systemd 템플릿 (봇마다 인스턴스 1개)
run.bat                     Windows 로컬 실행
```

## 새 봇 추가

1. 텔레그램 `@BotFather`에서 `/newbot`으로 봇을 만들고 토큰을 받는다.
2. `bots/nara`를 복사해서 `bots/<새이름>`을 만든다. `memory.db`는 복사하지 않는다.
3. `persona.md`, `config.toml`을 고친다.
4. `.env.example`을 `.env`로 복사하고 새 토큰과 Gemini 키를 넣는다.

## 로컬 실행 (Windows)

```
pip install -r requirements.txt
run.bat nara
```

같은 봇을 두 곳(PC와 서버)에서 동시에 실행하면 텔레그램 `Conflict` 에러가 난다. 한 곳에서만 실행한다.

## 서버 배포 (Ubuntu, 예: Oracle Cloud Always Free)

```
git clone <repo> ~/telegram-bots
# bots/<이름>/.env 를 서버에 직접 만들거나 scp로 올린다 (git에 없음)
bash ~/telegram-bots/deploy/setup.sh nara
```

운영 명령:

```
journalctl -u telegram-bot@nara -f          # 로그
sudo systemctl restart telegram-bot@nara     # 코드 수정 후 재시작
```

## 설정값

`bot.py` 상단: 사용 모델(`MODELS`), 기억 범위(`MAX_TURNS`), 요약 주기(`SUMMARY_EVERY`)
`bots/<이름>/config.toml`: `idle_hours`(먼저 연락까지 대기 시간), `active_hours`(연락 시간대)
