# 비공개 최소 상태와 독립 예약 — 설계·격리 테스트 단계

2026-10-19 수정안. 이전 공개 `alert-state` 설계를 대체한다. main·기존 cron·운영 권한은
변경하지 않았고, 새 서비스·자격정보 설정·실제 수집/발송도 하지 않았다. Draft PR은 재시도하지
않는다. 이 문서와 테스트 통과는 운영 준비 완료나 예정 시각 실행 성공을 의미하지 않는다.

## 결정과 저장 범위

계획했던 Cloudflare Worker와 SQLite-backed Durable Object(DO) **한 개**를 재사용한다.
Python/Actions 수집·분류·Slack/Telegram 코드는 유지하고, DO를 비공개 상태 원본으로 삼는다.
별도 비공개 Git repo, SQL 서버, KV, R2, 상태용 장기 공유 토큰은 추가하지 않는다.

| 상태 | 필요한 최소 정보 | 제외/보존 |
|---|---|---|
| sent/skipped_legacy | SHA-256(GUID), 채널명, 상태 | 완료 기사 제목·URL·본문·원문 XML·시각 제외. 해시는 임의 삭제하지 않음 |
| pending/failed/in_flight/uncertain | 원 GUID, 제목·요약·링크·게시시각, 기존 분류, 채널·상태·오류 유형·시각 | 재시도·수신 확인용. raw XML 제외, 완료 후 다음 체크포인트에서 본문 제거 |
| 회차·heartbeat | 회차, run ID, 상태·시각·오류 유형 | 최근 60회차, heartbeat 최대 120개 |
| 동시 실행·갱신 | run/attempt lease, revision | 낡은 작성자와 상태 덮어쓰기 거부 |
| 예약·감시 | 회차별 요청/경고 intent·결과 | 기사 내용 없음 |
| 이관 출처 | exact cache key, 원 DB SHA-256 | DB 전체·자격정보는 저장하지 않음 |

토큰·웹훅·Telegram chat ID는 상태에 저장하지 않는다. 해시는 익명화가 아니므로 이것도
비공개로 유지한다. 언론사 공개 목록은 재생성 가능해 원격 상태에 넣지 않는다. 현재 운영
캐시를 내려받거나 내용을 출력하지 않았다. 실제 이력 건수·대기 큐 크기는 승인 후 이관 시 검토한다.

## 왜 Actions 로그·캐시·아티팩트가 아닌가

| 대안 | 판단 |
|---|---|
| 로그/run 성공 여부 | 채널별 intent의 원자적 저장·갱신·잠금 수단이 아님. run 없음도 성공 로그로 판단할 수 없음 |
| 캐시 | 미접근 7일 후 삭제 및 용량별 eviction. 기본 브랜치 캐시는 fork PR에서도 읽을 수 있음 |
| 아티팩트 | 기본 로그/아티팩트 보존 90일, 저장소 read 사용자가 다운로드 가능. 공개 repo의 비공개 저장소가 아님. v4 불변 파일도 전송별 체크포인트와 맞지 않음 |
| 별도 비공개 Git repo | 가능하지만 contents:write 장기 자격정보·별도 repo·Git 충돌 처리가 추가됨 |
| 암호화 아티팩트 | 암호화 키·교체·복구가 추가되고 expiry/원자적 잠금 문제는 남음 |
| 기존 계획의 DO 재사용 | 비공개성·직렬화·원자적 확정·독립 감시를 같은 작은 저장소에서 제공 |

