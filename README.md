# YNA People Alert MVP

연합뉴스 `사람들` RSS(`https://www.yna.co.kr/rss/people.xml`)를 주기적으로 읽고:

1. `인사` / `부고` 기사로 분류
2. 언론인 관련(직군 키워드 + 언론사 사전 매칭)만 필터링
3. 콘솔/슬랙/텔레그램으로 알림 전송

네이버(`https://news.naver.com/main/officeList.naver`) + 다음(`https://news.daum.net/cplist`) 언론사 목록을 참고해 사전을 주기 갱신하며, 실패 시 기본 시드 사전으로 동작합니다.

## Quick Start

```bash
cd /Users/air/codes/orbituary
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env`를 수정한 뒤 1회 실행:

```bash
set -a; source .env; set +a
python3 -m yna_people_alert.main --once
```

지속 실행:

```bash
set -a; source .env; set +a
python3 -m yna_people_alert.main
```

## Environment Variables

- `RSS_URL`: 기본값 `https://www.yna.co.kr/rss/people.xml`
- `OFFICE_LIST_URL`: 기본값 `https://news.naver.com/main/officeList.naver`
- `DAUM_CPLIST_URL`: 기본값 `https://news.daum.net/cplist`
- `POLL_SECONDS`: RSS 폴링 주기(초), 기본 `43200`(12시간)
- `MEDIA_SCORE_THRESHOLD`: 언론인 관련 임계값, 기본 `5`
- `REQUEST_TIMEOUT_SECONDS`: HTTP timeout, 기본 `15`
- `OUTLET_REFRESH_HOURS`: 언론사 사전 갱신 주기(시간), 기본 `12`
- `DB_PATH`: SQLite 경로, 기본 `./data/yna_people_alert.db`
- `OUTLET_CACHE_PATH`: 언론사 캐시 JSON, 기본 `./data/outlets_cache.json`
- `SLACK_WEBHOOK_URL`: 설정 시 슬랙 알림 사용(알림 우선순위 1순위)
- `SLACK_MENTION`: 선택. 예) `@channel`, `<!subteam^S12345|editors>`
- `INCLUDE_SUMMARY_IN_ALERT`: 알림에 RSS 설명문 포함 여부, 기본 `true`
- `ALERT_SUMMARY_MAX_CHARS`: 설명문 최대 길이, 기본 `500`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`: 슬랙 미설정 시 텔레그램 알림 사용

## Project Structure

```text
yna_people_alert/
  settings.py           # env 설정
  rss_fetcher.py        # RSS 수집/파싱
  outlet_dictionary.py  # 언론사 사전(네이버 + 다음 + 시드 + 캐시)
  classifier.py         # 인사/부고 + 언론인 점수화
  notifier.py           # 콘솔/슬랙/텔레그램 전송
  store.py              # SQLite 저장소
  main.py               # 루프 오케스트레이션
```

## Slack 설정

1. 슬랙 워크스페이스에서 Incoming Webhook 앱을 추가합니다.
2. 웹훅 URL을 발급받아 `.env`의 `SLACK_WEBHOOK_URL`에 넣습니다.
3. `python3 -m yna_people_alert.main --once`로 테스트합니다.

## GitHub Actions 스케줄 실행

워크플로 파일은 [yna-people-alert.yml](/Users/air/codes/orbituary/.github/workflows/yna-people-alert.yml)에 포함되어 있습니다.

- 실행 시각(한국시간 KST): 매일 오전 10시, 오후 6시
- cron 기준(UTC): `01:00`, `09:00`
- 각 실행은 `--once`로 1회 처리 후 종료

리포지토리에 아래 값을 설정하세요.

- GitHub Secrets: `SLACK_WEBHOOK_URL` (필수: 슬랙 알림)
- GitHub Variables: `MEDIA_SCORE_THRESHOLD` (선택), `REQUEST_TIMEOUT_SECONDS` (선택), `SLACK_MENTION` (선택), `INCLUDE_SUMMARY_IN_ALERT` (선택), `ALERT_SUMMARY_MAX_CHARS` (선택)

## Scoring (MVP)

- 직군 키워드 매칭: 개수당 +2 (최대 +4)
- 언론사 매칭: 개수당 +3 (최대 +9)
- 직군+언론사 동시 존재: +2 보너스
- 총점 `>= MEDIA_SCORE_THRESHOLD` 이면 알림 대상

## Next Improvements

- 오탐/미탐 로깅 기반 키워드/사전 자동 보강
- 기사 본문 크롤링(선택)으로 정확도 향상
- 규칙 애매 케이스에만 LLM 보조 분류 적용
