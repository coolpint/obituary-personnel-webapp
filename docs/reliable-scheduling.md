# 독립 예약 실행과 회차 보호 — 배포 전 검토안

이 변경은 준비·격리 테스트 단계다. 운영 배포, 새 서비스/자격정보 설정,
상태 이관, 실제 수집·시험 발송, 기존 cron 비활성화는 수행하지 않았다.

## 조사와 선택

- 기준 main: `d58b1c8fc570bf9956588a27b1ae116b2ab64c8a`.
- 저장소에 AGENTS.md 등 별도 작업 지침은 없다.
- 기존 GitHub cron `01:07/09:07 UTC`는 그대로 남긴다. Actions 생성 지연에
  대비해 독립 실행기가 `workflow_dispatch`를 호출한다. GitHub runner/API 자체가
  장애이면 정시 실행을 보장할 수 없으며 외부 감시가 그 상황을 경고한다.
- `render.yaml`은 Python 헬스 HTTP 서비스만 실행한다. 실제 Render 계정의 요금제와
  서비스 상태는 확인할 연결 도구가 없어 미확인이다. 무료 web은 유휴 15분 후 중지되고
  로컬 파일이 소실되므로 독립 상시 예약기로 쓸 수 없다. Render Cron은 서비스당
  최소 월 $1이며 SQLite 영구 디스크도 지원하지 않는다.
- 연결된 Netlify/Vercel에서 `obituary` 이름의 프로젝트는 없었다. 다른 이름의
  기존 프로젝트나 계정 전체의 무료 잔여량은 확인하지 않았다.
- 로컬 `gh` 인증은 만료됐다. 공개 Git clone과 연결된 GitHub 읽기 API는 정상이다.
  기존 인증을 추출·복사하지 않으며, 로컬 상시 실행이나 Codex 로컬 자동화는 쓰지 않는다.
- 권고안: **Cloudflare Workers Free + SQLite Durable Object 1개**. 새 서비스이므로
  승인 전 생성·배포하지 않는다. 하루 예약 4회(수집 2회, 감시 2회)는 현재 무료 한도보다
  작다. 무료 계정이어야 하며 유료 전환을 자동 승인하지 않는다. 기존 계정의 공유
  무료 한도를 넘으면 실패할 수 있으므로 계정 확인과 사용량 점검이 필요하다.
- 대안: 사용자가 이미 운영하는 항상 켜진 서버에서 동일한 dispatch/watch 로직을 실행하거나,
  Render Cron(유료)을 사용한다. 새 무료 외부 cron 서비스도 가능하지만 정확한 회차 값을
  생성하고 독립 감시 및 알림 중복 방지 상태를 보존할 실행 환경이 추가로 필요하다.

