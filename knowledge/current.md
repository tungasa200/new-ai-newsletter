# 현재 권장 사항 메모

## Anthropic (Claude)
- 최신 모델: Claude Fable 5.2 (`claude-fable-5-2`). 1M 컨텍스트, 최대 출력 128k, 항상 켜진 적응형 사고, 입력 $8 / 출력 $40 per MTok. Claude Code에서 캐시 읽기는 $0.20/MTok이다.
- Claude Mythos 5.2 (`claude-mythos-5-2`)는 성능이 같지만 Anthropic 검증 프로그램(예: Cyber Verification Program)을 통과한 조직만 쓸 수 있다.
- Fable 5.2 fast mode: Messages API 요청에 `speed: "fast"`를 설정한다(베타 헤더 불필요). 출력 속도는 최대 2.5배, 가격은 입력 $16 / 출력 $80 per MTok이다.
- Claude Platform (베타): Console의 조직 IP 허용 목록(IPv4/IPv6, CIDR, 조직당 최대 1,000개). Admin API로 Enterprise 멤버를 모든 기기에서 로그아웃시킬 수 있다.
- Linked billing (베타, 베타 헤더 `user-profiles-billing-2026-10-01`): 고객이 자신의 Claude 조직을 연결하면, 각 `/v1/messages` 또는 `/v1/chat/completions` 요청의 `billing_policy`로 비용 부담 주체를 정한다. 고객은 Console에서 앱별 지출 상한을 설정하거나 연결을 해제할 수 있다.
- Claude Managed Agents 자체 호스팅 출시: Managed Agents 서비스 전체를 자체 인프라에서 운영하고 에이전트·세션·세션 기록을 직접 저장한다. 모델 요청은 선택한 제공자(기본 Anthropic)로 보낸다. 접근은 Anthropic 계정 팀에 요청한다.
- Dreams (리서치 프리뷰): Claude Fable 5.2를 지원한다.
- Claude Code: 최신 v2.1.327. Fable 5.2가 Fable 기본 모델이다. Vertex(global/us/eu)에서 fast mode를 지원하고, Vertex가 거부하면 표준 속도로 돌아간다. v2.1.325에서 zstd 압축 응답 디코딩 관련 보안 문제를 수정했다. v2.1.327에서 `claude mcp add-json <name> -`로 서버 JSON을 stdin에서 읽을 수 있다(토큰을 명령줄에 남기지 않기 위함, Claude Code 세션 안에서는 사용 불가).
- Claude Code Code Mode (실험적, 옵트인): Claude가 JavaScript를 작성해 도구를 호출한다. 조직 단위로 끌 수 있다. v2.1.326부터 에이전트 팀을 켠 상태에서도 Code Mode를 켤 수 있고, 이미 켠 경우 팀을 켠 세션은 Code Mode로 시작한다. 플러그인은 `plugin.json`의 `engines["claude-code"]`로 버전 범위를 지정하며, 범위 밖이면 설치·업데이트·로드되지 않는다.
- Claude Code auto mode: 텔레메트리나 기능 플래그가 꺼져 있으면(LLM 게이트웨이 포함) 서버가 확인하지 못하는 호출은 묻거나 거부한다. 내장 분류기로 폴백하지 않는다. 기존 동작을 유지하려면 `CLAUDE_CODE_AUTO_MODE_SERVER=0`을 설정한다(Bedrock, Vertex, Foundry는 변경 없음).
- Anthropic Python SDK: 최신 v2.10.0 (`billing_policy` 요청 파라미터 베타 지원, linked billing 거부 등 오류 코드와 외부 키 실패·복구 웹훅 이벤트 추가). `claude-fable-5-2`와 `claude-mythos-5-2`는 v2.7.0부터 지원한다.
- GitHub Copilot: Claude Fable 5.2 정식 출시(GA).