근거: [캐시 접근·삭제](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching),
[아티팩트 접근·보존](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts),
[v4 불변성](https://docs.github.com/en/actions/tutorials/store-and-share-data),
[DO 원자적 저장](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/).
새 저장소가 필요한 이유는 크기보다 **비공개성·전송 전 원격 확정·복구·동시 실행 보호**다.

## 실행·인증·복구

1. 외부 Cron `7 1,9 * * *`는 scheduledTime에서 명시 KST 회차를 만들어 main을 dispatch한다.
   기존 cron도 유지하고 모든 수집은 같은 Actions concurrency group을 통과한다.
2. Actions는 audience가 승인된 `https://…workers.dev/state`인 짧은 수명 GitHub OIDC JWT를
   요청한다. 장기 상태 비밀번호는 없다. TLS POST만 허용하고 redirect·토큰 출력은 금지한다.
3. Worker는 GitHub JWKS/RS256 서명, issuer/audience/시간, repository ID `857560108`,
   정확한 repo/main/workflow_ref/subject/trigger를 검증한다. PR/fork/다른 workflow는 거부한다.
   공개 URL에 인증 없이 데이터가 노출되는 것은 아니다. GET/health 경로도 제공하지 않는다.
4. DO가 lease와 snapshot+revision을 반환하고 임시 로컬 SQLite에 복원한다. 상태가 없으면
   빈 이력으로 시작하지 않는다. API는 initialize/acquire/checkpoint/release 네 가지다.
5. 회차 시작 → 전송 intent → 전송 결과 → 회차 완료를 CAS로 원격 확정한다. 저장 성공 응답
   전에 발송하지 않는다. revision 불일치·한도·인증 오류는 중단한다. 완료 해시 삭제도 거부한다.
6. 정상 종료 시 lease를 반납한다. 중단 lease는 시간만으로 만료시키지 않는다. 새 run은
   GitHub로 **이전 run의 정확한 attempt가 completed**임을 확인한 뒤 인계한다. 확인 불가면
   멈춘다. OIDC의 run/attempt와 일치하지 않는 작성자는 체크포인트를 쓸 수 없다.
7. 외부 `27 1,9 * * *` Cron은 DO를 직접 확인한다. run이 전혀 없거나 실패/미완료이면
   동일 Slack 목적지에 회차당 한 번 경고한다. 감시용 Actions run을 다시 만드는 방식이 아니다.
8. 기존 cron은 최초 run created_at으로 회차를 추정하며 추정 지연 6시간 초과 시 중단한다.
   GitHub가 원 예정 날짜를 제공하지 않아 하루 이상 늦은 이벤트를 완전히 구분하지 못하는
   한계는 남는다. 명시 slot 외부 dispatch는 이 추정에 의존하지 않는다.

[GitHub 공식 OIDC claims/권한](https://docs.github.com/en/actions/reference/security/oidc)에 따르면
`id-token:write`는 JWT 요청 허용이며 저장소 쓰기 권한을 주지 않는다. 기존 Git checkpoint를
제거하고 snapshot 어댑터·범위 제한 OIDC 검증을 추가했다. 범용 CRUD·다중 DB·토큰 서버·별도
백그라운드 큐는 만들지 않았다. OIDC는 보안상 중요하므로 실제 연동 검증은 별도 필수다.

기존 sent/skipped_legacy와 허용된 failed 재시도를 복원하고, 새 채널 과거 기사 보충 발송은
하지 않는다. 응답 유실 시 정확히 한 번 전송과 무조건 자동 복구를 동시에 보장할 수 없어,
in_flight/uncertain은 보류한다. 수신 확인 후 승인된 복구 작업에서 sent/pending으로 정정한다.
응답 유실 뒤 낡은 revision 재시도는 거절하고 다음 실행이 원격 상태를 다시 읽는다.
확인 대기 payload는 RSS 창에서 사라져도 남는다. 불확실 heartbeat도 정상 완료로 위장하지 않는다.

SQLite DO는 지난 30일 [PITR](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/#pitr-point-in-time-recovery-api)를
제공한다. 단 과거 상태로 무턱대고 복원하면 이미 전송한 알림을 다시 보낼 수 있다. 자동 PITR·
초기화·deleteAll API는 만들지 않았다. 재해 복구는 dispatch 중지·수신 이력 대조·상태 병합을
승인받아 수행해야 한다. PITR는 로컬에서 지원되지 않아 이번에 실제 복원 검증하지 않았다.

스냅샷 앱 한도는 1MiB다(서비스 value 한도 2MB). 초과하면 실패시키고 중복 방지 해시는
지우지 않는다. 커지면 DO 내 행별 저장으로 전환하는 별도 변경을 검토한다. durable 전송 로그에
GUID는 제외했지만 기존 집계·실패 정보는 Actions 로그에 남는다. 상태/본문/JWT 아티팩트는 없다.
Worker 자체 중단·공유 한도 소진은 같은 감시도 멈춘다. 독립 제공자의 dead-man 감시는 범위 밖이다.

## 무료 범위·예상 사용량·초과 동작 (2026-10-19 공식 확인)

| 항목 | Free 한도 | 이 설계 |
|---|---|---|
| Workers | 하루 100,000 요청, 요청 CPU 10ms | 정상 `14 + 2D` 요청/일. 완료 회차를 기존 cron 2회가 확인하면 +4 |
| DO | 하루 100,000 요청, duration 13,000 GB-s | 요청 수는 대체로 동일. D=기사의 채널별 전송 시도 수 |
| SQLite DO | 읽기 500만·쓰기 10만 rows/일, 계정 전체 5GB | snapshot 앱 상한 1MiB + 작은 lease/감시 기록, 내부 메타데이터/PITR·공유 계정 사용량 별도 |
| Cron | Free 계정 최대 5개 | 2개 사용 |

예시 **하루 신규 10기사×2채널이면 D=20**, 정상 54요청 또는 기존 cron 확인까지 58요청/일,
30일 약 1,740요청이다. 실제 기사량 측정값이 아니다. 58요청을 각 1초·128MiB로 가정하면
약 7.25GB-s/일이며 이것도 측정값이 아니다. 논리 key 처리는 수백 회 수준이나, 값 크기와
내부 저장 배치 때문에 물리 rows를 요청 수와 같다고 단정하지 않는다. 실제 rows·duration·
**무료 CPU 10ms 충족 여부**를 승인 후 계정 지표로 확인한다. Worker에서 큰 JSON 재직렬화를
피하고 DO로 넘기지만, OIDC 검증·본문 처리 CPU는 로컬 테스트로 보증하지 않는다.

Free에서는 한도 초과 시 추가 연산이 **실패**하고 일일 한도는 UTC 00:00에 재설정된다.
자동 유료 전환을 설정하지 않는다. 원격 체크포인트가 막히면 전송 전에 멈추며 상태 삭제나
유료 서비스로 우회하지 않는다. 같은 계정의 감시도 실패할 수 있어 경고까지 보장할 수는 없다.
**이미 유료인 계정에는 배포하지 않고**, Free 여부·기존 공유 사용량을 먼저 확인한다.
코드는 플랜/결제 설정을 바꾸지 않으며 계정 요금제를 코드만으로 보증할 수도 없다.
CPU·용량이 부족하면 먼저 중지하고 설계를 조정하며 유료 전환 승인을 추정하지 않는다.

근거: [Workers Free 요청/CPU](https://developers.cloudflare.com/workers/platform/pricing/),
[DO Free·초과 실패·리셋·할당량](https://developers.cloudflare.com/durable-objects/platform/pricing/),
[DO 저장 한도](https://developers.cloudflare.com/durable-objects/platform/limits/),
[Cron 수 한도](https://developers.cloudflare.com/workers/platform/limits/).

## 한 번에 승인받을 최소 항목 — 아직 적용하지 않음

1. **Cloudflare Free Worker + SQLite DO 한 개**, 인증된 workers.dev 상태 URL과 위 최소 상태
   비공개 저장. 유료 플랜·KV/R2·별도 DB는 사용하지 않는다. 사용자 대시보드 로그인으로
   Worker/cron/DO binding 배포·secret 입력을 진행하며 별도 장기 Cloudflare API token은
   만들지 않는다. 자동배포 API 인증을 추가한다면 실제 최소 권한을 다시 확인·승인받는다.
2. **fine-grained PAT**: `coolpint/obituary-personnel-webapp` 하나, **Actions:write**와 자동
   Metadata:read만. Contents/Secrets/Admin/Issues 권한 없음. dispatch와 이전 attempt 상태
   조회에 쓴다. GitHub가 workflow 한 개로 제한하지 않으므로 이 PAT가 대상 repo의 다른
   Actions 관리도 가능하다는 범위를 알린다. 만료일·교체 담당자를 정한다.
   [dispatch 최소 권한](https://docs.github.com/rest/actions/workflows#create-a-workflow-dispatch-event).
3. **동일 Slack webhook 외부 사용**: 사용자가 Worker secret에 직접 보안 입력한다.
   GitHub Secret을 추출하지 않는다. Telegram 자격정보는 외부로 이동하지 않는다.
4. **Actions 권한/변수**: 수집 `contents:read`, `actions:read`, `id-token:write`;
   이관 `contents:read`, `id-token:write`. STATE_ENDPOINT는 일반 변수다. 기존
   contents:write 제안은 철회한다. OIDC는 정확한 main의 수집/이관 workflow만 신뢰한다.
5. **후속 main 반영·최신 cache 이관·활성화·예정 회차 관찰**은 별도 승인받는다.
   지금 단계 승인이 아니다. 수동 실수집·시험 발송·과거 기사 재전송·기존 cron 중단도 보류다.
   Draft PR은 재시도하지 않는다.

## 승인 후 전환 초안과 현재 검증

처음에는 ENABLED/STATE_API_ENABLED/ALLOW_INITIALIZE 모두 false로 준비한다. 계정·권한과
OIDC/무료 CPU 연동을 실제 발송 없이 검증할 단계도 승인받는다. 구 코드 run의 실행·대기가
없도록 전환 창을 정하고 바로 직전 최신 cache/전송 이력을 검토한다. main 반영 승인 후
STATE_API_ENABLED와 ALLOW_INITIALIZE만 열어 exact cache·무결성을 확인한 최소 이력을 한 번
이관한다. 이미 상태가 있으면 initialize는 거부한다. 출처/건수 확인 후 ALLOW_INITIALIZE를
닫고 예약 ENABLED를 켠다. 다음 예정 오전·저녁의 run 생성·상태 완료·신규 전달과 +20분 감시를
관찰한다. 기사 없는 회차면 전달 검증은 미완료로 적는다. 롤백도 private state를 보존한다.

테스트는 임시 SQLite·모의 HTTP/DO storage·로컬 RSA 키만 사용한다. 서명/claims 거부,
lease/CAS/한도 실패, 본문 최소화, compact 복원 후 중복 방지, exact-cache 이관,
pending 복구·uncertain 보류, 지연 회차·heartbeat·미실행 감시를 검사한다. 실제 OIDC 발급,
Cloudflare Free CPU/DO/PITR, 운영 cache, 실제 발송·예정 시각 실행은 검증하지 않았다.

```sh
python -m unittest discover -v
node --test scheduler/*.test.mjs
```
