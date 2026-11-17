# AI interaction log

The assignment asks for the **complete** transcript of how AI tools were used. This file explains
what to include and how to export it. Replace the placeholder section with the real export.

## How to export your transcript
- **ChatGPT**: open the conversation > Share > Copy link (or Settings > Data controls > Export data).
- **Claude**: open the chat > Share, or Settings > Privacy > Export data.
- **Gemini**: open the chat > Share & export > Export to Docs, then download as Markdown/PDF.
- **Cursor / PyCharm AI Assistant / Copilot Chat**: right-click the chat > Export / Copy as Markdown.
- Save it as `docs/ai-transcript.md` (or .pdf) and link it below. Remove any API keys before committing.

## Summary of how AI was used (fill in / adjust)
| Phase | What AI did | What I decided / changed |
|---|---|---|
| Use-case choice | Compared infra provisioning vs incident triage vs deploy orchestration | Picked incident triage: natural need for autonomy + HITL, easy to evaluate |
| Architecture | Proposed loop/runtime/context split | Chose own loop over LangGraph to keep it explainable in the timebox |
| Code | Generated first drafts of modules and tests | Reviewed each; fixed a mock-policy bug where post-fix metrics changed the diagnosis |
| Live testing | Ran eval against Gemini | Found free-tier 429 (15 RPM) -> added retry-hint handling (commit "honour Gemini 429 retry hints") |
| Live testing (2) | Fresh-clone run with Gemini | Gemini chose to roll back the release that added the leaky cache (a valid fix the sim did not model) -> sim + golden set now accept restart OR rollback |
| UI polish | Anthropic's `frontend-design` skill (github.com/anthropics/skills) guided the UI pass: plan tokens first, one loud element, sentence-case action labels, structure that carries meaning | Kept it a quiet operator console; only the approval panel is loud; no behavior or API changes; verified in headless Chrome |
| Thinking panel + calm restyle | Same skill, plus a "calm, minimal, spacious" brief: warm neutral palette, one muted accent, serif headings, 1px dividers; a collapsible Thinking panel built from the existing event stream | No backend change; panel collapses to "Thought for Ns" when the run waits or ends |
| UI polish (2) | Skills from ui-skills.com: `interface-design` (github.com/Dammyjay93/interface-design) for console craft and consistency, `better-ui` (github.com/jakubkrehel/skills, skills/better-ui) for borders, press states, hover and motion; the site's quick rules (tabular-nums, text-wrap balance/pretty, 44x44px hit areas) | Applied as CSS only; verified in headless Chrome |
| Refactor | Split `Agent.step` into planner, executor, approval gate, guards and state modules (LangChain-style "model + tools + middleware" structure, no framework dependency) | Zero behavior change: 35/35 tests pass and the full event trace of 8 mock runs is byte-identical before and after |
| UI polish (3) | Second pass audited rule by rule against the three UI skills: token system, one type scale, button system, empty/loading/error states; approval card rebuilt as a plain-language review | Styling and markup only; verified in headless Chrome incl. error states |
| Readability | Backend helpers split out (loop, context, mock policy, db) and frontend organised into sections | Proven unchanged: identical event traces (backend) and identical DOM + computed styles (frontend) |
| Docs | Drafted HLD/LLD/API/README | Verified every claim against the code; edited for plain language |

## Transcript
_Paste or link the exported transcript here._
