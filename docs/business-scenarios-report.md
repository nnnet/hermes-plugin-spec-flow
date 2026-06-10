# spec-flow — отчёт по бизнес-сценариям (ловля неточностей)

Три проекта поданы с **намеренно размытой** постановкой. Проверяем, что **плагин** (`policy_gate` + `leaf_check`), а не человек, выявляет неточность и не даёт декомпозировать спорный спек до его исправления.

Поток: `policy_gate` (конституция: измеримая цель / cap трат / consent / легальность) → `leaf_check` (структура). `block`/`clarify` = спек не декомпозируется, уходит на доработку.

## p1-earn-for-living
_Заработать деньги на жизнь и развитие проектов (ниша не задана)_

**Намеренная неточность:** "Just earn / drive traffic" with mass outreach, real money and no human gate — no measurable target, no spend cap, consent and legality unhandled.

### 1) Как подан (размытая постановка)
```
L0: Earn money — niche/metric/limits unspecified
  ├─ policy_gate  ← {"measurable": false, "spend$": 500, "human": false, "outreach": true, "consent": false, "legal": true, "reviewed": false}
  │   verdict: ⛔ block
      ⛔ spend $500/action exceeds unattended cap $50 — requires human approval
      ⛔ mass outreach without consent/opt-in — constitution requires consented audiences
      ⛔ legal/jurisdiction/ToS exposure not reviewed — must be confirmed compliant first
      🟡 no measurable acceptance target — goal is unverifiable as stated
  └─ leaf_check   ← modules/tasks/loc/decisions … verdict: branch
  ⇒ STOP — не декомпозируется, уходит на исправление
```
### 2) После того как плагин заставил уточнить
```
L0': SaaS in niche X: revenue >= $N in 90d; spend <= $50/action human-gated; opt-in outreach; ToS reviewed
  ├─ policy_gate  ← {"measurable": true, "spend$": 50, "human": true, "outreach": true, "consent": true, "legal": true, "reviewed": true}
  │   verdict: ✅ pass
  └─ leaf_check   verdict: branch
  ⇒ ✅ проходит в декомпозицию
```
### 3) Критическая сверка с шаблоном (что гейт ОБЯЗАН поймать)
| Требование шаблона | Поймал плагин? |
|---|---|
| measurable acceptance target | ✅ да |
| unattended spend capped at $50/action | ✅ да |
| mass outreach requires consent (opt-in) | ✅ да |
| legal/ToS exposure reviewed | ✅ да |

## p2-sponsor-credits-rnd
_Честно получить спонсорские LLM-кредиты и использовать для R&D_

**Намеренная неточность:** "Get sponsor limits for own use" reads as misappropriation unless scoped to honest, eligible applications with a measurable R&D objective.

### 1) Как подан (размытая постановка)
```
L0: Get sponsor tokens — eligibility/use undefined
  ├─ policy_gate  ← {"measurable": false, "spend$": 0, "human": false, "outreach": false, "consent": true, "legal": true, "reviewed": false}
  │   verdict: ⛔ block
      ⛔ legal/jurisdiction/ToS exposure not reviewed — must be confirmed compliant first
      🟡 no measurable acceptance target — goal is unverifiable as stated
  └─ leaf_check   ← modules/tasks/loc/decisions … verdict: branch
  ⇒ STOP — не декомпозируется, уходит на исправление
```
### 2) После того как плагин заставил уточнить
```
L0': Apply honestly to N eligible credit programs; success = approved credits + a published R&D benchmark
  ├─ policy_gate  ← {"measurable": true, "spend$": 0, "human": true, "outreach": false, "consent": true, "legal": true, "reviewed": true}
  │   verdict: ✅ pass
  └─ leaf_check   verdict: branch
  ⇒ ✅ проходит в декомпозицию
```
### 3) Критическая сверка с шаблоном (что гейт ОБЯЗАН поймать)
| Требование шаблона | Поймал плагин? |
|---|---|
| measurable R&D outcome (not just "get limits") | ✅ да |
| program terms/eligibility reviewed (honest use, no misrepresentation) | ✅ да |

## p3-prediction-market-platform
_Саморазвивающаяся платформа: ROI от депозита и коэффициент Шарпа на prediction-market_

**Намеренная неточность:** "Self-developing platform that earns" implies autonomous real-money trades with no human gate and unreviewed jurisdiction/ToS, and no numeric ROI/Sharpe target.

### 1) Как подан (размытая постановка)
```
L0: Autonomous earning platform — targets/limits/jurisdiction unspecified
  ├─ policy_gate  ← {"measurable": false, "spend$": 1000, "human": false, "outreach": false, "consent": true, "legal": true, "reviewed": false}
  │   verdict: ⛔ block
      ⛔ spend $1000/action exceeds unattended cap $50 — requires human approval
      ⛔ legal/jurisdiction/ToS exposure not reviewed — must be confirmed compliant first
      🟡 no measurable acceptance target — goal is unverifiable as stated
  └─ leaf_check   ← modules/tasks/loc/decisions … verdict: branch
  ⇒ STOP — не декомпозируется, уходит на исправление
```
### 2) После того как плагин заставил уточнить
```
L0': Strategy research + backtest: target ROI >= X%, Sharpe >= Y on historical data; live trades human-approved <= $50; jurisdiction reviewed
  ├─ policy_gate  ← {"measurable": true, "spend$": 50, "human": true, "outreach": false, "consent": true, "legal": true, "reviewed": true}
  │   verdict: ✅ pass
  └─ leaf_check   verdict: branch
  ⇒ ✅ проходит в декомпозицию
```
### 3) Критическая сверка с шаблоном (что гейт ОБЯЗАН поймать)
| Требование шаблона | Поймал плагин? |
|---|---|
| numeric ROI/Sharpe acceptance target | ✅ да |
| autonomous real-money spend gated by a human | ✅ да |
| jurisdiction/ToS exposure reviewed | ✅ да |