공식 근거(2026-10-19 확인):
[Cloudflare Cron](https://developers.cloudflare.com/workers/configuration/cron-triggers/),
[Workers 한도](https://developers.cloudflare.com/workers/platform/limits/),
[Durable Objects 무료 SQLite/한도](https://developers.cloudflare.com/durable-objects/platform/pricing/),
[Render Free](https://render.com/docs/free),
[Render Cron 비용](https://render.com/docs/cronjobs),
[GitHub dispatch 권한](https://docs.github.com/rest/actions/workflows#create-a-workflow-dispatch-event).

## 실행과 상태

1. 외부 Cron `7 1,9 * * *`가 KST `YYYY-MM-DDT10:07+09:00` 또는
   `YYYY-MM-DDT18:07+09:00`을 전달한다. 실제 처리 시간이 아닌 이벤트의 scheduledTime을 쓴다.
2. 모든 수집은 동일한 Actions concurrency group을 통과한다. 로컬 DB 접근에는 flock도 걸린다.
   GitHub는 pending run을 하나만 보존하므로 폭주하는 dispatch는 피한다.
3. `alert-state` 전용 branch에 기존 SQLite 기사·채널 이력과 회차 기록을 보존한다.
   캐시 누락 시 새 DB를 만드는 fallback은 없다. branch/DB가 없거나 손상되면 전송 전에 멈춘다.
4. 회차 시작, 전송 직전 intent, 전송 결과, 회차 완료를 순서대로 Git commit/push한다.
   non-fast-forward나 인증 오류는 강제 덮어쓰지 않고 실패시킨다. 코드 main은 수정하지 않는다.
5. 완료된 회차는 수집·기사 발송·저녁 heartbeat를 다시 하지 않는다. 실패/중단된 회차는
   재시도할 수 있지만 저장된 성공 전송과 불확실 전송은 다시 보내지 않는다.
6. 기존 schedule은 최초 run의 created_at과 cron으로 회차를 구한다. 재실행 시각으로
   구하지 않는다. GitHub는 원래 예정 날짜를 제공하지 않으므로 추정 지연이 6시간을 넘으면
   중단한다. 하루 이상 늦은 이벤트가 같은 시간대에 도착하는 경우 원래 날짜를 구별할 수
   없다는 한계가 있다. 외부 dispatch는 명시 회차이므로 이 한계가 없다.
7. 외부 Cron `27 1,9 * * *`는 해당 회차의 health.json을 **GitHub API로 직접** 읽는다.
   기록 없음, running/failed, 전송 확인 필요, API/인증 오류를 기존 Slack 목적지로 경고한다.
   수집 워크플로를 재실행해서 감시하는 방식이 아니므로 run이 전혀 없어도 감지한다.
8. Durable Object가 중복 dispatch와 감시 경고를 직렬화한다. 경고는 회차당 한 번만 시도하고,
   발송 결과가 불확실하면 상태를 남기고 자동 재발송하지 않는다. 경고 이후 복구 알림은 없다.

기사의 GUID/채널별 이력, 기존 분류, 언론사 사전, Slack·Telegram 수신처는 그대로 사용한다.
과거 `skipped_legacy` 이력을 유지하며, 기존 `failed` 항목의 허용된 재시도도 유지한다.
운영 진입점은 반드시 `python -m yna_people_alert.scheduled`여야 한다. 기존 `main --once`
진입점에는 원격 상태 보장과 전역 잠금이 없으므로 운영 병행 실행을 금지한다.

## 외부 전송의 불확실성

Slack Incoming Webhook과 Telegram sendMessage는 이 프로그램이 사용할 수 있는
공통 멱등 키가 없다. 전송 성공 후 응답/저장이 유실되면 정확히 한 번 전송과 자동 복구를
동시에 보장할 수 없다. 이 변경은 중복 방지를 위해 다음을 선택한다.

- 원격 intent 저장 실패: 아직 보내지 않았으므로 중단한다.
- intent 후 중단 또는 응답 불확실: `in_flight`/`uncertain`을 보존하고 자동 재전송하지 않는다.
- 상태는 전송 문제가 해결될 때까지 failed이며 정상 heartbeat를 내지 않는다.
- 운영자가 수신처에서 실제 도착 여부를 확인한 뒤 상태를 sent 또는 pending으로
  정정하는 별도 승인이 필요하다. 확인 없이 pending으로 되돌리지 않는다.
- 따라서 일부 알림은 중복 대신 확인 대기로 남을 수 있다. 기존 실패 항목은 다음 실행에서
  재시도하지만, 새 durable 실행 중 발생한 불확실 오류는 이 보호 규칙을 따른다.
- 실패 알림용 기존 Actions 단계는 별도로 유지된다. 서로 다른 실패 run에 대한 경고는
  반복될 수 있다. 기사 알림과 저녁 heartbeat, 외부 watchdog 경고의 회차 보호와는 별개다.
- 외부 예약기 자체가 완전히 멈추면 같은 예약기의 watchdog도 멈춘다. Cloudflare 실행 이력
  점검 또는 별도 제공자의 dead-man 감시를 추가해야 그 장애까지 독립적으로 감지할 수 있다.

## 승인 요청을 한 번에 제시할 항목

1. **코드 main 병합/배포**와 짧은 전환 창. 기존 예약/수동 run이 실행 중·대기 중이지 않은지
   확인하고 전환한다. 코드가 바뀌기 전 run이 나중에 구 DB로 발송하지 않도록 모두 종료를 확인한다.
2. **임시 Actions 토큰 권한**: 수집 job `contents:write`, `actions:read`;
   이관 job `contents:write`. contents 권한은 GitHub 특성상 branch 한 개로 제한되지 않는다.
   코드에서는 `alert-state`만 push하지만 main 쓰기도 가능한 토큰이라는 점을 승인해야 한다.
3. **상태 공개 범위**: 현재 저장소는 공개다. `alert-state`도 공개되며 RSS 기사 원문/분류,
   채널명과 전송 상태·시각, 회차 기록이 Git 이력으로 남는다. 토큰·웹훅·chat ID는 저장하지 않는다.
   이런 메타데이터 공개가 불가하면 비공개 상태 저장소/외부 DB로 설계를 바꿔야 한다.
4. **Cloudflare Free Worker와 SQLite Durable Object 신규 생성**. 권한은 대상 계정의
   Workers Scripts 편집과 예약·binding·migration 배포 권한이며, 계정 OAuth/로그인 흐름을 사용한다.
   도메인·DNS·유료 플랜 권한은 필요 없다. 계정 전체 토큰은 만들지 않는다.
5. **새 GitHub fine-grained PAT**: `coolpint/obituary-personnel-webapp` 한 저장소만,
   `Actions:write`(dispatch 및 읽기), `Contents:read`(health), 자동 Metadata read.
   Secrets/Admin/Contents write/Issues 권한은 불필요하다. 적절한 만료일과 교체 담당자를 정한다.
   PAT를 사용자 보안 입력으로 Worker secret `GITHUB_DISPATCH_TOKEN`에 설정한다.
   GitHub App installation token 방식도 가능하지만 앱 설치·개인키 보관이 추가된다.
6. **동일 Slack 목적지 외부 사용**: 사용자가 기존 webhook을 직접 보안 입력으로
   Worker secret `SLACK_WEBHOOK_URL`에 설정한다. GitHub Secret에서 값을 읽어오지 않는다.
   Telegram 자격정보는 외부 예약기로 이동하지 않는다.
7. **실제 검증 범위**: 수동 실제 수집/Slack·Telegram 시험 발송은 별도 승인 전 금지다.
   권고는 시험 기사 재발송 대신 다음 예정 회차의 실제 실행/신규 기사 전달만 관찰하는 것이다.
8. **기존 cron**: 이 변경에서는 유지한다. 외부 예정 회차 성공 확인 후 비활성화 여부를
   결정한다. 비활성화는 별도 승인 후 수행한다.

## 승인 후 실행 순서

1. 미실행/지연 진단과 마지막 완료 run 및 **최신 cache key**를 다시 확인한다.
   `yna-people-alert-data-39130538585`는 조사 당시 성공 run의 후보일 뿐 최신이라고 단정하지 않는다.
   모든 기존 run을 확인하고 이관 시각 직전의 정상 DB를 선택한다. 과거 DB를 쓰면 최근 기사
   재전송 위험이 있다. 상태 cache가 없으면 빈 DB로 진행하지 않고 멈춘다.
2. 코드 병합 후 수집을 시작하기 전에 수동 `YNA state migration` workflow를 정확한 검토된
   cache key로 한 번 실행한다. 최신 이력이 실제 포함됐는지 상태 수·sent 이력·해시를 확인한다.
   이관은 수집/알림을 호출하지 않고, 기존 branch가 있으면 덮어쓰지 않는다.
3. Cloudflare를 Free로 확인하고 Worker/DO를 준비한다. 기본 `ENABLED=false`로 배포한다.
   보안 입력으로 두 secret을 설정한다. GitHub Secret 추출이나 CLI 토큰 출력은 금지다.
4. 설정, cron 시간, 대상 저장소/워크플로, DO binding, 권한을 확인한다. 승인받은 전환 창에
   `ENABLED=true`로 재배포한다. Cron 변경 전파는 최대 15분 걸릴 수 있으므로 예정 회차 전에 끝낸다.
5. 다음 KST 10:07 또는 18:07 회차에서 외부 scheduled 이벤트, GitHub run 생성/시작,
   DB running→succeeded, 기존 수신처 신규 기사 수신, 저녁 heartbeat를 확인한다.
   기사 없는 회차라면 신규 기사 발송 검증은 별도로 미완료라고 기록한다.
6. +20분 watchdog가 정상 회차에 침묵하는지 확인한다. 미실행 경고의 실제 외부 발송은
   모의 테스트까지만 확인했으므로, 운영 경고 시험을 원하면 별도 영향 승인을 받는다.
7. 같은 회차의 늦은 schedule은 상태 조회 후 skip해야 한다. 확인을 위해 실제 수집을
   강제 재실행하지 않고 자연 도착한 기존 schedule 로그 또는 격리 테스트 증거를 사용한다.
8. 첫 예정 회차 성공은 배포 완료와 별개로 기록한다. 최소 오전·저녁 두 회차 및 다음 날
   회차까지 관찰한 뒤 기존 cron 유지/중단을 결정한다.

## 롤백

Worker `ENABLED=false`로 외부 dispatch부터 중지한다. 진행 중 수집을 무턱대고 취소하지
않고 최신 `alert-state`를 보존한다. 코드만 구버전/과거 cache로 되돌리면 이미 전송된 기사를
다시 보낼 수 있다. 따라서 복구는 durable runner와 최신 상태를 유지한 채 수정하는 것이 우선이다.
구 실행기로 되돌리는 경우에도 최신 durable DB를 가져오는 절차를 승인받고 검증해야 한다.
상태 branch를 삭제하거나 force push하지 않는다.

## 검증 범위

Python 3.11 unittest와 Node 내장 test runner로 검증한다. 테스트는 HTTP를 모의 처리하고
임시 SQLite/로컬 bare Git만 사용한다. 실제 기사 수집·메신저 시험 발송은 없다.
Cloudflare 실제 계정/DO 런타임 배포, GitHub 운영 권한/branch 규칙, 최신 cache 이관,
예정 시각 실실행은 승인 후 확인해야 하며 로컬 테스트 통과로 대체하지 않는다.

```sh
python -m unittest discover -v
node --test scheduler/worker.test.mjs
```