## OpenAI
- GPT-6.2 Astra / GPT-6.2 Sol: ChatGPT Work, Codex, API에서 출시. 전문 업무 결과물, 코딩, 컴퓨터 사용, 효율이 개선되고 비용이 낮아졌다. GitHub Copilot에서도 GA(에이전트 코딩·코드 리뷰용)이다.
- openai-python 최신 v3.54.0 (Managed Agents 예산 이벤트 타입 지정). v3.53.0에서 Responses 2시간·8시간 background window와 GPT-6.2 Astra, GPT-6.2 Astra Law 모델 식별자 추가. v3.52.0에서 스케줄 일시중지 사유 추가, 베타 Agents 세션 오류를 스트림 이벤트로 전달. v3.51.0에서 agent, image, Live, external key 지원 확대. v3.50.0에서 Responses에 reasoning input transformations 노출. v3.49.0에서 타입이 지정된 async function tool 추가.
- GPT-6 Rosalind(`gpt-6-rosalind`): v3.46.0에서 모델 카탈로그에 추가, v3.48.0에서 베타 Responses에서 인식.
- Responses 12시간 캐싱과 캐시 쓰기 사용량 지원(v3.45.0).
- Codex CLI: 확인된 최신 버전은 0.169.1 (패치, 확인된 하이라이트 없음). 0.169.0에서 옵트인 터미널 워크스페이스의 여러 side-chat 탭 지원, 에이전트 커맨드 센터에서 이전 작업 검색. 0.167.2에서 `codex agents --no-daemon`으로 에이전트 커맨드 센터를 열 수 있다. Google Cloud Vertex AI에서는 도구 업데이트를 기본으로 점진 전송한다(끄려면 `config.toml`의 `[features]`에 `incremental_tools_legacy_gcp = false`).

## Google (Gemini)
- 최신 모델: Gemini 4 Carbon (`gemini-4-carbon`, GA). 2026-11-30까지 소개 가격은 입력 $2.00 / 출력 $10.00 per 1M 토큰이다. GitHub Copilot에서도 GA이다.
- Gemini 4 Hydrogen (`gemini-4-hydrogen`, GA): 일상적인 코딩·에이전트 워크플로용으로 속도와 효율을 높인 모델. 무료 티어 제공, 유료 가격은 입력 $1.50 / 출력 $7.50 per 1M 토큰이다. 네 번째 사고 수준 `xhigh`를 도입했다. GitHub Copilot에서도 GA이다. 공간·체화 추론을 로보틱스에 활용할 수 있으며, Google Antigravity용 Robotics 플러그인이 나왔다.
- Gemini Decision (`gemini-decision-1`)과 Decisions API: 텍스트·이미지·오디오·영상·PDF에 대해 출력 토큰 없이 빠르고 보정된 이진·범주형·루브릭 판정을 한다.
- Gemini Omni 1.2 Flash (`gemini-omni-1.2-flash`, GA): 최대 15초 영상 생성과 대화형 편집을 지원한다.
- Gemini 3.8 Flash TTS (`gemini-3.8-flash-tts`): 음성 리믹싱은 `POST /v1beta/voices`로 쓴다.
- 임베딩: `gemini-embedding-2`를 쓴다.
- google-genai Python SDK: 최신 v3.5.0. v3.0.0부터 Gemini Developer API 기본 버전이 `v1`이다(기존 `v1beta`). Vertex AI 기본값은 영향이 없다. v3.1.0에서 `gemini-omni-1.2-flash`를 추가하고 `gemini-api video` 기본값으로 지정했다. v3.2.0에서 Decisions API(`client.decisions.create`)를 추가했다. v3.3.0에서 `ThinkingLevel`에 `XHIGH`(`xhigh`)를 추가했다. v3.4.0에서 Interactions SDK에 tool search 타입을 추가하고, `google.genai.interactions`의 non-interactions 이름을 지원 중단(deprecate)했다. v3.5.0에서 environments의 자체 호스팅 BYOS 모드(system_instruction, custom_config, environment_key 포함)와 Live Interactions 타입·auth-token 클라이언트를 추가했다.
- Gemini CLI: 확인된 최신 버전은 v0.68.0.

## 대체된 기법
- `gemini-embedding-2-preview`(지원 중단, 2026-11-06부터 `gemini-embedding-2`로 라우팅) → 모델 문자열을 `gemini-embedding-2`로 바꾼다.
- google-genai 2.x의 기본 `v1beta` → 3.0.0부터 기본 `v1`이므로, `v1beta` 전용 기능은 API 버전을 명시한다.
- VS Code의 GitHub Copilot 에이전트 세션 Local harness → VS Code 1.146에서 지원 중단 예고, 2027년 2월 폐지.
