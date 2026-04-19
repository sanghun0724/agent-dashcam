# Agent Dashcam

[English](./README.md) · [한국어](./README.ko.md)

> **코딩 에이전트용 10축 세션 채점기 + 환경 업데이트 임팩트 레이더.**
> 세션에 왜 500k 토큰이 녹았는지 짐작하지 말고, 녹화본을 돌려보세요.

Agent Dashcam은 모든 코딩 에이전트 세션을 구조화된 텔레메트리로 변환합니다 — 그리고 LLM이 자기 과제를 스스로 채점하게 두지 않습니다. Python이 채점하고, Node 훅이 브리핑하고, 사람이 행동합니다.

[Claude Code](https://claude.com/claude-code), [Codex CLI](https://github.com/openai/codex-cli), [Gemini CLI](https://github.com/google-gemini/gemini-cli) 를 지원합니다. `agent-dashcam score --input <jsonl>` 은 provider를 자동 감지하고, provider별 stop-hook wrapper (`hooks/{session,codex,gemini}-stop.mjs`) 가 각 CLI의 네이티브 훅 매니페스트에 연결됩니다. 10축은 vendor-neutral 이지만 일부 휴리스틱 (`read_edit_ratio`, `count_useful_outputs`, 세션 타입 분류) 은 Claude PascalCase 툴 이름에 키잉되어 있어, canonical tool family 로 리프트되기 전까지 Codex / Gemini 세션에서는 중립값으로 fallback 합니다. 자세한 multi-provider 레이어링과 남은 known limitations 은 [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) 와 [`CHANGELOG.md`](./CHANGELOG.md) 참고.

> ### :pushpin: 이 점수가 무엇인지에 대한 안내
>
> **이 숫자들은 절대적 진리가 아닙니다.** "세션 품질" 자체가 본질적으로 모호하고, 맥락 의존적이며, 일부는 주관의 영역입니다 — 탐색성(spike) 세션에서 좋다고 여겨지는 것과 프로덕션 핫픽스에서 좋다고 여겨지는 것은 다릅니다. Agent Dashcam은 그 추상적이고 모호한 것을 **측정 가능하게 만들려는 하나의 의견 있는 시도** 입니다: 10개의 결정론적 프록시, 해당 축이 자연히 낮은 세션 타입을 억제하는 분류기, 그리고 단일 축 하락을 최종 판결로 취급하지 않기 위한 콤보 패턴.
>
> 각 축은 **판정이 아닌 들여다볼 신호** 로 읽으세요. `cost_efficiency` 가 0.42 라는 건 "한 번 살펴볼 가치가 있다" 이지 "당신이 잘못했다" 가 아닙니다. 대시캠은 녹화만 할 뿐, 그 녹화본의 의미는 결국 사람이 판단합니다. 프레임워크는 의도적으로 열려 있습니다 — 가중치를 포크하고, 자신만의 축을 추가하고, 분류기에 반박하세요. 목표는 "감" 에서 "두 번 들여다볼 수 있는 무언가" 로 대화를 옮기는 것입니다.

---

## 왜 "Agent Dashcam"인가

차량용 블랙박스는 차를 멈추지 않습니다. 그저 기록합니다. 충돌, 아찔한 순간, 수상하게 높은 정비소 청구서가 나왔을 때 — 그제서야 녹화본을 돌려봅니다.

LLM 코딩 세션도 똑같은 게 필요합니다. 추론 루프에 50달러가 녹은 세션을 인보이스가 오기 전엔 알아채지 못합니다. 모든 작업을 조용히 Opus 로 라우팅하기 시작한 스킬을 rate-limit 알람이 울리기 전엔 알아채지 못합니다. **녹화본은 이미 있습니다** — Claude Code, Codex, Gemini 모두 세션 JSONL 을 남깁니다. Agent Dashcam 은 그 녹화본을 읽고, 결정론적 규칙으로 채점한 뒤, 다음 세션 시작 시 행동 가능한 팁 하나만 띄웁니다.

| 차량 블랙박스 | Agent Dashcam |
|---|---|
| 논침습적으로 계속 기록 | 이미 존재하는 세션 JSONL을 읽기만 함 |
| 숫자는 거짓말하지 않음 (속도 · GPS · 타임스탬프) | `[0, 1]` 범위의 결정론적 10축 |
| 운전자가 보험사를 속일 수 없음 — 테이프가 있음 | LLM이 자가 채점할 수 없음 — 테이프가 있음 |
| 문제가 생겼을 때만 돌려봄 | 회귀 · 이상 징후 시에만 브리핑 등장 |
| 기록은 운전자 소유 | JSONL 은 로컬 보관, 외부로 전송 안 함 |

**원칙: 측정되는 주체는 채점할 수 없다.** LLM 자가 평가는 낙관 편향이 있고, 결정론적 Python 규칙은 그렇지 않습니다.

---

## 무엇을 측정하는가 — 10축

세 그룹, 열 개의 축, 각각 `[0, 1]` 로 정규화:

### 컨텍스트 / 비용 (4축)

| 축 | 의미 | 좋은 상태 |
|---|---|---|
| `context_efficiency` | 토큰 투입 대비 유의미 산출 | Grep-first, 좁은 범위 Read |
| `cost_efficiency` | 1k output 토큰당 USD | lookup 은 Haiku, edit 은 Sonnet, 아키텍처만 Opus |
| `cost_per_useful_output` | (commit + PR + 통과한 테스트) 당 USD — DX Core 4 스타일 | 세션 안에서 뭐라도 ship |
| `role_focus` | 툴 분포 엔트로피 (모든 게 Read 이면 안 됨) | executor · explore · architect 에이전트에 위임 |

### 인터랙션 품질 (3축, [lucemia](https://github.com/anthropics/claude-code/issues/42796) 실증 기반)

| 축 | 의미 | 좋은 상태 |
|---|---|---|
| `read_edit_ratio` | `reads / edits` — 이상치 2–6 | 각 edit 전에 관련 함수 1–3개 Read |
| `reasoning_loop` | 자기 재시도 언어 밀도 ("다시 해볼게요", "가장 단순한 방법") / 1k 툴콜 | 실행 전 계획, 한 번에 한 가설 |
| `sentiment` | 사용자 메시지 긍정 : 부정 비율 | 명확한 요구사항, 적은 코스 수정 |

### 인프라 건강도 (3축)

| 축 | 의미 | 좋은 상태 |
|---|---|---|
| `constraint_adherence` | 명시된 규칙 위반 (예: `--no-verify`) / 툴콜 | 훅 우회 안 함 |
| `hook_health` | 훅 에러율 | `logs/hook-errors.log` 이 비어 있음 |
| `operational_bottleneck` | 직렬 대 병렬 툴콜 비율, 백그라운드 작업 사용률 | 독립 호출 묶기, 긴 빌드는 백그라운드 |

최종 점수 = 가중 평균, 가중치는 `config.example.json` 에 정의.

---

## 세션 자동 분류

모든 세션이 commit 을 만들어야 하는 건 아닙니다. 순수 리팩터, 문서 편집, 디버그 드라이브 — 각각 **설계상 자연스럽게 낮은** 축이 있습니다. Agent Dashcam 은 모든 세션을 8가지 타입 중 하나로 분류하고, 해당되지 않는 축을 suppress 합니다.

| Type | 감지 휴리스틱 | 자연 낮음 축 (suppress) |
|---|---|---|
| `feature` | commit / PR ≥1 | — |
| `docs` | edit 의 ≥80% 가 `.md`/`.mdx` | `cost_per_useful_output`, `role_focus` |
| `explore` | Read-계열 툴 ≥60%, edit ≤2 | `cost_per_useful_output`, `read_edit_ratio` |
| `refactor` | edit ≥3, test 0, commit 0, edit-heavy | `cost_per_useful_output`, `read_edit_ratio` |
| `bugfix` | edit + test run, commit 없음 | — |
| `debug` | 디버그 키워드 + Bash ≥3 + test | `sentiment` |
| `meta` | edit 의 ≥60% 가 config / settings / yaml / toml | `cost_per_useful_output`, `role_focus` |
| `mixed` | 해당 없음 | — |

**Suppression 규칙**: 리포팅 윈도우 내 세션의 ≥50% 가 특정 축을 suppress 하면 그 축은 액션 아이템 · 콤보 감지에서 건너뜁니다. 채점기는 원시 점수는 여전히 기록합니다 — 자연 낮음에 대해 잔소리를 듣지 않을 뿐.

---

## 콤보 패턴

단일 축 dip 은 노이즈. 페어 dip 은 시그널. Agent Dashcam 은 다섯 가지 콤보를 찾습니다:

| 콤보 | 트리거 | 수정 방향 |
|---|---|---|
| Opus 과용 | `cost_efficiency < 0.4` AND `role_focus < 0.4` | 단순 작업은 `executor(model="haiku")` 로 라우팅 |
| 분석 마비 | `read_edit_ratio == 0` AND `cost_per_useful_output < 0.4` | Read 3회 후 강제 Edit |
| 삽질 루프 | `reasoning_loop < 0.4` AND `sentiment < 0.4` | 먼저 `/plan --consensus` |
| 환경 부패 | `hook_health < 0.5` AND `constraint_adherence < 0.5` | `agent-dashcam envup` 실행 후 훅 패치 |
| 골든 세션 | 10축 전부 ≥ 0.6 AND weighted_avg ≥ 0.75 | 해당 셋업 문서화 — 재현 가능함 |

---

## 아키텍처 — 3 단계, 프롬프트 오염 0

```
┌──────────────┐   ┌────────────────┐   ┌──────────────┐
│  1. COLLECT  │ → │   2. SCORE     │ → │  3. BRIEF    │
│  Node 훅이    │   │  Python stdlib │   │  Node 훅이    │
│  JSONL 에     │   │  결정론적      │   │  다음 세션    │
│  append       │   │  규칙 기반     │   │  시작 시 출력 │
└──────────────┘   └────────────────┘   └──────────────┘
 SessionStop        session-stop         SessionStart
  훅                채점기 실행          브리핑 + 팁
  (데이터만)       (세션당 1회,          (reminder
                    hot path 밖)         프레임으로)
```

**LLM 은 절대 자신을 채점하지 않고, 자기 평가를 자기 컨텍스트에 쓰지 않습니다.** 채점기는 `agent_dashcam_score.py` (stdlib only, LLM 호출 없음), 브리핑은 *다음* 세션 시작 시 `<system-reminder>` 블록에 기록됩니다.

---

## 3-훅 패턴

1. **세션 중** — 기존 훅이 세션 JSONL 에 툴 콜을 append. (Claude Code 가 해주는 걸 Agent Dashcam 이 읽음.)
2. **세션 종료** — `hooks/session-stop.mjs` 가 `scripts/agent_dashcam_score.py` 실행, `scores/<project>__<session>.json` 저장. 대화창 출력 0.
3. **세션 시작** — `hooks/session-start.mjs` 가 해당 프로젝트의 최근 3개 점수를 로드, 다음 대화의 `additionalContext` 에 브리핑 emit (weighted_avg, 최저 non-suppressed 축, 추세 화살표, 액션 가능 팁 1개).

Hot path 토큰 비용: **0**. 모든 채점은 out-of-band.

---

## 결과물 — 아침 Slack 브리핑

`agent-dashcam daily` 를 실행 (또는 cron 으로 걸어두면) 이렇게 DM 으로 도착합니다:

<p align="center">
  <img src="./docs/images/slack-daily-briefing.png" alt="Agent Dashcam 일일 Slack 브리핑" width="520" />
</p>

가중 평균 + 추세 화살표, 10축 바 차트, 임팩트 순 액션 아이템, 콤보 패턴 감지 — 한 눈에 끝. 대시보드 열고 그래프 들여다볼 필요 없이 슬랙 타임라인에서 바로 판단 가능.

---

## 설치

### 옵션 A — 자기 에이전트한테 시키기

이 프롬프트를 Claude Code / Codex CLI / Gemini CLI 에 붙여넣으면 자동 설치됩니다:

> `https://github.com/sanghun0724/agent-dashcam` 에서 Agent Dashcam 을 `~/.claude/agent-dashcam/` 에 설치해줘 (clone 또는 symlink). `config.example.json` 을 `config.json` 으로 복사하고, `python3 scripts/install_hooks.py` 로 훅 연결한 뒤, `python3 -m unittest discover -s fixtures` 로 검증해서 테스트 개수 알려줘. 내가 이미 다른 경로에 설치되어 있으면 `AGENT_DASHCAM_ROOT` 환경변수 써줘.

에이전트가 clone, symlink, 훅 연결, 검증까지 알아서 처리하고 실패 시 보고해줍니다.

### 옵션 B — 수동 설치

```bash
# 1. 레포 클론
git clone https://github.com/sanghun0724/agent-dashcam.git
cd agent-dashcam

# 2. ~/.claude/agent-dashcam/ 으로 심볼릭 링크 또는 복사
ln -s "$PWD" ~/.claude/agent-dashcam
# (또는: cp -R . ~/.claude/agent-dashcam)

# 3. config 템플릿 복사
cp ~/.claude/agent-dashcam/config.example.json ~/.claude/agent-dashcam/config.json

# 4. Claude Code 설정에 훅 연결
python3 ~/.claude/agent-dashcam/scripts/install_hooks.py

# 5. 검증
python3 -m unittest discover -s ~/.claude/agent-dashcam/fixtures
# → Ran 137 tests in 0.6s … OK
```

> **경로 노트**: 스크립트는 기본으로 `~/.claude/agent-dashcam/` 를 루트로 씁니다. `AGENT_DASHCAM_ROOT=/path/to/install` 로 다른 위치 지정 가능 (Python 스크립트 · Node 훅 둘 다 인식).

---

## 사용법

```bash
# 세션 JSONL 한 개 채점 (경로 + 첫 줄 sniff 로 provider 자동 감지)
agent-dashcam score --input /path/to/session.jsonl

# provider 명시
agent-dashcam score --input /path/to/rollout.jsonl --provider codex
agent-dashcam score --input /path/to/session-<uuid>.json --provider gemini

# 오늘의 데일리 리포트 (markdown + Slack blocks payload)
agent-dashcam daily

# 주간 리포트 — WoW delta, combo 빈도, golden session 비율, 요일별 활동 sparkline
agent-dashcam weekly                 # 기본 7일
agent-dashcam weekly --days 14       # 윈도우 확대

# 환경 업데이트 임팩트 분석
agent-dashcam envup

# 최근 30 세션 기준 비용 임계치 재캘리브레이션 (p20/p80)
agent-dashcam calibrate

# 현재 상태 표시
agent-dashcam status
```

데일리 Slack DM payload 는 `reports/daily/daily-<date>.slack.json` 에 저장 — 선호하는 Slack MCP 툴 또는 webhook 으로 연결.

---

## 테스트

```bash
python3 -m unittest discover -s fixtures
```

137 테스트가 커버:

- 10축 전부의 계산 (합성 입력 기반 유닛 테스트)
- 세션 타입 분류기 (8 타입 + 엣지 케이스)
- 동적 임계치 캘리브레이션 (p20/p80 분위 + 샘플 부족 skip)
- 스키마 드리프트 감지
- 100 라인 fixture JSONL 기반 10축 통합 테스트
- 가중치 합 invariant
- Claude · Codex · Gemini adapter + canonical event stream (3개 provider 의 tool-family map, `load_session` dict shape, `iter_events` typing, 잘못된 줄 resilience, fixture 전반의 end-to-end `score_jsonl()` smoke)
- CLI provider dispatch (`--provider {auto,claude,codex,gemini}` + 경로/첫 줄 자동 감지 + claude fallback)
- Codex + Gemini stop-hook wrapper (`node --check`, dry-run, 올바른 adapter 경유 end-to-end 채점)
- OpenAI (gpt-5 / gpt-5-codex / o1-mini / o4-mini) · Gemini (2.5-pro / 2.5-flash / 1.5-pro / 1.5-flash) 가격 조회
- 주간 리포트 (`scripts/weekly_report.py`) — 시간 윈도우 기반 점수 로드, 요일별 세션 버킷팅, 유니코드 sparkline, 세션별 combo 빈도 카운트, golden-rate 통계, 주간 대비 델타, best/worst 세션 픽, Markdown + Slack payload 렌더링

---

## 로드맵

- **Scorer tool-family 인식** — `compute_read_edit_ratio`, `count_useful_outputs`, `classify_session_type` 를 raw Claude PascalCase 문자열에서 canonical family 로 리프트해 Codex / Gemini 세션도 10축 전부 풀-피델리티 채점.
- **Codex / Gemini 용 SessionStart 브리퍼** — 기존 stop-hook wrapper 의 카운터파트. 각 CLI 의 네이티브 `SessionStart` 채널로 다음 세션 브리핑 emit.
- **OTel GenAI exporter** — canonical event stream → OTLP (`gen_ai.*` attributes) 로 Grafana · Honeycomb · Datadog 대시보드에 vendor lock-in 없이 연결.
- PyPI 패키지 (`pip install agent-dashcam`)
- PostHog · Prometheus pusher (옵션)
- Per-skill attribution (어느 스킬이 어느 점수 변화를 유발했는지)

---

## 라이선스

MIT. [`LICENSE`](./LICENSE) 참고.

---

> *"측정할 수 없다면, 개선할 수 없다."* — Peter Drucker
> *"LLM 에게 자기 채점을 맡기면, 점수는 항상 A 다."* — Agent Dashcam
