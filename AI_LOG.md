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
| Docs | Drafted HLD/LLD/README | Edited for plain language |

## Transcript
_Paste or link the exported transcript here._
